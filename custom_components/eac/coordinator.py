"""Coordinator that computes one EAC bill per configured billing period."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import billing
from .billing import BillResult, Tariff, calculate_bill
from .const import (
    CONF_CONSUMPTION,
    CONF_EXPORT,
    CONF_MONTH_RATES,
    CONF_PERIODS,
    CONF_TARIFF,
    CURRENT_ID,
    CURRENT_NAME,
    DOMAIN,
    EUR,
    KWH,
    P_END,
    P_ID,
    P_MANUAL_EXPORT,
    P_MANUAL_GROSS,
    P_NAME,
    P_RATE_MONTH,
    P_START,
    SENSOR_FIELDS,
    UPDATE_INTERVAL,
)
from .rates import resolve_month_rates
from .recorder_util import async_meter_delta, async_state_series
from .state_backfill import async_backfill_states

# Bill line items worth a daily history (cumulative over the period).
_CUMULATIVE = {
    key: (EUR if kind == "money" else KWH)
    for key, kind in SENSOR_FIELDS
    if kind in ("money", "energy")
}

_LOGGER = logging.getLogger(__name__)


class PeriodData:
    """Computed result for a single billing period."""

    def __init__(
        self,
        bill: BillResult | None,
        *,
        start: str,
        end: str,
        rate_month: str,
        has_fuel: bool,
        fuel_source: str,
        prod_source: str,
        consumption_entity: str,
        export_entity: str | None,
        data_start: str | None = None,
        data_end: str | None = None,
        coverage_complete: bool | None = None,
        error: str | None = None,
    ) -> None:
        self.bill = bill
        self.start = start
        self.end = end
        self.rate_month = rate_month
        self.has_fuel = has_fuel
        self.fuel_source = fuel_source
        self.prod_source = prod_source
        self.consumption_entity = consumption_entity
        self.export_entity = export_entity
        self.data_start = data_start
        self.data_end = data_end
        self.coverage_complete = coverage_complete
        self.error = error


def _period_bounds(start_str: str, end_str: str) -> tuple[datetime, datetime]:
    """Local-time window: [start 00:00, end+1day 00:00) so the end day is included."""
    start = dt_util.start_of_local_day(dt_util.parse_date(start_str))
    end = dt_util.start_of_local_day(dt_util.parse_date(end_str)) + timedelta(days=1)
    return start, end


class EacCoordinator(DataUpdateCoordinator):
    """Reads the meters and computes a bill for each billing period."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.entry = entry
        self._backfilled_day = None  # date for which past-day stats were last imported

    @property
    def consumption_entity(self) -> str:
        return self.entry.data[CONF_CONSUMPTION]

    @property
    def export_entity(self) -> str | None:
        return self.entry.data.get(CONF_EXPORT)

    @staticmethod
    def _net(gross: float, exported: float) -> float:
        """Net imported = gross − exported (may be negative when net-exporting)."""
        return gross - exported

    @property
    def periods(self) -> list[dict]:
        return self.entry.options.get(CONF_PERIODS, [])

    def _current_period(self) -> dict | None:
        """Synthetic ongoing period: start = latest configured end, end = today.

        Returns None when no periods are configured, or when the latest end is in
        the future (no sensible ongoing window yet).
        """
        ends = [p[P_END] for p in self.periods if p.get(P_END)]
        if not ends:
            return None
        start = max(ends)  # ISO dates sort chronologically
        today = dt_util.now().date()
        if dt_util.parse_date(start) > today:
            return None
        return {
            P_ID: CURRENT_ID,
            P_NAME: CURRENT_NAME,
            P_START: start,
            P_END: today.isoformat(),
        }

    def all_periods(self) -> list[dict]:
        """Configured periods plus the auto-maintained current period (if any)."""
        periods = list(self.periods)
        current = self._current_period()
        if current:
            periods.append(current)
        return periods

    @property
    def month_rates(self) -> dict:
        return self.entry.options.get(CONF_MONTH_RATES, {})

    def tariff(self) -> Tariff:
        return Tariff.from_overrides(self.entry.options.get(CONF_TARIFF))

    async def _async_update_data(self) -> dict[str, PeriodData]:
        tariff = self.tariff()
        result: dict[str, PeriodData] = {}
        for period in self.all_periods():
            try:
                result[period[P_ID]] = await self._compute(period, tariff)
            except Exception as err:  # noqa: BLE001 — never let one period break the rest
                _LOGGER.warning(
                    "EAC: failed to compute period '%s': %s", period.get(P_NAME), err
                )
                result[period[P_ID]] = PeriodData(
                    None,
                    start=period.get(P_START, ""),
                    end=period.get(P_END, ""),
                    rate_month="",
                    has_fuel=False,
                    fuel_source="error",
                    prod_source="error",
                    consumption_entity=self.consumption_entity,
                    export_entity=self.export_entity,
                    error=str(err),
                )
        try:
            await self._publish_current_daily(tariff)
        except Exception as err:  # noqa: BLE001 — stats are best-effort
            _LOGGER.warning("EAC: current-period daily statistics failed: %s", err)
        return result

    async def _publish_current_daily(self, tariff: Tariff) -> None:
        """Backfill the current-period bill sensors with their full history.

        Past complete days get one end-of-day value each; **today** is filled in
        hour by hour and recomputed every refresh, so the current day populates
        retroactively across the whole day (and stays current at the 5-minute
        cadence). Values are written as each sensor's own long-term statistic
        (statistic id = entity id). Past days are re-imported only when the date
        rolls over; today's hours every refresh.
        """
        current = self._current_period()
        if not current:
            return
        ed = dt_util.parse_date(current[P_END])
        rates = resolve_month_rates(ed.year, ed.month, self.month_rates, tariff.generation)
        if rates["fuel_c"] is None:
            return

        start_dt, end_dt = _period_bounds(current[P_START], current[P_END])
        today = dt_util.now().date()
        today_start = dt_util.start_of_local_day(today)

        # Read the meters as (ts, reading, change) buckets — past as whole days,
        # today hourly, today also at 5-min for the state backfill. gross/net and
        # the bill are computed by the SHARED billing.compute_bill_series — the
        # exact same function (and inputs) the standalone CLI uses.
        async def _series(entity, period, a, b):
            return await async_state_series(self.hass, entity, a, b, period) if entity else []

        cons_past = await _series(self.consumption_entity, "day", start_dt, today_start)
        cons_today = await _series(self.consumption_entity, "hour", today_start, end_dt)
        cons5 = await _series(self.consumption_entity, "5minute", today_start, end_dt)
        if not cons_past and not cons_today:
            return
        exp_past = await _series(self.export_entity, "day", start_dt, today_start)
        exp_today = await _series(self.export_entity, "hour", today_start, end_dt)
        exp5 = await _series(self.export_entity, "5minute", today_start, end_dt)

        gbase = billing.reading_at_start(cons_past or cons_today, start_dt)
        ebase = billing.reading_at_start(exp_past or exp_today or exp5, start_dt)
        fc, pr = rates["fuel_c"], rates["production"]
        day_series = billing.compute_bill_series(cons_past, exp_past, fc, pr, tariff, gbase=gbase, ebase=ebase)
        hour_series = billing.compute_bill_series(cons_today, exp_today, fc, pr, tariff, gbase=gbase, ebase=ebase)
        min5_series = billing.compute_bill_series(cons5, exp5, fc, pr, tariff, gbase=gbase, ebase=ebase)
        today_states = min5_series or hour_series

        if not day_series and not hour_series:
            return

        import_daily = self._backfilled_day != today
        first_bill = (day_series or hour_series)[0][1]
        registry = er.async_get(self.hass)
        resolved = False
        for key, unit in _CUMULATIVE.items():
            stat_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{self.entry.entry_id}_{CURRENT_ID}_{key}"
            )
            if not stat_id:
                continue  # entity not registered yet; picked up on next refresh
            resolved = True
            first = getattr(first_bill, key)
            source = (day_series if import_daily else []) + hour_series
            rows = [
                StatisticData(
                    start=when,
                    state=getattr(bill, key),
                    sum=getattr(bill, key) - first,
                    last_reset=start_dt,
                )
                for when, bill in source
            ]
            if not rows:
                continue
            meta = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_mean=False,
                has_sum=True,
                name=None,
                source="recorder",
                statistic_id=stat_id,
                unit_of_measurement=unit,
                unit_class="energy" if unit == KWH else None,
            )
            async_import_statistics(self.hass, meta, rows)

            # Also write today's 5-minute values as real STATE rows, so they
            # appear in the entity History tab (statistics never render there).
            state_points = [(when, getattr(bill, key)) for when, bill in today_states]
            if state_points:
                live = self.hass.states.get(stat_id)
                attrs = dict(live.attributes) if live else {"unit_of_measurement": unit}
                try:
                    await async_backfill_states(self.hass, stat_id, state_points, attrs)
                except Exception as err:  # noqa: BLE001 — best-effort, never break updates
                    _LOGGER.warning("EAC: state backfill for %s failed: %s", stat_id, err)

        if resolved:
            self._backfilled_day = today

    async def _compute(self, period: dict, tariff: Tariff) -> PeriodData:
        start_dt, end_dt = _period_bounds(period[P_START], period[P_END])

        # Pick the rate month (default = end month of the period).
        end_date = dt_util.parse_date(period[P_END])
        rm = period.get(P_RATE_MONTH)
        if rm:
            rate_year, rate_month = int(rm[:4]), int(rm[5:7])
        else:
            rate_year, rate_month = end_date.year, end_date.month
        rate_month_key = f"{rate_year:04d}-{rate_month:02d}"
        rates = resolve_month_rates(
            rate_year, rate_month, self.month_rates, tariff.generation
        )
        has_fuel = rates["fuel_c"] is not None

        def _result(bill, *, complete, data_start, data_end, error):
            return PeriodData(
                bill,
                start=period[P_START],
                end=period[P_END],
                rate_month=rate_month_key,
                has_fuel=has_fuel,
                fuel_source=rates["fuel_source"],
                prod_source=rates["prod_source"],
                consumption_entity=self.consumption_entity,
                export_entity=self.export_entity,
                data_start=data_start,
                data_end=data_end,
                coverage_complete=complete,
                error=error,
            )

        # Manual override: use entered kWh and skip statistics entirely.
        if period.get(P_MANUAL_GROSS) is not None:
            gross = float(period[P_MANUAL_GROSS])
            exported = float(period.get(P_MANUAL_EXPORT) or 0.0)
            bill = calculate_bill(
                gross, self._net(gross, exported), rates["fuel_c"] or 0.0,
                production_rate=rates["production"], tariff=tariff,
            )
            return _result(bill, complete=True, data_start=None, data_end=None, error=None)

        # gross = consumption meter delta; exported = export meter delta;
        # net = gross − exported.
        gross_usage = await async_meter_delta(
            self.hass, self.consumption_entity, start_dt, end_dt
        )
        if gross_usage is None:
            return _result(
                None, complete=False, data_start=None, data_end=None,
                error=(
                    f"no long-term statistics for {self.consumption_entity} "
                    f"in {period[P_START]}..{period[P_END]}"
                ),
            )

        exported = 0.0
        if self.export_entity:
            exp_usage = await async_meter_delta(
                self.hass, self.export_entity, start_dt, end_dt
            )
            if exp_usage is not None:
                exported = exp_usage.total
        net = self._net(gross_usage.total, exported)

        # Partial coverage: statistics that begin after the period start mean the
        # early part of the period was never recorded.
        complete = gross_usage.data_start <= start_dt + timedelta(hours=2)
        error = None
        if not complete:
            local_first = dt_util.as_local(gross_usage.data_start)
            error = (
                f"PARTIAL: {self.consumption_entity} statistics begin "
                f"{local_first:%Y-%m-%d %H:%M}, after period start {period[P_START]}"
            )
            _LOGGER.warning("EAC period '%s': %s", period.get(P_NAME), error)

        bill = calculate_bill(
            gross_usage.total, net, rates["fuel_c"] or 0.0,
            production_rate=rates["production"], tariff=tariff,
        )
        return _result(
            bill,
            complete=complete,
            data_start=dt_util.as_local(gross_usage.data_start).isoformat(),
            data_end=dt_util.as_local(gross_usage.data_end).isoformat(),
            error=error,
        )

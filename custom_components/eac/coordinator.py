"""Coordinator that computes one EAC bill per configured billing period."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

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
    P_END,
    P_ID,
    P_MANUAL_EXPORT,
    P_MANUAL_GROSS,
    P_NAME,
    P_RATE_MONTH,
    P_START,
    UPDATE_INTERVAL,
)
from .rates import resolve_month_rates
from .recorder_util import MeterUsage, async_consumption_between

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

    @property
    def consumption_entity(self) -> str:
        return self.entry.data[CONF_CONSUMPTION]

    @property
    def export_entity(self) -> str | None:
        return self.entry.data.get(CONF_EXPORT)

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
        return result

    async def _compute(self, period: dict, tariff: Tariff) -> PeriodData:
        start_dt, end_dt = _period_bounds(period[P_START], period[P_END])

        # Manual override: use entered kWh and skip statistics entirely.
        manual = period.get(P_MANUAL_GROSS) is not None
        if manual:
            gross_usage = MeterUsage(
                total=float(period[P_MANUAL_GROSS]), data_start=start_dt, data_end=end_dt
            )
            exported = float(period.get(P_MANUAL_EXPORT) or 0.0)
        else:
            gross_usage = await async_consumption_between(
                self.hass, self.consumption_entity, start_dt, end_dt
            )
            export_usage = None
            if self.export_entity:
                export_usage = await async_consumption_between(
                    self.hass, self.export_entity, start_dt, end_dt
                )
            exported = export_usage.total if export_usage else 0.0

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

        if gross_usage is None:
            return PeriodData(
                None,
                start=period[P_START],
                end=period[P_END],
                rate_month=rate_month_key,
                has_fuel=has_fuel,
                fuel_source=rates["fuel_source"],
                prod_source=rates["prod_source"],
                consumption_entity=self.consumption_entity,
                export_entity=self.export_entity,
                coverage_complete=False,
                error=(
                    f"no long-term statistics for {self.consumption_entity} "
                    f"in {period[P_START]}..{period[P_END]}"
                ),
            )

        # Detect partial coverage: statistics that begin after the period start
        # mean the early part of the period was never recorded (under-counts).
        complete = gross_usage.data_start <= start_dt + timedelta(hours=2)
        error = None
        if not complete:
            local_first = dt_util.as_local(gross_usage.data_start)
            error = (
                f"PARTIAL: {self.consumption_entity} statistics begin "
                f"{local_first:%Y-%m-%d %H:%M}, after period start {period[P_START]} "
                f"— gross is under-counted"
            )
            _LOGGER.warning("EAC period '%s': %s", period.get(P_NAME), error)

        bill = calculate_bill(
            gross_usage.total,
            exported,
            rates["fuel_c"] or 0.0,
            production_rate=rates["production"],
            tariff=tariff,
        )
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
            data_start=dt_util.as_local(gross_usage.data_start).isoformat(),
            data_end=dt_util.as_local(gross_usage.data_end).isoformat(),
            coverage_complete=complete,
            error=error,
        )

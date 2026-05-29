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
    DOMAIN,
    P_END,
    P_FUEL_OVERRIDE,
    P_ID,
    P_NAME,
    P_RATE_MONTH,
    P_START,
    UPDATE_INTERVAL,
)
from .rates import resolve_month_rates
from .recorder_util import async_get_reading_at

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
        error: str | None = None,
    ) -> None:
        self.bill = bill
        self.start = start
        self.end = end
        self.rate_month = rate_month
        self.has_fuel = has_fuel
        self.fuel_source = fuel_source
        self.prod_source = prod_source
        self.error = error


def _period_bounds(start_str: str, end_str: str) -> tuple[datetime, datetime]:
    """Local-time window: [start 00:00, end+1day 00:00) so the end day is included."""
    start = dt_util.start_of_local_day(dt_util.parse_date(start_str))
    end = dt_util.start_of_local_day(dt_util.parse_date(end_str)) + timedelta(days=1)
    return start, end


def _delta(start_val: float | None, end_val: float | None) -> float | None:
    if start_val is None or end_val is None:
        return None
    diff = end_val - start_val
    if diff < 0:
        # meter reset within the period: best-effort assume end value is the total
        _LOGGER.debug("EAC: meter decreased (%.3f→%.3f); assuming reset", start_val, end_val)
        return max(0.0, end_val)
    return diff


class EacCoordinator(DataUpdateCoordinator):
    """Reads the meters and computes a bill for each billing period."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL
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

    @property
    def month_rates(self) -> dict:
        return self.entry.options.get(CONF_MONTH_RATES, {})

    def tariff(self) -> Tariff:
        return Tariff.from_overrides(self.entry.options.get(CONF_TARIFF))

    async def _async_update_data(self) -> dict[str, PeriodData]:
        tariff = self.tariff()
        result: dict[str, PeriodData] = {}
        for period in self.periods:
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
                    error=str(err),
                )
        return result

    async def _compute(self, period: dict, tariff: Tariff) -> PeriodData:
        start_dt, end_dt = _period_bounds(period[P_START], period[P_END])

        c0 = await async_get_reading_at(self.hass, self.consumption_entity, start_dt)
        c1 = await async_get_reading_at(self.hass, self.consumption_entity, end_dt)
        gross = _delta(c0, c1)

        exported = 0.0
        if self.export_entity:
            e0 = await async_get_reading_at(self.hass, self.export_entity, start_dt)
            e1 = await async_get_reading_at(self.hass, self.export_entity, end_dt)
            exported = _delta(e0, e1) or 0.0

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

        # A legacy per-period fuel override (P_FUEL_OVERRIDE) still wins if set.
        if period.get(P_FUEL_OVERRIDE) is not None:
            rates["fuel_c"] = float(period[P_FUEL_OVERRIDE])
            rates["fuel_source"] = "override"

        has_fuel = rates["fuel_c"] is not None

        if gross is None:
            return PeriodData(
                None,
                start=period[P_START],
                end=period[P_END],
                rate_month=rate_month_key,
                has_fuel=has_fuel,
                fuel_source=rates["fuel_source"],
                prod_source=rates["prod_source"],
                error="no meter data",
            )

        bill = calculate_bill(
            gross,
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
        )

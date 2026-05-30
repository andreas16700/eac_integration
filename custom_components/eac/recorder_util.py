"""Read energy consumed by a meter over a period, via long-term statistics.

We deliberately use **long-term statistics** rather than raw recorder states:
raw states are purged (default ~10 days), so a historical billing period would
have no data. Statistics for energy meters (state_class total/total_increasing)
are kept indefinitely, and the ``change`` type gives the energy consumed in each
bucket — robust against meter resets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from homeassistant.components.recorder import get_instance, statistics
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass
class MeterUsage:
    """Energy consumed in a window, plus the actual data coverage found."""

    total: float          # summed hourly ``change`` over the window (kWh)
    data_start: datetime  # start of the first statistics bucket that had data
    data_end: datetime    # start of the last statistics bucket that had data


def _to_dt(value) -> datetime:
    """Normalise a statistics row 'start' (float seconds or datetime) to aware UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(value, tz=timezone.utc)


async def async_consumption_between(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> MeterUsage | None:
    """Energy consumed by ``statistic_id`` in [start, end), or None if no stats.

    For a Home-Assistant-recorded sensor the statistic id equals the entity id.
    Sums the hourly ``change`` over the window and also reports the first/last
    bucket that actually had data, so callers can detect partial coverage (the
    period starting before the meter's statistics began). None means the source
    has no long-term statistics in the window at all.
    """

    def _fetch() -> MeterUsage | None:
        rows = statistics.statistics_during_period(
            hass, start, end, {statistic_id}, "hour", None, {"sum", "change"}
        )
        series = [r for r in (rows.get(statistic_id) or []) if r.get("sum") is not None]
        if not series:
            return None
        # Use the monotonic cumulative `sum` (reset-corrected) rather than summing
        # per-bucket `change`, which can drift on noisy meters. Energy in the
        # window = sum(last) − sum(at period start). The first bucket's
        # (sum − change) is the cumulative value at the window start.
        first, last = series[0], series[-1]
        baseline = float(first["sum"]) - float(first.get("change") or 0.0)
        total = float(last["sum"]) - baseline
        return MeterUsage(
            total=max(0.0, total),
            data_start=_to_dt(first["start"]),
            data_end=_to_dt(last["start"]),
        )

    return await get_instance(hass).async_add_executor_job(_fetch)


async def async_sum_series(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime, period: str
) -> list[tuple[datetime, float, float]]:
    """Per-bucket (start, cumulative_sum, change) for ``statistic_id`` in [start, end)."""

    def _fetch() -> list[tuple[datetime, float, float]]:
        rows = statistics.statistics_during_period(
            hass, start, end, {statistic_id}, period, None, {"sum", "change"}
        )
        return [
            (_to_dt(r["start"]), float(r["sum"]), float(r.get("change") or 0.0))
            for r in (rows.get(statistic_id) or [])
            if r.get("sum") is not None
        ]

    return await get_instance(hass).async_add_executor_job(_fetch)


async def _async_changes(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime, period: str
) -> list[tuple[datetime, float]]:
    """Per-bucket energy change for ``statistic_id`` in [start, end) at ``period``.

    Returns a list of (bucket_start_utc, change_kwh), one per bucket with data.
    """

    def _fetch() -> list[tuple[datetime, float]]:
        rows = statistics.statistics_during_period(
            hass, start, end, {statistic_id}, period, None, {"change"}
        )
        out: list[tuple[datetime, float]] = []
        for row in rows.get(statistic_id) or []:
            change = row.get("change")
            if change is not None:
                out.append((_to_dt(row["start"]), float(change)))
        return out

    return await get_instance(hass).async_add_executor_job(_fetch)


async def async_daily_changes(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """Per-local-day energy change for ``statistic_id`` in [start, end)."""
    return await _async_changes(hass, statistic_id, start, end, "day")


async def async_hourly_changes(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """Per-hour energy change for ``statistic_id`` in [start, end)."""
    return await _async_changes(hass, statistic_id, start, end, "hour")

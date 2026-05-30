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


async def async_state_series(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime, period: str
) -> list[tuple[datetime, float, float]]:
    """Per-bucket (start, meter_reading, change) for ``statistic_id`` in [start, end).

    ``meter_reading`` is the recorded state (the sensor's own value) at the end of
    each bucket. ``change`` is included only so callers can derive the reading at
    the window start (reading − change). The reading may rise or fall (e.g. a net
    meter), so no monotonicity is assumed.
    """

    def _fetch() -> list[tuple[datetime, float, float]]:
        rows = statistics.statistics_during_period(
            hass, start, end, {statistic_id}, period, None, {"state", "change"}
        )
        return [
            (_to_dt(r["start"]), float(r["state"]), float(r.get("change") or 0.0))
            for r in (rows.get(statistic_id) or [])
            if r.get("state") is not None
        ]

    return await get_instance(hass).async_add_executor_job(_fetch)


async def async_meter_delta(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> MeterUsage | None:
    """Change in a meter's reading over [start, end): reading(end) − reading(start).

    Returns None if the meter has no statistics in the window. Also reports the
    first/last bucket with data so callers can detect partial coverage.
    """
    series = await async_state_series(hass, statistic_id, start, end, "hour")
    if not series:
        return None
    # reading at window start = first bucket's reading minus its own change
    baseline = series[0][1] - series[0][2]
    return MeterUsage(
        total=series[-1][1] - baseline,
        data_start=series[0][0],
        data_end=series[-1][0],
    )

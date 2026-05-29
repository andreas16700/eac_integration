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
            hass, start, end, {statistic_id}, "hour", None, {"change"}
        )
        series = rows.get(statistic_id)
        if not series:
            return None
        total = 0.0
        first = last = None
        for row in series:
            change = row.get("change")
            if change is None:
                continue
            total += float(change)
            when = _to_dt(row["start"])
            if first is None:
                first = when
            last = when
        if first is None:
            return None
        # Guard against a net-negative window from a meter reset mid-period.
        return MeterUsage(total=max(0.0, total), data_start=first, data_end=last)

    return await get_instance(hass).async_add_executor_job(_fetch)

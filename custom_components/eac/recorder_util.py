"""Read energy consumed by a meter over a period, via long-term statistics.

We deliberately use **long-term statistics** rather than raw recorder states:
raw states are purged (default ~10 days), so a historical billing period would
have no data. Statistics for energy meters (state_class total/total_increasing)
are kept indefinitely, and the ``change`` type gives the energy consumed in each
bucket — robust against meter resets.
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.recorder import get_instance, statistics
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_consumption_between(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> float | None:
    """Energy consumed by ``statistic_id`` in [start, end), or None if no stats.

    For a Home-Assistant-recorded sensor the statistic id equals the entity id.
    Returns the summed hourly ``change`` over the window. None means the source
    has no long-term statistics covering the window (e.g. wrong sensor type, or
    the period predates statistics collection).
    """

    def _fetch() -> float | None:
        rows = statistics.statistics_during_period(
            hass,
            start,
            end,
            {statistic_id},
            "hour",
            None,
            {"change"},
        )
        series = rows.get(statistic_id)
        if not series:
            return None
        total = 0.0
        seen = False
        for row in series:
            change = row.get("change")
            if change is not None:
                total += float(change)
                seen = True
        if not seen:
            return None
        # Guard against a net-negative window from a meter reset mid-period.
        return max(0.0, total)

    return await get_instance(hass).async_add_executor_job(_fetch)

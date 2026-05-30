"""Read a meter's per-bucket readings over a period, via long-term statistics.

We deliberately use **long-term statistics** rather than raw recorder states:
raw states are purged (default ~10 days), so a historical billing period would
have no data. Statistics for energy meters (state_class total/total_increasing)
are kept indefinitely.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.components.recorder import get_instance, statistics
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


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

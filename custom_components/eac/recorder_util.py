"""Helpers for reading a cumulative meter's value at a point in time."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance, history
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# How far back to look for the last known state at/just before `when`.
_LOOKBACK = timedelta(days=7)
_INVALID = {None, "", "unknown", "unavailable"}


async def async_get_reading_at(
    hass: HomeAssistant, entity_id: str, when: datetime
) -> float | None:
    """Return the meter reading (float) at or just before ``when``.

    Queries the recorder for the entity's states in a window ending at ``when``
    and returns the most recent valid numeric state. Returns None if no usable
    state exists (e.g. recorder has no history that far back).
    """

    def _fetch() -> float | None:
        window_start = when - _LOOKBACK
        result = history.state_changes_during_period(
            hass,
            window_start,
            when,
            entity_id,
            no_attributes=True,
            include_start_time_state=True,
        )
        states = result.get(entity_id) or []
        for state in reversed(states):
            if state.state in _INVALID:
                continue
            try:
                return float(state.state)
            except (ValueError, TypeError):
                continue
        return None

    return await get_instance(hass).async_add_executor_job(_fetch)

"""Write historical STATE rows into the recorder.

This is the only way backfilled values appear in the entity **History tab**:
within the recorder's retention window the History tab renders the raw ``states``
table, and long-term statistics are never shown there. There is no public API for
this — we use the recorder's private ``db_schema`` models on the recorder's own
executor, in one atomic transaction.

Caveats (by design):
* ``States``/``StatesMeta``/``StateAttributes`` are private and can change across
  recorder schema migrations.
* Inserted states are **purged after ``purge_keep_days``** (default 10) like any
  state, so this must be re-run regularly — which the coordinator does.
* Idempotent: a point is skipped if a state already exists at that exact second.
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import partial

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
    StatesMeta,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes

_LOGGER = logging.getLogger(__name__)


async def async_backfill_states(
    hass: HomeAssistant,
    entity_id: str,
    points: list[tuple[datetime, float]],
    attributes: dict,
) -> int:
    """Insert (timestamp, value) rows for ``entity_id`` into the recorder.

    Returns the number of new state rows written. Runs on the recorder executor.
    """
    if not points:
        return 0
    rec = get_instance(hass)
    return await rec.async_add_executor_job(
        partial(_insert, rec, entity_id, points, attributes)
    )


def _insert(rec, entity_id: str, points: list[tuple[datetime, float]], attributes: dict) -> int:
    session = rec.get_session()
    try:
        meta = (
            session.query(StatesMeta)
            .filter(StatesMeta.entity_id == entity_id)
            .first()
        )
        if meta is None:
            meta = StatesMeta(entity_id=entity_id)
            session.add(meta)
            session.flush()
        metadata_id = meta.metadata_id

        lo = points[0][0].timestamp() - 1
        hi = points[-1][0].timestamp() + 1
        existing = {
            round(ts, 0)
            for (ts,) in session.query(States.last_updated_ts)
            .filter(
                States.metadata_id == metadata_id,
                States.last_updated_ts >= lo,
                States.last_updated_ts <= hi,
            )
            .all()
            if ts is not None
        }

        # Shared attributes row (deduplicated via the model's own FNV-1a hash).
        attr_bytes = json_bytes(attributes)
        attr_hash = StateAttributes.hash_shared_attrs_bytes(attr_bytes)
        shared = attr_bytes.decode("utf-8")
        attr_row = (
            session.query(StateAttributes)
            .filter(StateAttributes.hash == attr_hash)
            .first()
        )
        if attr_row is None or attr_row.shared_attrs != shared:
            attr_row = StateAttributes(hash=attr_hash, shared_attrs=shared)
            session.add(attr_row)
            session.flush()
        attributes_id = attr_row.attributes_id

        written = 0
        for when, value in points:
            ts = when.timestamp()
            if round(ts, 0) in existing:
                continue
            session.add(
                States(
                    metadata_id=metadata_id,
                    state=f"{value:.4f}",
                    attributes_id=attributes_id,
                    last_updated_ts=ts,
                    last_changed_ts=None,   # NULL ⇒ equals last_updated (HA convention)
                    last_reported_ts=None,
                    origin_idx=0,
                )
            )
            written += 1
        session.commit()
        return written
    except Exception:  # noqa: BLE001
        session.rollback()
        raise
    finally:
        session.close()

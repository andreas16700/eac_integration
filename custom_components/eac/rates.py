"""Bundled EAC fuel-adjustment rate table + monthly multiplier resolution.

The bundled ``rates_data.json`` holds the published fuel adjustment (¢/kWh) per
bill-end month. Production multipliers are not published historically, so they
default to the tariff value unless overridden per month by the user.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .const import M_FUEL_C, M_PRODUCTION

_RATES_FILE = Path(__file__).parent / "rates_data.json"


@lru_cache(maxsize=1)
def _bundled() -> dict[str, dict]:
    with open(_RATES_FILE, encoding="utf-8") as fh:
        return json.load(fh).get("rates", {})


def month_key(year: int, month: int) -> str:
    """Return the canonical YYYY-MM key."""
    return f"{year:04d}-{month:02d}"


def bundled_fuel_rate(year: int, month: int) -> tuple[float | None, str | None]:
    """Return (rate_c_per_kwh, type) from the bundled table, or (None, None)."""
    rec = _bundled().get(month_key(year, month))
    if rec is None:
        return None, None
    return rec.get("rate_c_per_kwh"), rec.get("type")


def available_months() -> list[str]:
    """Sorted list of YYYY-MM keys present in the bundled table."""
    return sorted(_bundled().keys())


def resolve_month_rates(
    year: int,
    month: int,
    month_overrides: dict | None,
    tariff_production: float,
) -> dict:
    """Resolve the multipliers for a given rate month.

    Override precedence: per-month override → bundled table → tariff default.

    Returns a dict with keys:
      fuel_c       – fuel adjustment ¢/kWh (or None if unknown)
      fuel_source  – "override" | "<bundled type>" | "unknown"
      production   – production multiplier €/kWh
      prod_source  – "override" | "default"
    """
    key = month_key(year, month)
    overrides = (month_overrides or {}).get(key, {})

    # Fuel adjustment
    if overrides.get(M_FUEL_C) is not None:
        fuel_c = float(overrides[M_FUEL_C])
        fuel_source = "override"
    else:
        fuel_c, ftype = bundled_fuel_rate(year, month)
        fuel_source = ftype if fuel_c is not None else "unknown"

    # Production multiplier
    if overrides.get(M_PRODUCTION) is not None:
        production = float(overrides[M_PRODUCTION])
        prod_source = "override"
    else:
        production = tariff_production
        prod_source = "default"

    return {
        "fuel_c": fuel_c,
        "fuel_source": fuel_source,
        "production": production,
        "prod_source": prod_source,
    }

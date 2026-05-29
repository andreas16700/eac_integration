"""Constants for the EAC (ΑΗΚ) integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import CURRENCY_EURO, UnitOfEnergy

DOMAIN = "eac"
PLATFORMS = ["sensor"]
DEFAULT_NAME = "EAC"

# Config entry data
CONF_CONSUMPTION = "consumption_entity"
CONF_EXPORT = "export_entity"

# Options
CONF_PERIODS = "periods"          # list[dict] of billing periods
CONF_TARIFF = "tariff"            # dict of global tariff overrides
CONF_MONTH_RATES = "month_rates"  # dict "YYYY-MM" -> {fuel_c, production}

# Period dict keys
P_ID = "id"
P_NAME = "name"
P_START = "start"            # ISO date "YYYY-MM-DD"
P_END = "end"               # ISO date "YYYY-MM-DD"
P_RATE_MONTH = "rate_month"  # "YYYY-MM" used to pick fuel/production multipliers

# Monthly-rate override dict keys
M_FUEL_C = "fuel_c"          # fuel adjustment, ¢/kWh
M_PRODUCTION = "production"   # production multiplier, €/kWh

UPDATE_INTERVAL = timedelta(hours=1)

# Tariff override fields (must match billing.Tariff field names)
TARIFF_FIELDS = (
    "generation",
    "network",
    "ancillary",
    "pso",
    "res_fund",
    "fixed_meter",
    "fixed_supply",
    "vat",
)

RATE_UNIT = "¢/kWh"
KWH = UnitOfEnergy.KILO_WATT_HOUR
EUR = CURRENCY_EURO

# Bill fields exposed as sensors: (BillResult attribute, kind)
# kind ∈ {"money", "energy", "rate", "prate"}
SENSOR_FIELDS: tuple[tuple[str, str], ...] = (
    ("total", "money"),
    ("production", "money"),
    ("network", "money"),
    ("ancillary", "money"),
    ("meter_data", "money"),
    ("supply", "money"),
    ("subtotal_base", "money"),
    ("fuel_adjustment", "money"),
    ("pso", "money"),
    ("subtotal_pre_vat", "money"),
    ("res_fund", "money"),
    ("subtotal_ex_vat", "money"),
    ("vat", "money"),
    ("gross_kwh", "energy"),
    ("net_kwh", "energy"),
    ("exported_kwh", "energy"),
    ("offset_kwh", "energy"),
    ("fuel_rate_c", "rate"),
    ("production_rate", "prate"),
)

"""Pure EAC (ΑΗΚ) bill calculation — no Home Assistant dependencies.

This module is intentionally framework-free so it can be unit-tested standalone
and reused outside Home Assistant. It mirrors the logic of the original
``eac_bill.py`` CLI, including net metering.

Net metering model
------------------
* ``gross_kwh``    – energy imported from the grid over the period.
* ``exported_kwh`` – energy exported to the grid over the period (optional).
* The exported energy offsets imported energy: ``offset = min(gross, export)``.
* ``net_kwh = gross - offset`` is billed in full (all charges).
* The offset portion (``gross - net``) is billed for network, ancillary and PSO
  only — it is exempt from **production**, **fuel adjustment** and **RES fund**.
* Excess export beyond import is banked/lost (not paid out) — not modelled here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class Tariff:
    """EAC Tariff 01 (residential, low voltage) per-unit charges.

    Defaults reflect the latest values confirmed against a 2026 bill. The
    network rate is municipality-dependent, so it (and any other field) can be
    overridden per Home Assistant config entry.
    """

    generation: float = 0.1789   # Παραγωγή Ηλεκτρικής Ενέργειας (€/kWh, net only)
    network: float = 0.0366      # Χρήση Δικτύου (€/kWh, gross) — municipality-specific
    ancillary: float = 0.0065    # Επικουρικές Υπηρεσίες (€/kWh, gross)
    pso: float = 0.00051         # Υποχρ. Παροχ. Δημόσ. Υπηρ. (€/kWh, gross)
    res_fund: float = 0.005      # Ταμείο ΑΠΕ & ΕΞΕ (€/kWh, net only)
    fixed_meter: float = 0.96    # Διαχείριση Μετρητικών Δεδομένων (€, fixed)
    fixed_supply: float = 6.88   # Προμήθεια Ηλεκτρικής Ενέργειας (€, fixed)
    vat: float = 0.05            # ΦΠΑ (fraction)

    @classmethod
    def from_overrides(cls, overrides: dict | None) -> "Tariff":
        """Build a Tariff from a partial dict of overrides (None/missing ignored)."""
        if not overrides:
            return cls()
        valid = {f.name for f in fields(cls)}
        base = asdict(cls())
        base.update(
            {k: float(v) for k, v in overrides.items() if k in valid and v is not None}
        )
        return cls(**base)


@dataclass(frozen=True)
class BillResult:
    """Itemised result of an EAC bill calculation. All money values in €."""

    gross_kwh: float
    net_kwh: float
    exported_kwh: float
    offset_kwh: float
    fuel_rate_c: float          # fuel adjustment rate applied, in ¢/kWh
    production_rate: float       # production multiplier applied, in €/kWh
    production: float
    network: float
    ancillary: float
    meter_data: float
    supply: float
    subtotal_base: float
    fuel_adjustment: float
    pso: float
    subtotal_pre_vat: float
    res_fund: float
    subtotal_ex_vat: float
    vat: float
    total: float


def split_net_metering(gross_kwh: float, exported_kwh: float) -> tuple[float, float]:
    """Return (net_kwh, offset_kwh) given gross import and export."""
    gross = max(0.0, gross_kwh)
    exported = max(0.0, exported_kwh)
    offset = min(gross, exported)
    return gross - offset, offset


def calculate_bill(
    gross_kwh: float,
    exported_kwh: float = 0.0,
    fuel_rate_c: float = 0.0,
    production_rate: float | None = None,
    tariff: Tariff | None = None,
) -> BillResult:
    """Calculate an EAC bill.

    ``fuel_rate_c`` (¢/kWh) and ``production_rate`` (€/kWh) are the monthly
    multipliers selected for the period (see the rate month). When
    ``production_rate`` is None the tariff default is used.
    """
    tariff = tariff or Tariff()
    gen_rate = tariff.generation if production_rate is None else production_rate
    net_kwh, offset_kwh = split_net_metering(gross_kwh, exported_kwh)
    rate_fuel = fuel_rate_c / 100.0  # ¢/kWh → €/kWh

    production = net_kwh * gen_rate                    # net only
    network = gross_kwh * tariff.network              # gross
    ancillary = gross_kwh * tariff.ancillary          # gross
    meter_data = tariff.fixed_meter                   # fixed
    supply = tariff.fixed_supply                      # fixed
    subtotal_base = production + network + ancillary + meter_data + supply

    fuel_adjustment = net_kwh * rate_fuel             # net only
    pso = gross_kwh * tariff.pso                       # gross
    subtotal_pre_vat = subtotal_base + fuel_adjustment + pso

    res_fund = net_kwh * tariff.res_fund              # net only, outside VAT
    subtotal_ex_vat = subtotal_pre_vat + res_fund
    vat = subtotal_pre_vat * tariff.vat               # VAT excludes RES fund
    total = subtotal_ex_vat + vat

    return BillResult(
        gross_kwh=gross_kwh,
        net_kwh=net_kwh,
        exported_kwh=exported_kwh,
        offset_kwh=offset_kwh,
        fuel_rate_c=fuel_rate_c,
        production_rate=gen_rate,
        production=production,
        network=network,
        ancillary=ancillary,
        meter_data=meter_data,
        supply=supply,
        subtotal_base=subtotal_base,
        fuel_adjustment=fuel_adjustment,
        pso=pso,
        subtotal_pre_vat=subtotal_pre_vat,
        res_fund=res_fund,
        subtotal_ex_vat=subtotal_ex_vat,
        vat=vat,
        total=total,
    )

"""Pure EAC (ΑΗΚ) bill calculation — no Home Assistant dependencies.

The bill for a period is a function of three things only:
  * gross imported energy (kWh) over the period,
  * net imported energy (kWh) over the period,
  * the period's fuel-adjustment and production multipliers.

Both gross and net come directly from input sensors. The offset (gross − net,
the part of import cancelled by export) is billed for network, ancillary and
PSO, but NOT for production, fuel adjustment or RES fund — those apply to the
net imported energy only. The total can rise or fall as net imported changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import timedelta


@dataclass(frozen=True)
class Tariff:
    """EAC Tariff 01 (residential, low voltage) per-unit charges."""

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
    offset_kwh: float           # gross − net (import cancelled by export)
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


def calculate_bill(
    gross_kwh: float,
    net_kwh: float,
    fuel_rate_c: float = 0.0,
    production_rate: float | None = None,
    tariff: Tariff | None = None,
) -> BillResult:
    """Calculate an EAC bill from gross + net imported energy and the multipliers.

    ``fuel_rate_c`` (¢/kWh) and ``production_rate`` (€/kWh) are the period's
    multipliers; when ``production_rate`` is None the tariff default is used.
    """
    tariff = tariff or Tariff()
    gen_rate = tariff.generation if production_rate is None else production_rate
    rate_fuel = fuel_rate_c / 100.0  # ¢/kWh → €/kWh

    production = net_kwh * gen_rate                    # net only
    network = gross_kwh * tariff.network               # gross
    ancillary = gross_kwh * tariff.ancillary           # gross
    meter_data = tariff.fixed_meter                    # fixed
    supply = tariff.fixed_supply                       # fixed
    subtotal_base = production + network + ancillary + meter_data + supply

    fuel_adjustment = net_kwh * rate_fuel              # net only
    pso = gross_kwh * tariff.pso                        # gross
    subtotal_pre_vat = subtotal_base + fuel_adjustment + pso

    res_fund = net_kwh * tariff.res_fund               # net only, outside VAT
    subtotal_ex_vat = subtotal_pre_vat + res_fund
    vat = subtotal_pre_vat * tariff.vat                # VAT excludes RES fund
    total = subtotal_ex_vat + vat

    return BillResult(
        gross_kwh=gross_kwh,
        net_kwh=net_kwh,
        offset_kwh=gross_kwh - net_kwh,
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


def baseline(series: list) -> float:
    """Reading at the start of the first bucket = state − change of bucket 0."""
    return series[0][1] - series[0][2] if series else 0.0


def reading_at_start(series: list, start, tol_hours: float = 36.0) -> float:
    """Cumulative meter reading at the period start, else 0.

    If the meter has data at (within ``tol_hours`` of) the period start, return
    its reading there (first bucket's state − change). Otherwise the meter had no
    reading at the start (e.g. solar added mid-period) → treat as 0.
    ``series`` is ascending (key, reading, change) with key a datetime.
    """
    if series and series[0][0] <= start + timedelta(hours=tol_hours):
        return series[0][1] - series[0][2]
    return 0.0


def compute_bill_series(
    cons: list,
    exp: list,
    fuel_rate_c: float,
    production_rate: float | None,
    tariff: Tariff | None = None,
    *,
    gbase: float | None = None,
    ebase: float | None = None,
) -> list:
    """Bill at each point, from cumulative-meter buckets — the single source of
    truth used by both the live integration and the standalone CLI.

    ``cons`` / ``exp`` are ascending lists of ``(key, reading, change)`` where
    ``key`` is the bucket's timestamp. For each consumption point:
        gross = reading − gbase
        net   = gross − (latest export reading ≤ key − ebase)
    Baselines default to the first bucket's reading at its start. Returns a list
    of ``(key, BillResult)``.
    """
    if not cons:
        return []
    if gbase is None:
        gbase = baseline(cons)
    if ebase is None:
        ebase = baseline(exp)
    exp_at = {k: r for k, r, _ in exp}
    out = []
    last_e = ebase
    for key, reading, _ in cons:
        gross = reading - gbase
        if key in exp_at:
            last_e = exp_at[key]
        net = gross - (last_e - ebase)
        out.append(
            (key, calculate_bill(gross, net, fuel_rate_c,
                                 production_rate=production_rate, tariff=tariff))
        )
    return out

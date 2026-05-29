# EAC (Electricity Authority of Cyprus / ΑΗΚ) — Home Assistant integration

Turns your cumulative energy meters into an itemised **EAC electricity bill**
(Tariff 01, residential / low-voltage), with full **net-metering** support.

Each line of an EAC bill becomes a sensor. A **billing period** (start → end)
is a device that groups those sensors. Change a period's dates and the sensors
recompute from your meter history.

## How it works

Inputs (config flow):
- **Energy consumption meter** — cumulative grid import (kWh). Required.
- **Energy export meter** — cumulative grid export (kWh). Optional.

For each billing period the integration reads the meters at the period start and
end (from the recorder) to get the energy used:

```
gross imported = consumption(end) − consumption(start)
exported       = export(end) − export(start)        # 0 if no export meter
offset         = min(gross, exported)
net imported   = gross − offset
```

Billing rules (matching real EAC net-metering bills):
- **Net imported** is billed in full.
- The **offset** (gross − net) is billed for network, ancillary and PSO only —
  it is exempt from **production**, **fuel adjustment** and **RES fund**.
- VAT applies to everything except the RES fund.

### Sensors (per period)

`total`, `production`, `network`, `ancillary`, `meter_data`, `supply`,
`subtotal_base`, `fuel_adjustment`, `pso`, `subtotal_pre_vat`, `res_fund`,
`subtotal_ex_vat`, `vat` (all €), plus `gross_kwh`, `net_kwh`, `exported_kwh`,
`offset_kwh` (kWh), `fuel_rate_c` (¢/kWh) and `production_rate` (€/kWh).

The `total` sensor carries the period metadata as attributes (start/end, rate
month, fuel/production rate + source, gross/net/exported kWh).

## Rates

The **fuel adjustment** and **production** multipliers are *monthly* values. A
period uses a single month's multipliers — by default the **month of the period
end**, or an explicit `YYYY-MM` you set on the period.

- Fuel adjustment: bundled table extracted from the official EAC spreadsheet
  (`rates_data.json`, ΧΑΜΗΛΗ / low-voltage bimonthly, revised values, 2013–2026),
  overridable per month under **Options → Monthly rates** (e.g. before EAC
  publishes a month).
- Production: defaults to the tariff value, overridable per month the same way.

Other charges (network — *municipality-specific*, ancillary, PSO, RES fund,
fixed charges, VAT) live under **Options → Tariff charges**.

## Installation

### HACS (custom repository)
1. HACS → ⋮ → **Custom repositories** → add this repo, category **Integration**.
2. Install **EAC (Electricity Authority of Cyprus)**, then restart HA.
3. **Settings → Devices & Services → Add Integration → EAC**.

### Manual
Copy `custom_components/eac/` into your HA `config/custom_components/` and restart.

## Configure

1. Add the integration and pick your consumption (and optional export) meter.
2. **Configure** → **Add billing period**: name, start, end (and optional rate
   month). Sensors populate for that period.
3. Adjust dates / rates any time via **Configure**; the entry reloads and the
   sensors recompute.

## Defaults & accuracy

Tariff defaults reflect a 2026 low-voltage bill: production `0.1789 €/kWh`,
network `0.0366 €/kWh` (verify against *your* municipality), ancillary
`0.0065`, PSO `0.00051`, RES fund `0.005 €/kWh` (billed on net), fixed
`0.96 + 6.88 €`, VAT `5%`. Override any of them under **Tariff charges**.

## Development

The billing math is framework-free in `custom_components/eac/billing.py` and
unit-tested without Home Assistant:

```
python3 tests/test_billing.py
```

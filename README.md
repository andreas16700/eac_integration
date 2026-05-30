# EAC (Electricity Authority of Cyprus / ΑΗΚ) — Home Assistant integration

Turns your cumulative energy meters into an itemised **EAC electricity bill**
(Tariff 01, residential / low-voltage), with full **net-metering** support.

Each line of an EAC bill becomes a sensor. A **billing period** (start → end)
is a device that groups those sensors. Change a period's dates and the sensors
recompute from your meter history.

## How it works

Inputs (config flow) — two cumulative kWh meters:
- **Consumption / grid-import meter** — energy imported from the grid (= gross
  imported). Required.
- **Solar export meter** — energy exported to the grid. Optional.

From these two the integration derives **gross** (= consumption) and **net
imported** (= gross − export), then computes the bill from gross, net and the
period's multipliers. Net can go negative when you export more than you import,
and the total falls with it. That's expected.

> **Requirement:** the meter sensors must have a `state_class` so Home Assistant
> keeps **long-term statistics** for them. The integration reads those statistics
> (not raw history), so it works for billing periods far in the past. The recorder
> integration must be enabled (it is, by default).

For each billing period:

```
gross imported = consumption(end) − consumption(start)
exported       = export(end) − export(start)        # 0 if no export meter
net imported   = gross − exported                   # may be negative
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

### Current period & daily history

When at least one period is configured, a **Current** period device is added
automatically: its start = the latest configured period's end, its end = today
(`is_current_period: true` on its total). Values recompute **every 5 minutes**.

The current-period bill **sensors themselves** are populated with their full
history (long-term statistics on each sensor's own entity id): every past day
gets one **end-of-day** value, and **today is filled in hour by hour** and kept
current at the 5-minute cadence. So opening `sensor.eac_current_total` (or any
`sensor.eac_current_*`) shows the bill accumulating across the whole period,
including the current day. Past days are re-imported only on date rollover;
today's hours every refresh.

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

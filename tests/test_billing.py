"""Standalone tests for the pure billing math (no Home Assistant required).

Run with:  python3 tests/test_billing.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "eac"))

from billing import Tariff, calculate_bill, split_net_metering  # noqa: E402


def approx(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(a - b) <= tol


def test_split_net_metering():
    assert split_net_metering(1005, 548) == (457, 548)
    assert split_net_metering(732.05, 56.01) == (676.04, 56.01)
    # export exceeds import → net 0, offset capped at gross
    assert split_net_metering(100, 250) == (0, 100)
    # no export
    assert split_net_metering(700, 0) == (700, 0)


def test_neighbor_net_metering():
    # gross 1005 / net 457 (export 548), May 2026 fuel 2.3796, production 0.1789, VAT 5%
    b = calculate_bill(1005, 548, 2.3796)
    assert approx(b.production, 81.76), b.production
    assert approx(b.network, 36.78), b.network
    assert approx(b.fuel_adjustment, 10.87), b.fuel_adjustment
    assert approx(b.res_fund, 2.29), b.res_fund      # on net 457
    assert approx(b.vat, 7.22), b.vat                # 5%
    assert approx(b.total, 153.80), b.total


def test_user_net_metering():
    # gross 732.05 / net 676.04 (export 56.01), May 2026
    b = calculate_bill(732.05, 56.01, 2.3796)
    assert approx(b.total, 189.02), b.total


def test_normal_no_export():
    # 700 kWh normal, May 2026
    b = calculate_bill(700, 0, 2.3796)
    assert b.net_kwh == 700 and b.offset_kwh == 0
    assert approx(b.production, 125.23), b.production   # 700 * 0.1789
    assert approx(b.res_fund, 3.50), b.res_fund          # on 700
    assert approx(b.total, 192.76), b.total


def test_production_rate_override():
    # old production multiplier 0.1034 selectable per month
    b = calculate_bill(700, 0, 2.3796, production_rate=0.1034)
    assert approx(b.production, 72.38), b.production     # 700 * 0.1034
    assert approx(b.production_rate, 0.1034)


def test_tariff_override_network():
    # municipality-specific network rate
    t = Tariff.from_overrides({"network": 0.0300})
    b = calculate_bill(1000, 0, 0.0, tariff=t)
    assert approx(b.network, 30.0), b.network


def test_offset_exempt_from_production_and_fuel():
    # The (gross-net) offset must NOT carry production / fuel / RES.
    full = calculate_bill(1000, 0, 5.0)       # net 1000
    half = calculate_bill(1000, 400, 5.0)     # net 600, offset 400
    # production scales with net only
    assert approx(half.production, full.production * 0.6)
    assert approx(half.fuel_adjustment, full.fuel_adjustment * 0.6)
    assert approx(half.res_fund, full.res_fund * 0.6)
    # network scales with gross (unchanged)
    assert approx(half.network, full.network)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

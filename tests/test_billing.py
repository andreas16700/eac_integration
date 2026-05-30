"""Standalone tests for the pure billing math (no Home Assistant required).

Run with:  python3 tests/test_billing.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "eac"))

from billing import Tariff, calculate_bill  # noqa: E402


def approx(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(a - b) <= tol


def test_neighbor():
    # gross 1005, net 457, May 2026 fuel 2.3796, production 0.1789, VAT 5%
    b = calculate_bill(1005, 457, 2.3796)
    assert approx(b.offset_kwh, 548), b.offset_kwh
    assert approx(b.production, 81.76), b.production      # net 457
    assert approx(b.network, 36.78), b.network            # gross 1005
    assert approx(b.fuel_adjustment, 10.87), b.fuel_adjustment
    assert approx(b.res_fund, 2.29), b.res_fund           # net 457
    assert approx(b.vat, 7.22), b.vat
    assert approx(b.total, 153.80), b.total


def test_user():
    b = calculate_bill(732.05, 676.04, 2.3796)
    assert approx(b.total, 189.02), b.total


def test_normal_net_equals_gross():
    b = calculate_bill(700, 700, 2.3796)
    assert b.offset_kwh == 0
    assert approx(b.production, 125.23), b.production      # 700 * 0.1789
    assert approx(b.res_fund, 3.50), b.res_fund
    assert approx(b.total, 192.76), b.total


def test_production_rate_override():
    b = calculate_bill(700, 700, 2.3796, production_rate=0.1034)
    assert approx(b.production, 72.38), b.production
    assert approx(b.production_rate, 0.1034)


def test_tariff_override_network():
    t = Tariff.from_overrides({"network": 0.0300})
    b = calculate_bill(1000, 1000, 0.0, tariff=t)
    assert approx(b.network, 30.0), b.network


def test_offset_exempt_from_net_charges():
    # Production / fuel / RES scale with NET; network/ancillary/PSO with GROSS.
    full = calculate_bill(1000, 1000, 5.0)   # net == gross
    part = calculate_bill(1000, 600, 5.0)    # net 600, offset 400
    assert approx(part.production, full.production * 0.6)
    assert approx(part.fuel_adjustment, full.fuel_adjustment * 0.6)
    assert approx(part.res_fund, full.res_fund * 0.6)
    assert approx(part.network, full.network)      # gross unchanged
    assert approx(part.ancillary, full.ancillary)
    assert approx(part.pso, full.pso)


def test_total_falls_when_net_falls():
    # If net imported drops (e.g. more solar export), the total drops too.
    more = calculate_bill(1000, 800, 5.0)
    less = calculate_bill(1000, 300, 5.0)
    assert less.total < more.total


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

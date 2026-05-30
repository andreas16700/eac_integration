#!/usr/bin/env python3
"""Standalone EAC current-period STATE backfiller.

Runs inside the Home Assistant container (uses its pymysql + the recorder DB +
the integration's OWN billing.calculate_bill). Computes each bill-item value per
5 minutes from the recorder's statistics and inserts `states` rows so the values
appear in the entity History tab — no HA restart, independent of the loaded code.

  docker exec homeassistant python3 /config/eac_backfill.py --since-hours 1 --dry-run
  docker exec homeassistant python3 /config/eac_backfill.py --since-hours 1
"""
from __future__ import annotations
import argparse, json, re, sys
import datetime as dt
from zoneinfo import ZoneInfo
import pymysql

sys.path.insert(0, "/config/custom_components/eac")
import billing  # the integration's own calculation code  # noqa: E402

CFG = "/config"
TZ = ZoneInfo("Asia/Nicosia")

# billing.BillResult attribute -> eac_current_* entity suffix
MAP = {
    "total": "total", "production": "production", "network": "network_use",
    "ancillary": "ancillary_services", "meter_data": "metering_data_management",
    "supply": "supply", "subtotal_base": "subtotal_at_base_fuel_price",
    "fuel_adjustment": "fuel_adjustment", "pso": "pso",
    "subtotal_pre_vat": "subtotal_before_vat", "res_fund": "res_ee_fund",
    "subtotal_ex_vat": "subtotal_excl_vat", "vat": "vat",
    "gross_kwh": "gross_imported", "net_kwh": "net_imported",
    "offset_kwh": "net_metering_offset",
}


def db_conn():
    sec = open(f"{CFG}/secrets.yaml", encoding="utf-8").read()
    m = re.search(r"recorder_db_url:\s*\"?(mysql\+pymysql://[^\"\s]+)", sec)
    u = re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^/:]+)(?::(\d+))?/([^?]+)", m.group(1))
    user, pw, host, port, name = u.groups()
    return pymysql.connect(host=host, port=int(port or 3306), user=user,
                           password=pw, database=name, charset="utf8mb4", autocommit=False)


def load_cfg():
    e = next(x for x in json.load(open(f"{CFG}/.storage/core.config_entries"))["data"]["entries"]
             if x["domain"] == "eac")
    opts = e.get("options", {})
    start_date = max((p["end"] for p in opts.get("periods", []) if p.get("end")), default=None)
    return dict(consumption=e["data"]["consumption_entity"], export=e["data"].get("export_entity"),
                start_date=start_date, month_rates=opts.get("month_rates", {}),
                tariff=billing.Tariff.from_overrides(opts.get("tariff")))


def fuel_and_prod(cfg, rate_month):
    mo = (cfg["month_rates"] or {}).get(rate_month, {})
    prod = float(mo["production"]) if mo.get("production") is not None else cfg["tariff"].generation
    if mo.get("fuel_c") is not None:
        return float(mo["fuel_c"]), prod
    rates = json.load(open(f"{CFG}/custom_components/eac/rates_data.json"))["rates"]
    return float(rates[rate_month]["rate_c_per_kwh"]), prod


def stat_meta(cur, sid):
    cur.execute("SELECT id FROM statistics_meta WHERE statistic_id=%s", (sid,))
    r = cur.fetchone()
    return r[0] if r else None


def reading_at(cur, mid, ts):
    if mid is None:
        return None
    for tbl in ("statistics", "statistics_short_term"):
        cur.execute(f"SELECT state FROM {tbl} WHERE metadata_id=%s AND start_ts<=%s "
                    "AND state IS NOT NULL ORDER BY start_ts DESC LIMIT 1", (mid, ts))
        r = cur.fetchone()
        if r:
            return float(r[0])
    return None


def series_5m(cur, mid, lo, hi):
    if mid is None:
        return []
    cur.execute("SELECT start_ts, state FROM statistics_short_term WHERE metadata_id=%s "
                "AND start_ts>=%s AND start_ts<%s AND state IS NOT NULL ORDER BY start_ts", (mid, lo, hi))
    return [(float(s), float(v)) for s, v in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-hours", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    if not cfg["start_date"]:
        print("No configured period -> no current period."); return
    ps_ts = dt.datetime.fromisoformat(cfg["start_date"]).replace(tzinfo=TZ).timestamp()
    now = dt.datetime.now(TZ)
    rate_month = f"{now.year:04d}-{now.month:02d}"
    fuel_c, prod = fuel_and_prod(cfg, rate_month)
    lo = (now - dt.timedelta(hours=args.since_hours)).timestamp()
    hi = now.timestamp() + 1

    conn = db_conn(); cur = conn.cursor()
    src = stat_meta(cur, cfg["consumption"])
    exp = stat_meta(cur, cfg["export"]) if cfg["export"] else None
    base_g = reading_at(cur, src, ps_ts) or 0.0
    base_e = reading_at(cur, exp, ps_ts) or 0.0          # no solar reading -> 0
    print(f"period_start={cfg['start_date']} rate_month={rate_month} fuel={fuel_c} prod={prod}")
    print(f"consumption base={base_g} export base={base_e} (export={cfg['export']})")

    cons = series_5m(cur, src, lo, hi)
    exp_map = dict(series_5m(cur, exp, lo, hi))
    if not cons:
        print("No 5-minute source data in window."); return

    meta = {}
    for suffix in MAP.values():
        eid = f"sensor.eac_current_{suffix}"
        cur.execute("SELECT metadata_id FROM states_meta WHERE entity_id=%s", (eid,))
        mr = cur.fetchone()
        if not mr:
            continue
        mid = mr[0]
        cur.execute("SELECT attributes_id FROM states WHERE metadata_id=%s AND attributes_id IS NOT NULL "
                    "ORDER BY last_updated_ts DESC LIMIT 1", (mid,))
        ar = cur.fetchone()
        cur.execute("SELECT last_updated_ts FROM states WHERE metadata_id=%s AND last_updated_ts>=%s "
                    "AND last_updated_ts<%s", (mid, lo - 300, hi + 300))
        buckets = {int(x[0] // 300) for x in cur.fetchall() if x[0] is not None}
        meta[suffix] = (mid, ar[0] if ar else None, buckets)

    # Exact same calculation + baselines as the integration:
    cons_series = [(ts, v, 0.0) for ts, v in cons]              # (key, reading, change)
    exp_series = [(ts, v, 0.0) for ts, v in sorted(exp_map.items())]
    points = billing.compute_bill_series(
        cons_series, exp_series, fuel_c, prod, cfg["tariff"], gbase=base_g, ebase=base_e
    )

    preview, inserted = [], 0
    for ts, bill in points:
        preview.append((ts, bill.total, bill.gross_kwh, bill.net_kwh))
        for attr, suffix in MAP.items():
            if suffix not in meta:
                continue
            mid, aid, buckets = meta[suffix]
            if int(ts // 300) in buckets:
                continue
            if not args.dry_run:
                cur.execute(
                    "INSERT INTO states (metadata_id,state,attributes_id,last_updated_ts,"
                    "last_changed_ts,last_reported_ts,origin_idx) VALUES (%s,%s,%s,%s,%s,%s,0)",
                    (mid, f"{getattr(bill, attr):.4f}", aid, ts, ts, ts))
            if suffix == "total":
                inserted += 1

    print(f"\n{len(cons)} 5-min points. Sample (ts, total€, gross, net):")
    for ts, tot, g, n in preview[:3] + preview[-3:]:
        print(f"  {dt.datetime.fromtimestamp(ts, TZ):%m-%d %H:%M}  €{tot:7.2f}  g={g:7.2f} n={n:7.2f}")
    if args.dry_run:
        print(f"\nDRY-RUN — would insert ~{inserted} rows/sensor across {len(meta)} sensors.")
        conn.rollback()
    else:
        conn.commit()
        print(f"\nINSERTED ~{inserted} rows/sensor across {len(meta)} sensors.")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()

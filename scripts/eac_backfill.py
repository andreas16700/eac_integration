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
from pymysql.constants import CLIENT

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
    # FOUND_ROWS so UPDATE reports matched (not changed) rows — required for the
    # idempotent upsert: a re-run with identical values must NOT insert duplicates.
    return pymysql.connect(host=host, port=int(port or 3306), user=user,
                           password=pw, database=name, charset="utf8mb4",
                           autocommit=False, client_flag=CLIENT.FOUND_ROWS)


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
    """Meter reading AT ``ts`` = state of the last bucket ENDING at/before ts,
    i.e. the bucket starting strictly before ts (its `state` is the end-of-bucket
    reading). This is the reading at the period start — the offset."""
    if mid is None:
        return None
    for tbl in ("statistics", "statistics_short_term"):
        cur.execute(f"SELECT state FROM {tbl} WHERE metadata_id=%s AND start_ts<%s "
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
    ap.add_argument("--whole-period", action="store_true",
                    help="window = from the period start")
    ap.add_argument("--this-month", action="store_true",
                    help="window = from the 1st of the current month")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    if not cfg["start_date"]:
        print("No configured period -> no current period."); return
    ps_ts = dt.datetime.fromisoformat(cfg["start_date"]).replace(tzinfo=TZ).timestamp()
    now = dt.datetime.now(TZ)
    rate_month = f"{now.year:04d}-{now.month:02d}"
    fuel_c, prod = fuel_and_prod(cfg, rate_month)
    # The offsets are always the readings at the PERIOD start (the bill accumulates
    # from there); only the window to write differs.
    if args.whole_period:
        lo = ps_ts
    elif args.this_month:
        lo = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    else:
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
        meta[suffix] = (mid, ar[0] if ar else None)

    # Source short-term buckets are already on the 5-minute grid (:00,:05,…).
    # Exact same calc + baselines as the integration:
    cons_series = [(int(ts), v, 0.0) for ts, v in cons]   # (grid_ts, reading, _)
    exp_series = [(int(ts), v, 0.0) for ts, v in sorted(exp_map.items())]
    points = billing.compute_bill_series(
        cons_series, exp_series, fuel_c, prod, cfg["tariff"], gbase=base_g, ebase=base_e
    )

    preview = [(ts, b.total, b.gross_kwh, b.net_kwh) for ts, b in points]

    # Idempotent UPSERT, batched per sensor: split grid points into those whose
    # exact aligned timestamp already exists (UPDATE in place — corrects stale/
    # spike points) vs new (INSERT). executemany keeps the whole-period run fast.
    INS = ("INSERT INTO states (metadata_id,state,attributes_id,last_updated_ts,"
           "last_changed_ts,last_reported_ts,origin_idx) VALUES (%s,%s,%s,%s,%s,%s,0)")
    UPD = ("UPDATE states SET state=%s, attributes_id=%s, last_changed_ts=%s, "
           "last_reported_ts=%s WHERE metadata_id=%s AND last_updated_ts=%s")
    upserted = 0
    for attr, suffix in MAP.items():
        if suffix not in meta:
            continue
        mid, aid = meta[suffix]
        cur.execute("SELECT last_updated_ts FROM states WHERE metadata_id=%s "
                    "AND last_updated_ts>=%s AND last_updated_ts<=%s", (mid, lo, hi))
        existing = {int(round(x[0])) for x in cur.fetchall() if x[0] is not None}
        ins, upd = [], []
        for ts, bill in points:
            val = f"{getattr(bill, attr):.4f}"
            if ts in existing:
                upd.append((val, aid, ts, ts, mid, ts))
            else:
                ins.append((mid, val, aid, ts, ts, ts))
        if not args.dry_run:
            if ins:
                cur.executemany(INS, ins)
            if upd:
                cur.executemany(UPD, upd)
        if suffix == "total":
            upserted = len(ins) + len(upd)

    print(f"\n{len(cons)} 5-min points. Sample (ts, total€, gross, net):")
    for ts, tot, g, n in preview[:3] + preview[-3:]:
        print(f"  {dt.datetime.fromtimestamp(ts, TZ):%m-%d %H:%M}  €{tot:7.2f}  g={g:7.2f} n={n:7.2f}")
    if args.dry_run:
        print(f"\nDRY-RUN — would upsert {upserted} grid points/sensor across {len(meta)} sensors.")
        conn.rollback()
    else:
        conn.commit()
        print(f"\nUPSERTED {upserted} grid points/sensor across {len(meta)} sensors.")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()

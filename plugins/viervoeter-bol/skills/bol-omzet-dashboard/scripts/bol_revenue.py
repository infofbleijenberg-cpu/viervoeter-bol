#!/usr/bin/env python3
"""Fetch bol.com revenue per day via the Retailer API (v10).

Credentials come ONLY from environment variables BOL_CLIENT_ID and
BOL_CLIENT_SECRET. Never put them in this repository (it is public).

Orders are listed per latest-change-date (one query per day from the start
date to today): without that filter bol only returns the last few days.

Revenue = sum((quantity - quantityCancelled) * unitPrice) per order item,
grouped by the day the order was placed (Europe/Amsterdam).
bol's unitPrice is the consumer price, so revenue is INCLUDING btw.
Returns are not subtracted.

Usage:
  python3 bol_revenue.py --days 14 --out OUT_DIR
  python3 bol_revenue.py --from 2026-07-10 --to 2026-10-08 --out OUT_DIR

Writes one JSON file per month: OUT_DIR/<YYYY-MM>.json
  {"month": "2026-10", "days": {"2026-10-01": {"revenue": 123.45, "orders": 3, "units": 4}, ...}}
Days in range without orders are written with zeros.
"""
import argparse
import base64
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Amsterdam")
API = "https://api.bol.com/retailer"
ACCEPT = "application/vnd.retailer.v10+json"


class Bol:
    def __init__(self, cid, secret):
        self.cid, self.secret = cid, secret
        self.token, self.exp = None, 0
        self.s = requests.Session()

    def _auth(self):
        if self.token and time.time() < self.exp - 30:
            return
        basic = base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode()
        r = self.s.post(
            "https://login.bol.com/token?grant_type=client_credentials",
            headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
            timeout=30,
        )
        if r.status_code != 200:
            sys.exit(f"bol login failed ({r.status_code}). Check BOL_CLIENT_ID / BOL_CLIENT_SECRET.")
        j = r.json()
        self.token, self.exp = j["access_token"], time.time() + int(j.get("expires_in", 299))

    def get(self, path, params=None):
        for attempt in range(8):
            self._auth()
            r = self.s.get(
                API + path,
                params=params,
                headers={"Authorization": f"Bearer {self.token}", "Accept": ACCEPT},
                timeout=60,
            )
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code == 401:
                self.token = None
                continue
            r.raise_for_status()
            return r.json() if r.content else {}
        sys.exit(f"bol API kept rate-limiting on {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="last N days incl. today")
    ap.add_argument("--from", dest="dfrom")
    ap.add_argument("--to", dest="dto")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    cid, sec = os.environ.get("BOL_CLIENT_ID"), os.environ.get("BOL_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit("BOL_CLIENT_ID and BOL_CLIENT_SECRET are not set in the environment.")

    today = datetime.now(TZ).date()
    if a.days:
        d_from, d_to = today - timedelta(days=a.days - 1), today
    else:
        d_from = date.fromisoformat(a.dfrom)
        d_to = date.fromisoformat(a.dto) if a.dto else today
    oldest = today - timedelta(days=89)
    if d_from < oldest:
        print(f"note: bol keeps orders ~3 months; clipping start to {oldest}", file=sys.stderr)
        d_from = oldest

    bol = Bol(cid, sec)

    # 1. List all orders (all statuses, all fulfilment methods), filter on placed date.
    # Without latest-change-date bol only returns recent orders, so query
    # every change date from d_from to today and de-duplicate on orderId.
    wanted, seen = [], set()
    cd = d_from
    while cd <= today:
        page = 1
        while True:
            j = bol.get("/orders", {"page": page, "fulfilment-method": "ALL", "status": "ALL",
                                    "latest-change-date": cd.isoformat()})
            orders = j.get("orders", [])
            if not orders:
                break
            for o in orders:
                if o["orderId"] in seen:
                    continue
                seen.add(o["orderId"])
                placed = datetime.fromisoformat(o["orderPlacedDateTime"].replace("Z", "+00:00")).astimezone(TZ).date()
                if d_from <= placed <= d_to:
                    wanted.append((o["orderId"], placed, o.get("orderItems", [])))
            page += 1
            if page > 2000:
                break
        print(f"change-date {cd}: {len(seen)} orders seen", file=sys.stderr, flush=True)
        cd += timedelta(days=1)

    # 2. Revenue per order (use unitPrice from list if present, else order detail).
    days = defaultdict(lambda: {"revenue": 0.0, "orders": 0, "units": 0})
    for n_, (oid, placed, items) in enumerate(wanted):
        if n_ % 50 == 0: print(f"detail {n_}/{len(wanted)}", file=sys.stderr, flush=True)
        if not items or any("unitPrice" not in it for it in items):
            items = bol.get(f"/orders/{oid}").get("orderItems", [])
        rev, units = 0.0, 0
        for it in items:
            q = int(it.get("quantity", 0)) - int(it.get("quantityCancelled", 0) or 0)
            if q <= 0:
                continue
            rev += q * float(it.get("unitPrice", 0))
            units += q
        if units:
            k = placed.isoformat()
            days[k]["revenue"] += rev
            days[k]["orders"] += 1
            days[k]["units"] += units

    # 3. Write month files, zero-filling empty days.
    months = defaultdict(dict)
    d = d_from
    while d <= d_to:
        v = days.get(d.isoformat(), {"revenue": 0.0, "orders": 0, "units": 0})
        months[d.strftime("%Y-%m")][d.isoformat()] = {
            "revenue": round(v["revenue"], 2), "orders": v["orders"], "units": v["units"]}
        d += timedelta(days=1)
    os.makedirs(a.out, exist_ok=True)
    for m, dd in sorted(months.items()):
        with open(os.path.join(a.out, f"{m}.json"), "w") as f:
            json.dump({"month": m, "days": dd}, f, indent=1)
    total = sum(v["revenue"] for dd in months.values() for v in dd.values())
    print(json.dumps({"from": d_from.isoformat(), "to": d_to.isoformat(),
                      "orders_found": len(wanted), "revenue_incl_btw": round(total, 2),
                      "months": sorted(months)}))


if __name__ == "__main__":
    main()

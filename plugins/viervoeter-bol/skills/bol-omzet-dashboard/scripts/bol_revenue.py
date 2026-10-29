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

Optional, per product (EAN):
  --products-out DIR   also write DIR/<YYYY-MM>.json
      {"month": "2026-10", "days": {"2026-10-01": {"<EAN>": {"bolRev": 49.99, "bolUnits": 1}, ...}}}
      Same order items and same rule as the day totals, counted in whole cents,
      so per day the sum of bolRev is exactly the day revenue above.
  --products-base DIR  existing product docs (JSON files <YYYY-MM>.json, also in
      sub-folders, e.g. an ArtifactData out_dir). EANs that had bol sales on a day
      in the window but have none now (e.g. cancelled later) get
      {"bolRev": {"__delete__": true}, "bolUnits": {"__delete__": true}}, so an
      ArtifactData `update` with the output file keeps the day exactly in sync.
      Shopify fields (shopRev/shopUnits) are never touched.
  The JSON summary on stdout then also has products_check ("ok" or the days
  where the per-EAN sum differs), products_eans and products_titles
  ({EAN: bol product title}).
"""
import argparse
import base64
import glob
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
    ap.add_argument("--products-out", dest="pout")
    ap.add_argument("--products-base", dest="pbase")
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
    # Amounts are summed in whole cents so per-EAN figures add up exactly.
    days = defaultdict(lambda: {"cents": 0, "orders": 0, "units": 0})
    prods = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # day -> ean -> [cents, units]
    titles = {}  # ean -> bol product title (from the order detail)
    for n_, (oid, placed, items) in enumerate(wanted):
        if n_ % 50 == 0: print(f"detail {n_}/{len(wanted)}", file=sys.stderr, flush=True)
        if not items or any("unitPrice" not in it for it in items) or \
                (a.pout and any(not (it.get("ean") or (it.get("product") or {}).get("ean")) for it in items)):
            items = bol.get(f"/orders/{oid}").get("orderItems", [])
        cents, units = 0, 0
        k = placed.isoformat()
        for it in items:
            q = int(it.get("quantity", 0)) - int(it.get("quantityCancelled", 0) or 0)
            if q <= 0:
                continue
            c = q * round(float(it.get("unitPrice", 0)) * 100)
            cents += c
            units += q
            ean = str(it.get("ean") or (it.get("product") or {}).get("ean") or "onbekend")
            t = (it.get("product") or {}).get("title") or it.get("title")
            if t:
                titles[ean] = t
            prods[k][ean][0] += c
            prods[k][ean][1] += q
        if units:
            days[k]["cents"] += cents
            days[k]["orders"] += 1
            days[k]["units"] += units

    # 3. Write month files, zero-filling empty days.
    months = defaultdict(dict)
    d = d_from
    while d <= d_to:
        v = days.get(d.isoformat(), {"cents": 0, "orders": 0, "units": 0})
        months[d.strftime("%Y-%m")][d.isoformat()] = {
            "revenue": v["cents"] / 100, "orders": v["orders"], "units": v["units"]}
        d += timedelta(days=1)
    os.makedirs(a.out, exist_ok=True)
    for m, dd in sorted(months.items()):
        with open(os.path.join(a.out, f"{m}.json"), "w") as f:
            json.dump({"month": m, "days": dd}, f, indent=1)
    total = sum(v["revenue"] for dd in months.values() for v in dd.values())
    extra = {}
    if a.pout:
        extra = write_products(a, prods, months, d_from, d_to)
        extra["products_titles"] = {e: titles.get(e, "") for e in extra["products_eans"]}
    print(json.dumps({**extra, "from": d_from.isoformat(), "to": d_to.isoformat(),
                      "orders_found": len(wanted), "revenue_incl_btw": round(total, 2),
                      "months": sorted(months)}))


def write_products(a, prods, months, d_from, d_to):
    """Write per-EAN bol figures per day; returns summary fields."""
    base = {}  # day -> set of EANs with bol fields in the existing docs
    if a.pbase:
        for f in glob.glob(os.path.join(a.pbase, "**", "*.json"), recursive=True):
            try:
                doc = json.load(open(f))
            except Exception:
                continue
            doc = doc.get("data", doc) if isinstance(doc, dict) else {}
            for day, eans in (doc.get("days") or {}).items():
                for ean, v in (eans or {}).items():
                    if isinstance(v, dict) and ("bolRev" in v or "bolUnits" in v):
                        base.setdefault(day, set()).add(ean)
    out = defaultdict(dict)
    removed, mismatch, eans_seen = 0, [], set()
    d = d_from
    while d <= d_to:
        k = d.isoformat()
        day = {}
        for ean, (c, q) in sorted(prods.get(k, {}).items()):
            day[ean] = {"bolRev": c / 100, "bolUnits": q}
            eans_seen.add(ean)
        for ean in sorted(base.get(k, set()) - set(day)):
            day[ean] = {"bolRev": {"__delete__": True}, "bolUnits": {"__delete__": True}}
            removed += 1
        tot = sum(round(v["bolRev"] * 100) for v in day.values() if isinstance(v["bolRev"], (int, float)))
        if tot != round(months[d.strftime("%Y-%m")][k]["revenue"] * 100):
            mismatch.append(k)
        out[d.strftime("%Y-%m")][k] = day
        d += timedelta(days=1)
    os.makedirs(a.pout, exist_ok=True)
    for m, dd in sorted(out.items()):
        with open(os.path.join(a.pout, f"{m}.json"), "w") as f:
            json.dump({"month": m, "days": dd}, f, indent=1)
    return {"products_months": sorted(out), "products_eans": sorted(eans_seen),
            "products_removed": removed, "products_check": "ok" if not mismatch else mismatch}


if __name__ == "__main__":
    main()

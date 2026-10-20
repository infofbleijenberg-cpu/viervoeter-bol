#!/usr/bin/env python3
"""Fetch ALL bol.com returns (every product) via the Retailer API (v10).

Credentials come ONLY from environment variables BOL_CLIENT_ID and
BOL_CLIENT_SECRET. Never print them or write them to a file.

Usage:
  python3 bol_returns.py --days 62 --out /tmp/bol_returns

Writes one JSON file per month: OUT/<YYYY-MM>.json
  {"retouren": [{"datum": "2026-10-19", "ean": "...", "titel": "...", "aantal": 1,
                 "reden": "...", "afgehandeld": false, "orderId": "...", "returnId": "...",
                 "fulfilment": "FBR", "prijs": 279.0, "bedrag": 279.0}, ...],
   "retourenBijgewerkt": "<ISO>"}
Write each file to the dashboard with ArtifactData `update` on collection
`bolretour`, doc id = month. `update` merges, so the existing fields
(dagen, modellen, open, redenen, totaal) stay untouched.
datum = registration date at bol (Europe/Amsterdam).
prijs = unitPrice incl. btw from the original bol order, bedrag = prijs x aantal
When bol no longer serves the order (older than about 3 months), prijs is the
median price of that EAN in the other returns and prijsGeschat is true; null
when that EAN has no known price at all.
"""
import argparse
import base64
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Amsterdam")
API = "https://api.bol.com/retailer"
ACCEPT = "application/vnd.retailer.v10+json"
MAX_PAGES = 60


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
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            return r.json() if r.content else {}
        sys.exit(f"bol API kept rate-limiting on {path}")


def local_date(ts):
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(TZ).date().isoformat()


def reason_text(item):
    r = item.get("returnReason") or {}
    main, det = r.get("mainReason"), r.get("detailedReason")
    if main and det and det != main:
        return f"{main} – {det}"
    return main or det or "Geen reden"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=62)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    cid, sec = os.environ.get("BOL_CLIENT_ID"), os.environ.get("BOL_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit("BOL_CLIENT_ID / BOL_CLIENT_SECRET not set in this environment.")
    bol = Bol(cid, sec)
    # Start at the first day of the oldest month, so every month file is complete.
    cutoff = (datetime.now(TZ).date() - timedelta(days=a.days - 1)).replace(day=1).isoformat()

    seen, rows = set(), []
    for method in ("FBR", "FBB"):
        for handled in ("false", "true"):
            for page in range(1, MAX_PAGES + 1):
                j = bol.get("/returns", {"page": page, "handled": handled, "fulfilment-method": method})
                rets = j.get("returns") or []
                if not rets:
                    break
                any_recent = False
                for ret in rets:
                    d = local_date(ret.get("registrationDateTime"))
                    if not d or d < cutoff:
                        continue
                    any_recent = True
                    for it in ret.get("returnItems") or []:
                        key = (ret.get("returnId"), it.get("rmaId"), it.get("ean"))
                        if key in seen:
                            continue
                        seen.add(key)
                        rows.append({
                            "datum": d,
                            "ean": it.get("ean") or "",
                            "titel": it.get("title") or "",
                            "aantal": int(it.get("expectedQuantity") or 1),
                            "reden": reason_text(it),
                            "afgehandeld": bool(it.get("handled")) or handled == "true",
                            "orderId": it.get("orderId") or "",
                            "returnId": str(ret.get("returnId") or ""),
                            "fulfilment": method,
                        })
                if not any_recent:
                    break

    # Price per returned item: unitPrice (incl. btw) of that EAN in the original order.
    prices = {}
    for oid in sorted({r["orderId"] for r in rows if r["orderId"]}):
        for oi in bol.get(f"/orders/{oid}").get("orderItems") or []:
            ean = (oi.get("product") or {}).get("ean") or oi.get("ean")
            if ean and oi.get("unitPrice") is not None:
                prices[(oid, ean)] = float(oi["unitPrice"])
    # bol no longer serves orders older than about 3 months. For those, use the
    # median price of the same EAN in the orders that could be read (prijsGeschat).
    by_ean = defaultdict(list)
    for (oid, ean), p in prices.items():
        by_ean[ean].append(p)
    for r in rows:
        p, est = prices.get((r["orderId"], r["ean"])), False
        if p is None and by_ean.get(r["ean"]):
            ps = sorted(by_ean[r["ean"]])
            p, est = ps[len(ps) // 2], True
        r["prijs"] = p
        r["prijsGeschat"] = est
        r["bedrag"] = round(p * r["aantal"], 2) if p is not None else None

    by_month = defaultdict(list)
    for r in sorted(rows, key=lambda x: (x["datum"], x["returnId"]), reverse=True):
        by_month[r["datum"][:7]].append(r)

    # Make sure months in range without returns get an empty list too.
    d = datetime.fromisoformat(cutoff).date()
    end = datetime.now(TZ).date()
    while d <= end:
        by_month.setdefault(d.isoformat()[:7], [])
        d += timedelta(days=1)

    os.makedirs(a.out, exist_ok=True)
    now = datetime.now(TZ).isoformat(timespec="seconds")
    for m, items in sorted(by_month.items()):
        with open(os.path.join(a.out, f"{m}.json"), "w") as f:
            json.dump({"retouren": items, "retourenBijgewerkt": now}, f, ensure_ascii=False)

    print(json.dumps({
        "from": cutoff,
        "returns": len(rows),
        "open": sum(1 for r in rows if not r["afgehandeld"]),
        "bedrag": round(sum(r["bedrag"] or 0 for r in rows), 2),
        "zonderPrijs": sum(1 for r in rows if r["bedrag"] is None),
        "months": {m: len(v) for m, v in sorted(by_month.items())},
    }))


if __name__ == "__main__":
    main()

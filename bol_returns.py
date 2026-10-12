#!/usr/bin/env python3
"""Count bol.com returns of the 4 Viervoeter Fresh litter boxes via the Retailer API (v10).

Credentials come ONLY from environment variables BOL_CLIENT_ID and
BOL_CLIENT_SECRET. Never put them in this repository (it is public).

Lists all returns (handled and unhandled, FBR and FBB), keeps the ones for the
four Fresh litter boxes, and groups them per registration day (Europe/Amsterdam).

Usage:
  python3 bol_returns.py --from 2026-09-01 --out OUT_DIR

Writes one JSON file per month from --from up to the current month:
  OUT_DIR/<YYYY-MM>.json
  {"maand": "2026-10", "bijgewerkt": "...+02:00", "totaal": 4,
   "modellen": {"goud": 1, "silver": 1, "fresh2": 0, "drinkfontein": 2},
   "dagen": {"2026-10-02": {"drinkfontein": 1}, ...},
   "open": 4, "redenen": {"Geen reden": 3, ...}}
"open" = all unhandled Fresh litter-box returns right now (same in every month file).
Prints a JSON summary on stdout when done; progress goes to stderr.
"""
import argparse
import base64
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Amsterdam")
API = "https://api.bol.com/retailer"
ACCEPT = "application/vnd.retailer.v10+json"

# EAN of the new (A-quality) product on bol -> model key used by the dashboard
MODELS = {
    "8720726296588": "goud",          # Fresh 3 goud/brons
    "8720726296823": "silver",        # Fresh 3 Silver
    "8720726296465": "fresh2",        # Fresh 2
    "8720726296656": "drinkfontein",  # Fresh incl. drinkfontein
}
KEYS = ["goud", "silver", "fresh2", "drinkfontein"]
MAX_PAGES = 200


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


def local_day(ts):
    """ISO timestamp from bol -> YYYY-MM-DD in Europe/Amsterdam."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).date().isoformat()


def fetch_returns(bol, d_from):
    """All return items for the Fresh models; stops paging once a page is entirely older than d_from."""
    items, seen = [], set()
    for handled in ("false", "true"):
        for method in ("FBR", "FBB"):
            for page in range(1, MAX_PAGES + 1):
                j = bol.get("/returns", {"page": page, "handled": handled, "fulfilment-method": method})
                rets = j.get("returns") or []
                if not rets:
                    break
                newest = ""
                for ret in rets:
                    ts = ret.get("registrationDateTime") or ""
                    day = local_day(ts) if ts else ""
                    newest = max(newest, day)
                    for it in ret.get("returnItems") or []:
                        model = MODELS.get(str(it.get("ean", "")))
                        if not model:
                            continue
                        key = it.get("rmaId") or f'{ret.get("returnId")}-{it.get("orderId")}-{it.get("ean")}'
                        if key in seen:
                            continue
                        seen.add(key)
                        reason = (it.get("returnReason") or {}).get("mainReason") or "Geen reden"
                        items.append({
                            "day": day,
                            "model": model,
                            "qty": int(it.get("expectedQuantity") or 1),
                            "handled": bool(it.get("handled", handled == "true")),
                            "reason": reason,
                        })
                print(f"handled={handled} {method} page {page}: {len(rets)} returns, {len(items)} Fresh items so far",
                      file=sys.stderr, flush=True)
                if newest and newest < d_from.isoformat():
                    break
    return items


def month_iter(d_from, d_to):
    y, m = d_from.year, d_from.month
    while (y, m) <= (d_to.year, d_to.month):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", required=True, help="first day, e.g. 2026-09-01")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    cid, sec = os.environ.get("BOL_CLIENT_ID"), os.environ.get("BOL_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit("BOL_CLIENT_ID and BOL_CLIENT_SECRET are not set in the environment.")

    d_from = date.fromisoformat(a.dfrom)
    now = datetime.now(TZ)
    items = fetch_returns(Bol(cid, sec), d_from)

    open_now = sum(i["qty"] for i in items if not i["handled"])
    docs = {}
    for ym in month_iter(d_from, now.date()):
        docs[ym] = {
            "maand": ym,
            "bijgewerkt": now.replace(microsecond=0).isoformat(),
            "totaal": 0,
            "modellen": {k: 0 for k in KEYS},
            "dagen": defaultdict(lambda: defaultdict(int)),
            "open": open_now,
            "redenen": defaultdict(int),
        }
    for i in items:
        if not i["day"] or i["day"] < d_from.isoformat():
            continue
        doc = docs.get(i["day"][:7])
        if not doc:
            continue
        doc["totaal"] += i["qty"]
        doc["modellen"][i["model"]] += i["qty"]
        doc["dagen"][i["day"]][i["model"]] += i["qty"]
        doc["redenen"][i["reason"]] += i["qty"]

    os.makedirs(a.out, exist_ok=True)
    summary = {}
    for ym, doc in docs.items():
        doc["dagen"] = {d: dict(v) for d, v in sorted(doc["dagen"].items())}
        doc["redenen"] = dict(doc["redenen"])
        with open(os.path.join(a.out, f"{ym}.json"), "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        summary[ym] = {"totaal": doc["totaal"], "modellen": doc["modellen"]}
    print(json.dumps({"ok": True, "open": open_now, "months": summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()

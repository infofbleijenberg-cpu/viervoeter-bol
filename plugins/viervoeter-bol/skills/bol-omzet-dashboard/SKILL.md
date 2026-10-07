---
name: bol-omzet-dashboard
description: Refresh Freek's live bol.com revenue dashboard (Viervoeter) from the bol Retailer API. Use for "bol omzet", "update bol dashboard", "ververs bol omzet" and the scheduled weekday 08:45 run.
---

# bol omzet dashboard

Fills the live dashboard https://claude.ai/artifact/SA6J8aKi3PT3R4fj9mmSht with bol.com revenue per day (incl. 21% btw), so it shows yesterday, the last 7 days and the last 2 months (60 days).

## Data model (artifact database)

- Collection `months`, one doc per month, id `YYYY-MM`:
  `{"month": "2026-10", "days": {"2026-10-01": {"revenue": 123.45, "orders": 3, "units": 4}, ...}}`
- Doc `meta/status`: `{"lastRun": "<ISO timestamp>", "source": "bol Retailer API v10", "note": "..."}`

## Steps

1. Check that `BOL_CLIENT_ID` and `BOL_CLIENT_SECRET` exist in the environment (`test -n "$BOL_CLIENT_ID"`). Never print, echo or log their values, and never write them to any file or to this public repo. If missing: stop and tell Freek the bol API credentials are not set in this environment.
2. Run the script (it lives next to this file):
   ```
   python3 <skill dir>/scripts/bol_revenue.py --days 14 --out /tmp/bol_out
   ```
   14 days so late cancellations on recent orders get corrected. For the first run or after a gap, use `--days 62`.
   The script prints a JSON summary and writes `/tmp/bol_out/<YYYY-MM>.json` per month.
3. Read the existing month docs: `ArtifactData` `list` on collection `months` (url above). Note each doc's `version`.
4. Write one `ArtifactData` `batch`:
   - for each month file: if the doc exists → `op: "update"` with `file_path` of that month file and `if_version` (update merges the `days` map, so older days stay); if not → `op: "set"` with the `file_path`, no `if_version`.
   - `meta/status`: `set` (or `update` with its version if it exists) `{"lastRun": <now ISO>, "source": "bol Retailer API v10"}`.
   If a pinned write fails on a version conflict, re-list and redo it once.
5. Report in one or two lines: yesterday's revenue, last 7 days total, and the dashboard link. All amounts incl. btw (bol unitPrice already includes btw; do NOT multiply by 1.21).

## Notes

- Revenue = unitPrice × (quantity − quantityCancelled) per order item, grouped by order date in Europe/Amsterdam. Returns are not subtracted.
- The script handles 429 rate limits itself. A login failure means wrong or expired credentials.

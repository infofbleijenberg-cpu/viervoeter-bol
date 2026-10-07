---
name: bol-omzet-dashboard
description: Refresh Freek's live revenue dashboard (Viervoeter, bol.com + Shopify webshop) from the bol Retailer API and Shopify analytics. Use for "bol omzet", "update bol dashboard", "ververs bol omzet", "omzet dashboard" and the scheduled weekday 09:30 run.
---

# Omzet dashboard (bol.com + Shopify)

Fills the live dashboard https://claude.ai/artifact/SA6J8aKi3PT3R4fj9mmSht with revenue per day (incl. 21% btw) from bol.com and the Shopify webshop. The page shows 7 days, the last month (30 days) and the last 2 months (60 days).

## Data model (artifact database)

- Collection `months` (bol.com), one doc per month, id `YYYY-MM`:
  `{"month": "2026-10", "days": {"2026-10-01": {"revenue": 123.45, "orders": 3, "units": 4}, ...}}`
- Collection `shopify`, same shape without `units`:
  `{"month": "2026-10", "days": {"2026-10-01": {"revenue": 2145.23, "orders": 26}, ...}}`
- Doc `meta/status`: `{"lastRun": "<ISO timestamp>", "source": "bol Retailer API v10 + Shopify", "note": "..."}`

`update` deep-merges the `days` map, so writing 14 days keeps the older days of the month.

## Steps

1. Check that `BOL_CLIENT_ID` and `BOL_CLIENT_SECRET` exist in the environment (`test -n "$BOL_CLIENT_ID"`). Never print, echo or log their values, and never write them to any file or to this public repo. If missing: stop and tell Freek the bol API credentials are not set in this environment.
2. Run the script (it lives next to this file) in the background, it takes a few minutes:
   ```
   nohup python3 <skill dir>/scripts/bol_revenue.py --days 14 --out /tmp/bol_out > /tmp/bol.out 2> /tmp/bol.err &
   ```
   Poll `/tmp/bol.out` until it holds the JSON summary (progress goes to `/tmp/bol.err`).
   14 days so late cancellations on recent orders get corrected. For the first run or after a gap, use `--days 62` (about 10 minutes).
   The script writes `/tmp/bol_out/<YYYY-MM>.json` per month.
3. Shopify: run the Shopify connector's `run-analytics-query` with
   `FROM sales SHOW orders, total_sales TIMESERIES day SINCE -14d UNTIL yesterday`
   and write `/tmp/shop_out/<YYYY-MM>.json` per month in the shape above (`revenue` = total_sales as a number, `orders` as int). total_sales is already incl. btw, after discounts and returns.
4. Read the existing docs: `ArtifactData` `list` on `months` and on `shopify`, and `get` `meta/status`. Note each `version`.
5. Write one `ArtifactData` `batch`:
   - for each month file of both sources: if the doc exists → `op: "update"` with `file_path` and `if_version`; if not → `op: "set"` with `file_path`, no `if_version`.
   - `meta/status`: `update` (with its version) or `set` `{"lastRun": <now ISO>, "source": "bol Retailer API v10 + Shopify"}`.
   If a pinned write fails on a version conflict, re-list and redo it once.
6. Report in one or two lines in Dutch: omzet gisteren and laatste 7 dagen for bol, Shopify and total, plus the dashboard link. All amounts incl. btw (bol unitPrice already includes btw; do NOT multiply by 1.21).

## Notes

- bol revenue = unitPrice × (quantity − quantityCancelled) per order item, grouped by order date in Europe/Amsterdam. Returns are not subtracted.
- bol only returns recent orders unless `latest-change-date` is passed, so the script queries one change date at a time and de-duplicates on orderId. bol keeps about 3 months of orders.
- The script handles 429 rate limits itself. A login failure means wrong or expired credentials.
- If the Shopify connector is not available, still write the bol data and say Shopify was skipped.

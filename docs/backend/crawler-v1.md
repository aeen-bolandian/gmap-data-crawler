# Google Maps Review Crawler — v1.0.0

**Developer:** aeen | **Date:** 2026-10-22 | **Status:** Production-ready (68% coverage)

## Overview

A modular, fault-tolerant pipeline that extracts Google Maps reviews for
cafes stored in `cafes_cafe.db`. Phase 1 builds a Google Maps search URL
per cafe; Phase 2 opens it in a CDP-attached Chrome, navigates to the
correct place, and streams reviews to an append-only CSV.

**Final v1.0.0 result:** 558 / 823 approved cafes (68%), 3,711 reviews.

## Prerequisites — READ BEFORE RUNNING

The crawler attaches to a manually started Chrome debug session. Without
this setup, Google Maps opens tabs with a blank page or "Limited View"
search results, and the crawl produces zero rows.

**Setup steps (in order):**

1. Close all regular Chrome windows.

2. Launch Chrome in debug mode:

   Linux / macOS:
   `google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug`

   Windows:
   `"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-debug-crawler"`

3. Log in to a Google account in this Chrome window. Google Maps serves
   "Limited View" search results to anonymous visitors. The session
   persists in the `--user-data-dir` profile.

4. Open exactly N Maps tabs, where N = the `--workers` value (default 4).
   Each tab must fully load `https://www.google.com/maps` — the map must
   be visible, not blank.

   Why this matters: in a CDP session, `context.new_page()` produces tabs
   that do NOT inherit the debug session and often load blank. The pool of
   user-opened Maps tabs is the only reliable way to keep the
   authenticated session alive.

5. Leave Chrome running for the entire crawl. Do not close the tabs.

6. Run the crawler: `python run.py --db cafes_cafe.db --workers 4 --log-level INFO`

Warning: `--workers` must equal the number of Maps tabs you opened in
step 4. If you pass `--workers 4` but only opened 2 tabs, the other 2
workers will create fresh (blank) tabs and their cafes will fail.

## Rate-limit awareness

Google's rate limiter kicks in after ~50 cafes if requests are too
frequent. Default `MIN_DELAY = 3.0` and `MAX_DELAY = 8.0` seconds per
worker were tuned to survive ~800 cafes on a single IP.

Signs of rate limiting: `No result matched within 300m` suddenly appears
for many cafes; success rate drops below 50%; Chrome tabs redirect to
`https://www.google.com/sorry/...`.

Recovery: stop the crawl (Ctrl+C; state is preserved), close affected
tabs, open fresh Maps tabs, wait 30-60 minutes, resume with
`python run.py ...` — state picks up where it left off.

## Components

| File | Purpose |
|---|---|
| `src/db_loader.py` | Read-only SQLite access. Loads approved cafes with `search_address` as an optional field. |
| `src/resolver.py` | Builds Google Maps search URLs. Prefers `search_address` (shortened to last 3 meaningful parts) over `neighborhood + city`. |
| `src/scraper.py` | Playwright engine. Owns CDP connection, builds a pool of existing Maps tabs, exposes `ScraperWorker` for per-tab scraping. |
| `src/exporter.py` | Append-only CSV writer (UTF-8 with BOM for Excel). |
| `run.py` | CLI entrypoint. Spawns N concurrent workers sharing an `asyncio.Queue`. |

## Pipeline

DatabaseLoader reads approved cafes → LocationResolver builds the search
URL (name + short search_address) → ConcurrentPipeline fans out to N
ScraperWorkers, each claiming a tab from the pool → ScraperWorker goes to
the URL, detects the page type (place page / results list / feed), picks
the first result whose href coordinates are within 300 m of the cafe,
navigates to it, clicks Reviews, scrolls event-driven, expands "More"
buttons in a single JS pass, and extracts 10-150 reviews →
CSVExporter appends rows atomically.

## Validations — why wrong-cafe reviews never reach the CSV

Three checks gate every extraction. Failure in any → skip and log.

**1. Coordinate match.** After navigating to the target place, extract
`/@LAT,LNG` (or `!3dLAT!4dLNG`) from the actual URL via
`window.location.href`. Compare with `cafe.latitude/longitude` using
haversine. Require distance ≤ `MAX_MATCH_DISTANCE_M` (300 m).

Note: `page.url` is unreliable because Google Maps updates the address
bar via the History API without a full navigation. `window.location.href`
is always current.

**2. Clicked-result coordinates.** When navigating from a `/maps/search`
results list, the result's `href` carries true place coordinates in its
`!3d...!4d...` payload. Those are trusted over the page URL.

**3. Name fallback.** Only when no coordinates are available (rare):
compare normalized names (lowercase, Arabic→Persian characters, stripped
"cafe/restaurant" tokens, minimum 3 chars). Used for places whose URL
lacks `/@lat,lng` and `!3d/!4d`.

If any check fails, the cafe is not checkpointed and will be retried on
the next run. This is why the crawler never stores reviews for the wrong
cafe.

## Checkpointing

`crawler_state.json` stores the set of processed `cafe_id`s. Writes are
atomic (`os.replace` over a temp file) and batched every 10 checkpoints,
protected by an `asyncio.Lock` shared across workers.

Resuming is automatic: `run.py` filters `iter_cafes()` by
`state.is_processed()`, so restarted crawls skip successfully-processed
cafes. Skipped cafes (validation failures, zero reviews) are retried.

To repair a desynchronized state (e.g. after a crash mid-flush):

```python
import csv, json
with open("data/google_reviews.csv", encoding="utf-8-sig") as f:
    csv_ids = {int(r["cafe_id"]) for r in csv.DictReader(f) if r.get("cafe_id")}
with open("crawler_state.json") as f:
    state = json.load(f)
merged = set(state["processed_ids"]) | csv_ids
with open("crawler_state.json", "w") as f:
    json.dump({"processed_ids": sorted(merged)}, f, indent=2)
```

## CLI

```bash
python run.py --db cafes_cafe.db --workers 4 --log-level INFO
python run.py --limit 100 --workers 2 --headful       # debug
python run.py --cdp-url http://localhost:9222         # custom CDP port
```

| Flag | Default | Notes |
|---|---|---|
| `--db` | `cafes_cafe.db` | SQLite path (read-only) |
| `--output` | `data/google_reviews.csv` | Append-only |
| `--state` | `crawler_state.json` | Checkpoint file |
| `--limit` | unlimited | Max cafes this run |
| `--workers` | 4 | Must equal the number of Maps tabs you opened |
| `--headful` | False | Only used if CDP fails |
| `--cdp-url` | `http://localhost:9222` | Chrome debug endpoint |
| `--log-level` | INFO | DEBUG is very verbose |

## Output schema

`data/google_reviews.csv` columns:

| Column | Example |
|---|---|
| `cafe_id` | `188` |
| `cafe_name` | `کافه رستوران گُجا (مهرآباد)` |
| `city` | `تهران` |
| `author` | `Ali S. M` |
| `rating` | `5 stars` (from `aria-label`) |
| `date` | `2 months ago` |
| `text` | full review body (newlines collapsed) |

UTF-8 with BOM, append-only, atomic header on first write.

## Known limitations (v1.0.0)

**68% success rate.** 558 / 823 approved cafes extracted. The remaining
265 have name-duplicate or ambiguous queries — e.g. "کافه آرامش" appears
5 times in the DB with 5 different Google Maps entries, so the search URL
lands on a results list and the coordinate filter cannot disambiguate.
Solving these requires Google Places API or residential proxy support.

**Rate limiting.** Running more than 2-3 batches of 100 cafes in a row
triggers Google's "sorry" page (captcha). Wait 30-60 min between batches.
The 3-8 s inter-cafe delay helps but does not eliminate the risk.

**CDP tabs must be user-opened.** Tabs created via `context.new_page()`
in a CDP session load blank. This is a Chrome/Playwright limitation, not
something we can fix in the crawler. `ScraperWorker` claims tabs from an
`asyncio.Queue` populated at startup with the user's existing Maps tabs,
and only falls back to `new_page()` (with a loud warning) when the pool
is empty.

**Progress bar is cosmetic.** `progress.update(1)` is called after
`_process_one` returns, but `_process_one` can return early on
exceptions, so the bar may not reach 100% even after the crawl completes.
The `state` file is the ground truth.

## Phase 2 (future work)

1. **Google Places API integration** for the 265 skipped cafes. Cost
   ~$15 for 823 requests. Eliminates both the 68% ceiling and the rate
   limiter, because the API returns the exact place by `place_id` with no
   ambiguity.
2. **Residential proxy support** for crawls larger than 1,000 cafes.
   Rotate IPs across workers so the rate limiter never sees more than a
   handful of requests from any single address.
3. **Dashboard** for the 3,711 collected reviews (rating distribution,
   time-series, per-neighborhood sentiment).
4. **Manual override table** for the 265 skipped cafes, so an operator
   can paste the correct Google Maps URL once and re-run.

## Performance reference (v1.0.0 measurements)

| Configuration | Success | Time (823 cafes) |
|---|---|---|
| Serial, no fixes | ~12% | ~4-5 hours |
| 4 workers, 0.5-1.5 s delay | 78% first run → 54% third run | ~1 h |
| 4 workers, 3-8 s delay | 68% | ~1 h 2 min |

The 3-8 s delay is the sweet spot: long enough to avoid the rate limiter,
short enough to keep the crawl under 90 minutes for the full database.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| All tabs load blank | Forgot to open N Maps tabs before running | Open N tabs, restart crawl |
| `Page pool empty` warning | Fewer tabs than `--workers` | Open more tabs or lower `--workers` |
| Success rate drops to <50% mid-run | Rate limiter | Stop (Ctrl+C), wait 30-60 min, resume |
| `google.com/sorry` in tab URL | Captcha triggered | Solve manually in Chrome, wait 15 min |
| `No result matched within 300m` for many cafes | Wrong `search_address`, or place absent from Maps | Check the cafe in Google Maps manually |
| CSV has duplicate `cafe_id` | State desync | Run the repair snippet above |

## Contact / ownership

Maintained by the Data Engineering Team. For issues with the 265 skipped
cafes or requests to raise the success rate, file a ticket referencing
this document.
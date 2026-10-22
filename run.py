"""
run.py
======
Main CLI entrypoint for the gmaps-review-crawler pipeline.

Orchestrates:
    1. Loading state (processed IDs) from crawler_state.json
    2. Iterating over unresolved cafes from SQLite
    3. Running Phase 1 (resolver) + Phase 2 (scraper) concurrently
    4. Streaming output via CSVExporter (thread-safe, append-only)
    5. Atomically checkpointing state after each successful cafe

IMPORTANT
---------
Before running with --workers N, open N Maps tabs in the debug
Chrome (e.g. `google.com/maps`). In a CDP session, tabs created via
Playwright do NOT inherit the debug session and often load blank.

Usage
-----
$ python run.py --db cafes_cafe.db --output data/google_reviews.csv
$ python run.py --limit 50 --workers 4 --headful
$ python run.py --limit 2 --cdp-url http://localhost:9222
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path
from typing import Optional

from loguru import logger
from tqdm.asyncio import tqdm_asyncio

from src.db_loader import DatabaseLoader, CafeRecord
from src.exporter import CSVExporter
from src.resolver import LocationResolver, ResolvedTarget
from src.scraper import GoogleMapsScraper, ScraperWorker


# ---------------------------------------------------------------------------
# State Manager (Checkpointing, concurrency-safe)
# ---------------------------------------------------------------------------
class StateManager:
    """
    Persists processed cafe IDs for crash-safe resumption.

    Thread-safe across asyncio workers via asyncio.Lock. Writes are
    batched (FLUSH_EVERY) so we don't rewrite the JSON file 800 times
    for 800 cafes.
    """

    FLUSH_EVERY = 10

    def __init__(
        self, state_path: str | Path = "crawler_state.json"
    ) -> None:
        self.state_path = Path(state_path)
        self.processed: set[int] = self._load()
        self._dirty = 0
        self._lock = asyncio.Lock()

    def _load(self) -> set[int]:
        if not self.state_path.exists():
            return set()
        try:
            data = json.loads(
                self.state_path.read_text(encoding="utf-8")
            )
            return set(data.get("processed_ids", []))
        except json.JSONDecodeError:
            logger.warning("Corrupt state file — starting fresh.")
            return set()

    async def mark_processed(self, cafe_id: int) -> None:
        async with self._lock:
            self.processed.add(cafe_id)
            self._dirty += 1
            if self._dirty >= self.FLUSH_EVERY:
                self._save()
                self._dirty = 0

    async def flush(self) -> None:
        async with self._lock:
            if self._dirty > 0 or not self.state_path.exists():
                self._save()
                self._dirty = 0

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        payload = {"processed_ids": sorted(self.processed)}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def is_processed(self, cafe_id: int) -> bool:
        return cafe_id in self.processed


# ---------------------------------------------------------------------------
# Concurrent Pipeline
# ---------------------------------------------------------------------------
class ConcurrentPipeline:
    """
    Worker pool sharing an asyncio.Queue of cafes.

    Each worker owns one tab (ScraperWorker) and processes cafes one
    at a time. Exporter and StateManager are shared across workers and
    are internally synchronized.
    """

    # Small jitter between cafes within the same worker. Prevents all
    # workers from hitting Google at exactly the same rate.
    MIN_DELAY = 0.5
    MAX_DELAY = 1.5

    # On transient failure, reset the tab and retry once.
    MAX_ATTEMPTS = 2

    def __init__(
        self,
        loader: DatabaseLoader,
        scraper: GoogleMapsScraper,
        exporter: CSVExporter,
        state: StateManager,
        workers: int = 4,
        resolver: Optional[LocationResolver] = None,
    ) -> None:
        self.loader = loader
        self.scraper = scraper
        self.exporter = exporter
        self.state = state
        self.workers = workers
        self.resolver = resolver or LocationResolver()

    async def run(self, limit: Optional[int] = None) -> None:
        pending: list[CafeRecord] = [
            cafe
            for cafe in self.loader.iter_cafes(approved_only=True)
            if not self.state.is_processed(cafe.id)
        ]
        if limit:
            pending = pending[:limit]

        total = len(pending)
        logger.info(
            f"Pending cafes to crawl: {total} "
            f"(workers={self.workers})"
        )
        if total == 0:
            logger.success("Nothing to do.")
            return

        queue: asyncio.Queue[CafeRecord] = asyncio.Queue()
        for cafe in pending:
            queue.put_nowait(cafe)

        progress = tqdm_asyncio(
            total=total, desc="Crawling", unit="cafe"
        )
        try:
            await asyncio.gather(
                *[
                    self._worker(i, queue, progress)
                    for i in range(self.workers)
                ]
            )
        finally:
            progress.close()
            await self.state.flush()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    async def _worker(
        self,
        worker_id: int,
        queue: asyncio.Queue,
        progress,
    ) -> None:
        worker_scraper = ScraperWorker(
            self.scraper.context,
            worker_id,
            self.scraper.page_pool,
        )
        try:
            while True:
                try:
                    cafe = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                try:
                    await self._process_one(
                        worker_scraper, worker_id, cafe, progress
                    )
                finally:
                    queue.task_done()

                await asyncio.sleep(
                    random.uniform(self.MIN_DELAY, self.MAX_DELAY)
                )
        finally:
            await worker_scraper.close()
            logger.debug(f"[w{worker_id}] Worker shutting down.")

    async def _process_one(
        self,
        worker_scraper: ScraperWorker,
        worker_id: int,
        cafe: CafeRecord,
        progress,
    ) -> None:
        progress.set_postfix_str(
            f"w{worker_id} id={cafe.id} · {cafe.name[:25]}"
        )

        target: ResolvedTarget = self.resolver.resolve(cafe)

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                reviews = await worker_scraper.scrape(target)
                if reviews:
                    await self.exporter.append_many(reviews)
                    await self.state.mark_processed(cafe.id)
                    logger.debug(
                        f"[w{worker_id}][{cafe.id}] checkpoint saved "
                        f"(rows={len(reviews)})."
                    )
                else:
                    logger.warning(
                        f"[w{worker_id}][{cafe.id}] No reviews extracted "
                        f"— NOT checkpointed, will retry on next run."
                    )
                return
            except Exception as exc:
                logger.exception(
                    f"[w{worker_id}][{cafe.id}] attempt {attempt} "
                    f"failed: {exc}"
                )
                if attempt < self.MAX_ATTEMPTS:
                    await worker_scraper.reset_page()
                    await asyncio.sleep(1.0)
                else:
                    logger.error(
                        f"[w{worker_id}][{cafe.id}] giving up after "
                        f"{self.MAX_ATTEMPTS} attempts."
                    )
        progress.update(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gmaps-review-crawler",
        description="Google Maps review crawler (async, resumable).",
    )
    p.add_argument(
        "--db", default="cafes_cafe.db",
        help="Path to SQLite database.",
    )
    p.add_argument(
        "--output", default="data/google_reviews.csv",
        help="Output CSV path.",
    )
    p.add_argument(
        "--state", default="crawler_state.json",
        help="Checkpoint state file.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Max cafes to process.",
    )
    p.add_argument(
        "--workers", type=int, default=4,
        help=(
            "Number of concurrent tabs (default: 4). "
            "Open this many Maps tabs in your debug Chrome first."
        ),
    )
    p.add_argument(
        "--headful", action="store_true",
        help="Show browser window (only used if CDP fails).",
    )
    p.add_argument(
        "--cdp-url", default="http://localhost:9222",
        help="CDP endpoint of a running Chrome instance.",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def _configure_logging(level: str) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> — "
        "<level>{message}</level>",
    )
    Path("logs").mkdir(exist_ok=True)
    logger.add(
        "logs/crawler_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
    )


async def _main_async(args: argparse.Namespace) -> None:
    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")

    with DatabaseLoader(args.db) as loader:
        exporter = CSVExporter(args.output)
        state = StateManager(args.state)

        async with GoogleMapsScraper(
            headless=not args.headful,
            cdp_url=args.cdp_url,
        ) as scraper:
            if scraper.page_pool.qsize() < args.workers:
                logger.warning(
                    f"Only {scraper.page_pool.qsize()} Maps tab(s) "
                    f"available, but --workers={args.workers}. "
                    f"Some workers will create new (possibly blank) "
                    f"tabs. Open more Maps tabs in Chrome first."
                )
            pipeline = ConcurrentPipeline(
                loader=loader,
                scraper=scraper,
                exporter=exporter,
                state=state,
                workers=args.workers,
            )
            await pipeline.run(limit=args.limit)

    logger.success("Crawl complete.")


def main() -> None:
    args = _build_parser().parse_args()
    _configure_logging(args.log_level)
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        logger.warning(
            "Interrupted by user — state preserved. Resume anytime."
        )


if __name__ == "__main__":
    main()
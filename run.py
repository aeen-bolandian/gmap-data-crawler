"""
run.py
======
Main CLI entrypoint for the gmaps-review-crawler pipeline.

Orchestrates:
    1. Loading state (processed IDs) from crawler_state.json
    2. Iterating over unresolved cafes from SQLite
    3. Running Phase 1 (resolver) + Phase 2 (scraper)
    4. Streaming output via CSVExporter
    5. Atomically checkpointing state after each cafe

Usage
-----
$ python run.py --db cafes_cafe.db --output data/google_reviews.csv
$ python run.py --limit 50 --headful    # debug mode
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

from loguru import logger
from tqdm.asyncio import tqdm_asyncio

from src.db_loader import DatabaseLoader, CafeRecord
from src.exporter import CSVExporter
from src.resolver import LocationResolver, ResolvedTarget
from src.scraper import GoogleMapsScraper


# ---------------------------------------------------------------------------
# State Manager (Checkpointing)
# ---------------------------------------------------------------------------
class StateManager:
    """Persists processed cafe IDs for crash-safe resumption."""

    def __init__(
        self, state_path: str | Path = "crawler_state.json"
    ) -> None:
        self.state_path = Path(state_path)
        self.processed: set[int] = self._load()

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

    def mark_processed(self, cafe_id: int) -> None:
        self.processed.add(cafe_id)
        self._save()

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        payload = {"processed_ids": sorted(self.processed)}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)  # atomic on POSIX & Windows

    def is_processed(self, cafe_id: int) -> bool:
        return cafe_id in self.processed


# ---------------------------------------------------------------------------
# Pipeline Orchestrator
# ---------------------------------------------------------------------------
class CrawlerPipeline:
    # Jitter between cafes to look human and avoid rate-limit bans.
    MIN_DELAY = 3.0
    MAX_DELAY = 8.0

    def __init__(
        self,
        loader: DatabaseLoader,
        scraper: GoogleMapsScraper,
        exporter: CSVExporter,
        state: StateManager,
        resolver: LocationResolver | None = None,
    ) -> None:
        self.loader = loader
        self.scraper = scraper
        self.exporter = exporter
        self.state = state
        self.resolver = resolver or LocationResolver()

    async def run(self, limit: int | None = None) -> None:
        # Collect pending cafes in one pass.
        pending: list[CafeRecord] = [
            cafe
            for cafe in self.loader.iter_cafes(approved_only=True)
            if not self.state.is_processed(cafe.id)
        ]

        if limit:
            pending = pending[:limit]

        total = len(pending)
        logger.info(f"Pending cafes to crawl: {total}")

        if total == 0:
            logger.success(
                "Nothing to do — all records already processed."
            )
            return

        progress = tqdm_asyncio(
            total=total, desc="Crawling", unit="cafe"
        )
        try:
            for idx, cafe in enumerate(pending):
                progress.set_postfix_str(
                    f"id={cafe.id} · {cafe.name[:30]}"
                )
                try:
                    ok = await self._process_one(cafe)
                    if not ok:
                        logger.warning(
                            f"[{cafe.id}] No reviews extracted — "
                            f"NOT checkpointed, will retry on next run."
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        f"[{cafe.id}] Pipeline failure: {exc}"
                    )
                finally:
                    progress.update(1)

                # Inter-cafe jitter (skip after the last one).
                if idx < total - 1:
                    delay = random.uniform(
                        self.MIN_DELAY, self.MAX_DELAY
                    )
                    logger.debug(
                        f"Sleeping {delay:.1f}s before next cafe."
                    )
                    await asyncio.sleep(delay)
        finally:
            progress.close()

    async def _process_one(self, cafe: CafeRecord) -> bool:
        """Returns True if the cafe was successfully processed."""
        target: ResolvedTarget = self.resolver.resolve(cafe)
        reviews = await self.scraper.scrape(target)

        if not reviews:
            # Could be a transient failure or a genuinely review-less
            # place. We do NOT checkpoint so it will be retried.
            return False

        written = await self.exporter.append_many(reviews)
        self.state.mark_processed(cafe.id)
        logger.debug(
            f"[{cafe.id}] checkpoint saved (rows={written})."
        )
        return True


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
    with DatabaseLoader(args.db) as loader:
        exporter = CSVExporter(args.output)
        state = StateManager(args.state)

        async with GoogleMapsScraper(
            headless=not args.headful,
            cdp_url=args.cdp_url,
        ) as scraper:
            pipeline = CrawlerPipeline(
                loader, scraper, exporter, state
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
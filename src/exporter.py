"""
src/exporter.py
===============
Append-only CSV streaming exporter.

Writes `ReviewRecord` objects to disk incrementally so that long-running
crawls never hold data in memory — critical when processing 7,000+
establishments and hundreds of thousands of reviews.

Features
--------
- Atomic header creation (only on first write)
- Async-safe file handle via asyncio.Lock
- Automatic directory creation
- UTF-8 with BOM for Excel compatibility
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path
from typing import Iterable

from loguru import logger

from src.scraper import ReviewRecord


class CSVExporter:
    """Streams review records into an append-only CSV file."""

    COLUMNS = [
        "cafe_id", "cafe_name", "city", "author", "rating", "date", "text"
    ]

    def __init__(self, output_path: str | Path) -> None:
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def append_many(self, reviews: Iterable[ReviewRecord]) -> int:
        """Append a batch of reviews. Returns number of rows written."""
        reviews = list(reviews)
        if not reviews:
            return 0
        async with self._lock:
            await asyncio.to_thread(self._write_batch, reviews)
        return len(reviews)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _write_batch(self, reviews: list[ReviewRecord]) -> None:
        new_file = (
            not self.output_path.exists()
            or self.output_path.stat().st_size == 0
        )
        with self.output_path.open(
            "a", encoding="utf-8-sig", newline=""
        ) as fh:
            writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
            if new_file:
                writer.writerow(self.COLUMNS)
                logger.info(f"Created CSV with header → {self.output_path}")
            for review in reviews:
                writer.writerow(review.to_csv_row())
        logger.debug(
            f"Appended {len(reviews)} rows to {self.output_path}"
        )
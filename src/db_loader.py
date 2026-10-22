"""
src/db_loader.py
================
Secure SQLite data extraction layer.

Loads approved cafe records from a local SQLite database (e.g. `cafes_cafe.db`).
All private data is kept out of version control via `.gitignore`.

Provides
--------
- `CafeRecord`        : Typed dataclass for a single establishment.
- `DatabaseLoader`    : Context-managed SQLite reader (read-only).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Iterator, Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Typed Model
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CafeRecord:
    """Immutable representation of a single cafe record."""

    id: int
    name: str
    address: str
    city: str
    neighborhood: str
    latitude: Optional[float]
    longitude: Optional[float]
    phone: Optional[str]
    search_address: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def has_coordinates(self) -> bool:
        """True when both lat/lng are valid numbers (0.0 is a valid value)."""
        return (
            self.latitude is not None
            and self.longitude is not None
            and -90 <= self.latitude <= 90
            and -180 <= self.longitude <= 180
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
class DatabaseLoader:
    """
    Read-only SQLite loader for `cafes_cafe` table.

    Usage
    -----
    >>> with DatabaseLoader("cafes_cafe.db") as loader:
    ...     for cafe in loader.iter_cafes():
    ...         print(cafe.name)
    """

    TABLE_NAME = "cafes_cafe"
    REQUIRED_COLUMNS = (
        "id", "name", "address", "city", "neighborhood",
        "latitude", "longitude", "phone",
    )

    # Optional columns — read if present, default to None otherwise.
    OPTIONAL_COLUMNS = ("search_address", "search_name", "search_city")

    # Approval filtering — adjust APPROVAL_VALUE if your DB stores a
    # different string (e.g. "APPROVED", "1", "Approved").
    APPROVAL_COLUMN = "approval_status"
    APPROVAL_VALUE = "approved"

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._connection: Optional[sqlite3.Connection] = None
        self._cached_columns: Optional[set[str]] = None

    # ----------------------------- Lifecycle -----------------------------
    def __enter__(self) -> "DatabaseLoader":
        if not self.db_path.exists():
            raise FileNotFoundError(f"SQLite database not found: {self.db_path}")

        uri = f"file:{self.db_path}?mode=ro"
        try:
            self._connection = sqlite3.connect(uri, uri=True)
            self._connection.row_factory = sqlite3.Row
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"Cannot open SQLite file {self.db_path}: {exc}\n"
                f"Recover with:  sqlite3 {self.db_path} \".recover\" "
                f"| sqlite3 {self.db_path}.recovered"
            ) from exc

        logger.info(f"SQLite opened (read-only): {self.db_path}")
        self._integrity_precheck()
        self._validate_schema()
        self._cached_columns = self._column_names()
        self._warn_if_approval_mismatch()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._connection is not None:
            self._connection.close()
            logger.debug("SQLite connection closed.")
            self._connection = None
            self._cached_columns = None

    # ----------------------------- Schema --------------------------------
    def _integrity_precheck(self) -> None:
        """Fail fast with a clear message if the file is corrupted."""
        assert self._connection is not None
        cursor = self._connection.cursor()
        try:
            cursor.execute("PRAGMA quick_check;")
            row = cursor.fetchone()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"SQLite file {self.db_path} is malformed: {exc}\n"
                f"Recover with:  sqlite3 {self.db_path} \".recover\" "
                f"| sqlite3 {self.db_path}.recovered"
            ) from exc

        result = row[0] if row else "no result"
        if result != "ok":
            raise RuntimeError(
                f"SQLite integrity check FAILED on {self.db_path}: {result}\n"
                f"Recover with:  sqlite3 {self.db_path} \".recover\" "
                f"| sqlite3 {self.db_path}.recovered"
            )

    def _validate_schema(self) -> None:
        assert self._connection is not None
        cols = self._column_names()
        missing = set(self.REQUIRED_COLUMNS) - cols
        if missing:
            raise RuntimeError(
                f"Table '{self.TABLE_NAME}' missing required columns: {missing}"
            )
        logger.debug(f"Schema OK — {len(cols)} columns detected.")

    def _warn_if_approval_mismatch(self) -> None:
        """Warn loudly if the approval filter would match zero rows."""
        assert self._connection is not None
        if self.APPROVAL_COLUMN not in (self._cached_columns or set()):
            logger.warning(
                f"Approval column '{self.APPROVAL_COLUMN}' not found — "
                f"approved_only filter will be ignored."
            )
            return
        try:
            cursor = self._connection.cursor()
            cursor.execute(
                f"SELECT COUNT(*) FROM {self.TABLE_NAME} "
                f"WHERE {self.APPROVAL_COLUMN} = ?;",
                (self.APPROVAL_VALUE,),
            )
            count = cursor.fetchone()[0]
        except sqlite3.DatabaseError as exc:
            logger.warning(f"Approval precheck failed: {exc}")
            return

        if count == 0:
            logger.warning(
                f"No rows match {self.APPROVAL_COLUMN}='{self.APPROVAL_VALUE}'. "
                f"Check APPROVAL_VALUE — the crawl would produce zero work."
            )
        else:
            logger.debug(f"Approval precheck OK — {count} approved rows.")

    # ----------------------------- Queries -------------------------------
    def _select_columns(self) -> str:
        """Build the SELECT list including optional columns if present."""
        cols = [
            "id", "name", "address", "city", "neighborhood",
            "latitude", "longitude", "phone",
        ]
        for opt in self.OPTIONAL_COLUMNS:
            if opt in (self._cached_columns or set()):
                cols.append(opt)
        return ", ".join(cols)

    def iter_cafes(self, approved_only: bool = True) -> Iterator[CafeRecord]:
        """
        Yield cafe records as `CafeRecord` objects (streamed, not buffered).

        When `approved_only=True` and the approval column exists, only
        rows matching `APPROVAL_VALUE` are yielded.
        """
        assert self._connection is not None
        cursor = self._connection.cursor()

        where_clause = ""
        params: tuple = ()
        if approved_only and self.APPROVAL_COLUMN in (self._cached_columns or set()):
            where_clause = f"WHERE {self.APPROVAL_COLUMN} = ?"
            params = (self.APPROVAL_VALUE,)

        query = f"""
            SELECT {self._select_columns()}
            FROM {self.TABLE_NAME}
            {where_clause}
            ORDER BY id ASC;
        """
        cursor.execute(query, params)

        count = 0
        for row in cursor:                      # streamed, no fetchall()
            yield self._row_to_record(row)
            count += 1

        logger.info(f"Loaded {count} cafe records from {self.TABLE_NAME}.")

    def get_by_id(self, cafe_id: int) -> Optional[CafeRecord]:
        assert self._connection is not None
        cursor = self._connection.cursor()
        cursor.execute(
            f"SELECT {self._select_columns()} FROM {self.TABLE_NAME} WHERE id = ?;",
            (cafe_id,),
        )
        row = cursor.fetchone()
        return self._row_to_record(row) if row else None

    # ----------------------------- Helpers -------------------------------
    def _column_names(self) -> set[str]:
        assert self._connection is not None
        cursor = self._connection.cursor()
        cursor.execute(f"PRAGMA table_info({self.TABLE_NAME});")
        return {row["name"] for row in cursor.fetchall()}

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> CafeRecord:
        def _clean_str(val) -> Optional[str]:
            return str(val).strip() if val not in (None, "") else None

        def _clean_float(val) -> Optional[float]:
            # 0.0 is a legitimate coordinate — use `is not None`, not truthiness.
            return float(val) if val is not None and val != "" else None

        def _get_optional(key: str):
            """Safely read an optional column if it exists in the row."""
            try:
                return row[key]
            except (IndexError, KeyError):
                return None

        return CafeRecord(
            id=int(row["id"]),
            name=(row["name"] or "").strip(),
            address=(row["address"] or "").strip(),
            city=(row["city"] or "").strip(),
            neighborhood=(row["neighborhood"] or "").strip(),
            latitude=_clean_float(row["latitude"]),
            longitude=_clean_float(row["longitude"]),
            phone=_clean_str(row["phone"]),
            search_address=_clean_str(_get_optional("search_address")),
        )
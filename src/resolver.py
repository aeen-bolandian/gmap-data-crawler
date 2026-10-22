"""
src/resolver.py
===============
Phase 1 — Location Resolver Pipeline.

Builds the most accurate Google Maps URL for each establishment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote_plus

from loguru import logger

from src.db_loader import CafeRecord


class ResolverStrategy(str, Enum):
    DIRECT_SEARCH = "direct_search"
    PHONE_SEARCH = "phone_search"
    TEXT_SEARCH = "text_search"


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """Output of the resolver — everything the scraper needs."""

    cafe: CafeRecord
    url: str
    strategy: ResolverStrategy


class LocationResolver:
    """
    Converts CafeRecord -> ResolvedTarget (Google Maps URL).

    Notes
    -----
    - We use the `?q=` query form (not `/search/<query>/@lat,lng,z`) because
      it is far more stable across Google's URL scheme changes and safely
      handles names containing `/`, `#`, etc.
    - `hl=en` is forced so scrapers don't depend on the user's UI language.
    - Coordinates are only appended when present; the map still centers on
      the search result otherwise.
    """

    MAPS_BASE = "https://www.google.com/maps"
    DEFAULT_LANG = "en"

    def __init__(self, zoom: int = 17, lang: str = DEFAULT_LANG) -> None:
        self.zoom = zoom
        self.lang = lang

    def resolve(self, cafe: CafeRecord) -> ResolvedTarget:
        """Resolve a cafe record into a Google Maps URL."""
        query = self._build_query(cafe)
        url = self._build_url(query, cafe)
        strategy = ResolverStrategy.DIRECT_SEARCH
        logger.debug(f"[{cafe.id}] Resolved URL → {url}")
        return ResolvedTarget(cafe, url, strategy)

    # ----------------------------- Internals -----------------------------
    def _build_query(self, cafe: CafeRecord) -> str:
        clean_name = cafe.name.replace("(", " ").replace(")", " ").strip()
        parts = [clean_name, cafe.neighborhood, cafe.city]
        return " ".join(p for p in parts if p)

    def _build_url(self, query: str, cafe: CafeRecord) -> str:
        encoded = quote_plus(query)
        base = f"{self.MAPS_BASE}/search/?api=1&query={encoded}&hl={self.lang}"
        if cafe.has_coordinates:
            base += f"&center={cafe.latitude},{cafe.longitude}&zoom={self.zoom}"
        return base

    @staticmethod
    def _phone_query(phone: str) -> str:
        """Normalize a phone number for use as a search query."""
        digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
        return digits or phone
"""
src/resolver.py
===============
Phase 1 — Location Resolver Pipeline.

Builds the most accurate Google Maps URL for each establishment.

Query strategy
--------------
Google Maps' `?q=` searches on keywords. Adding `neighborhood + city`
to the cafe name works when the neighborhood is unique, but many cafes
share a name across branches (e.g. "Gennaro" has Zafaranieh and
Marzdaran branches) and the DB's `neighborhood` column is sometimes
coarser than the Google-formatted `search_address`.

We therefore prefer `search_address` (a rich, Google-formatted
address) when it is available, and fall back to
`neighborhood + city` when it is not. `_short_address()` picks the
last 3 meaningful parts of `search_address` so the query stays focused
(e.g. "بلوار مرزداران اطاعتی جنوبی نسترن" instead of the full
"ایران، تهران، شهر تهران، ...").
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


# Tokens that mark a part as noise (country/city duplicates) when we
# extract from `search_address`.
_NOISE_TOKENS = ("ایران", "تهران", "شهر تهران", "استان تهران", "شهر")

# Tokens that mark a part as meaningful (used to keep the tail of the
# address).
_MEANINGFUL_PREFIXES = (
    "منطقه", "منطقه‌", "شهرک", "محله", "خیابان", "بلوار", "کوچه",
    "میدان", "بزرگراه", "اتوبان", "باغ", "پارک",
)


def _short_address(search_address: str, keep_tail: int = 3) -> str:
    """
    Extract the last `keep_tail` meaningful parts of a Google-formatted
    address and join them with spaces.

    Example
    -------
    >>> _short_address(
    ...     "ایران، تهران، شهر تهران، تهران، منطقه2 شهر تهران، "
    ...     "شهرک آزمایش، بلوار مرزداران، اطاعتی جنوبی، نسترن"
    ... )
    'بلوار مرزداران اطاعتی جنوبی نسترن'
    """
    if not search_address:
        return ""

    parts = [p.strip() for p in search_address.split("،") if p.strip()]
    meaningful: list[str] = []
    for p in parts:
        # Skip pure-noise tokens.
        if p in _NOISE_TOKENS:
            continue
        # Skip "شهر تهران" variants (contain city name and no meaningful prefix).
        if p.startswith(("شهر ", "استان ")) and "تهران" in p:
            continue
        meaningful.append(p)

    # Also strip "منطقه N شهر تهران" into just "منطقه N".
    cleaned: list[str] = []
    for p in meaningful:
        if p.startswith("منطقه") and "شهر تهران" in p:
            cleaned.append(p.split("شهر تهران")[0].strip())
        else:
            cleaned.append(p)

    # Keep only the trailing N parts — the head is usually administrative.
    tail = cleaned[-keep_tail:] if len(cleaned) > keep_tail else cleaned
    return " ".join(tail)


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
    - The query is built from `name + short_address` when `search_address`
      is available, and `name + neighborhood + city` otherwise.
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
        """
        Build the Google Maps `?q=` query string.

        Preference order:
        1. name + short_address (from search_address)
        2. name + neighborhood + city (fallback)
        """
        clean_name = cafe.name.replace("(", " ").replace(")", " ").strip()

        short_addr = _short_address(cafe.search_address or "")
        if short_addr:
            query = f"{clean_name} {short_addr}".strip()
            logger.debug(
                f"[{cafe.id}] Query (search_address): {query!r}"
            )
            return query

        parts = [clean_name, cafe.neighborhood, cafe.city]
        query = " ".join(p for p in parts if p)
        logger.debug(
            f"[{cafe.id}] Query (fallback name+neighborhood+city): {query!r}"
        )
        return query

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
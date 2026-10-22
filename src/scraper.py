"""
src/scraper.py
==============
Phase 2 — Review Extraction Engine.

Strategy
--------
- Prefer reusing an existing logged-in tab from a CDP-attached Chrome
  (so we inherit the user's real session, avoiding Google's
  "Limited View" mode for anonymous visitors).
- Fall back to a fresh tab / new browser only when no session is available.
- Never close a tab we did not create ourselves.
- VALIDATE that the opened place matches the cafe in the database
  (by coordinates, falling back to name) before extracting reviews.
  This prevents storing reviews of a different cafe when the ?q=
  query is ambiguous.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
)
from playwright_stealth import stealth_async

from src.db_loader import CafeRecord
from src.resolver import ResolvedTarget


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReviewRecord:
    cafe_id: int
    cafe_name: str
    city: str
    author: str
    rating: str
    date: str
    text: str

    def to_csv_row(self) -> list[str]:
        # Collapse newlines so Excel users see single-line cells.
        safe_text = " ".join(self.text.splitlines()).strip()
        return [
            str(self.cafe_id),
            self.cafe_name,
            self.city,
            self.author,
            self.rating,
            self.date,
            safe_text,
        ]


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------
# Matches Google Maps' `/@LAT,LNG,ZOOMz` URL fragment.
_COORD_RE = re.compile(r"/@(-?\d+\.\d+),(-?\d+\.\d+)")

# Max distance (meters) between DB coords and URL coords to consider
# the opened place a match for the cafe record.
MAX_MATCH_DISTANCE_M = 100.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters between two lat/lng points."""
    R = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2.0) ** 2
    )
    return 2.0 * R * math.asin(math.sqrt(a))


def extract_coords_from_url(url: str) -> Optional[tuple[float, float]]:
    """Pull (lat, lng) out of a Google Maps place URL, if present."""
    m = _COORD_RE.search(url)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Name normalization (fallback when URL has no coords)
# ---------------------------------------------------------------------------
# Common cafe/restaurant prefixes that add noise to matching.
_STRIP_TOKENS = (
    "cafe", "کافه", "coffee", "restaurant", "رستوران", "کافهرستوران",
    "کافه رستوران", "کافې", "کافە",
)


def _normalize_name(name: str) -> str:
    """
    Normalize a place name for comparison:
    - Unicode NFKC (collapse ﬁ → fi, etc.)
    - Lowercase
    - Convert Arabic ي/ك to Persian ی/ک
    - Remove diacritics (U+064B..U+065F, U+0670)
    - Replace ZWNJ/ZWJ with space
    - Strip common cafe/restaurant tokens
    - Collapse whitespace
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name)
    s = s.lower()
    s = s.replace("\u064A", "\u06CC")  # ي -> ی
    s = s.replace("\u0643", "\u06A9")  # ك -> ک
    # Remove Arabic diacritics
    s = re.sub(r"[\u064B-\u065F\u0670]", "", s)
    # ZWNJ/ZWJ -> space
    s = s.replace("\u200C", " ").replace("\u200D", " ")
    # Strip leading/trailing cafe/restaurant tokens
    for tok in _STRIP_TOKENS:
        s = s.strip()
        if s.startswith(tok + " "):
            s = s[len(tok) + 1 :]
        if s.endswith(" " + tok):
            s = s[: -len(tok) - 1]
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def names_look_same(a: str, b: str) -> bool:
    """
    Loose name-match used only as a fallback when coordinates are
    missing from the URL. Returns True only when the shorter string is
    at least 5 chars and is fully contained in the longer one.
    """
    na = _normalize_name(a)
    nb = _normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = sorted((na, nb), key=len)
    if len(shorter) < 5:
        return False
    return shorter in longer


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------
class GoogleMapsScraper:
    # --- Selectors ---------------------------------------------------------
    # `jftiEf` is the *card* class. `data-review-id` is set on both the
    # card and (sometimes) an outer wrapper, so matching on both with a
    # comma-selector double-counts every review. Use `jftiEf` first and
    # only fall back to `data-review-id` when no card is found at all.
    SEL_REVIEW_ITEM = "div.jftiEf"
    SEL_REVIEW_ITEM_FALLBACK = "div[data-review-id]"

    SEL_MORE_BUTTON = (
        'button.w8nwRe.kyuRq, '
        'button[aria-label*="More" i], '
        'button[aria-label*="بیشتر" i]'
    )
    # Author: `d4r55` is the canonical class; fallback is a contributor link.
    SEL_AUTHOR = ".d4r55, button[data-href*='/maps/contrib/'] .d4r55"
    # Rating: the star span reliably carries role="img" + aria-label.
    SEL_RATING = (
        'span[role="img"][aria-label*="star" i], '
        'span[role="img"][aria-label*="ستاره" i], '
        'span.kvMYJc'
    )
    # Date: scoped to the review header row, not generic body-small text.
    SEL_DATE = "span.rsqaWe"
    # Text: scoped to the visible review body.
    SEL_TEXT = "span.wiI7pd"

    # --- Page-type detection --------------------------------------------
    SEL_PLACE_H1 = "h1"
    SEL_PLACE_TAB = 'button[role="tab"]'
    SEL_REVIEWS_TAB = (
        'button[role="tab"]:has-text("Reviews"), '
        'button[role="tab"]:has-text("نظرات"), '
        'button[aria-label*="Reviews for" i], '
        'button[aria-label*="نظرات" i]'
    )
    SEL_FEED = 'div[role="feed"]'
    # Results are sometimes a plain list of `/maps/place/` links instead
    # of a `div[role="feed"]` (Google serves multiple layouts).
    SEL_RESULT_LINK = 'a[href*="/maps/place/"]'
    SEL_FIRST_RESULT = (
        'div[role="feed"] a[href*="/maps/place/"], a.hfpxzc, '
        + SEL_RESULT_LINK
    )

    # --- Timing configuration -------------------------------------------
    PAGE_LOAD_TIMEOUT_MS = 60_000
    POST_GOTO_SETTLE_MS = 5_000
    POST_TAB_CLICK_SETTLE_MS = 2_000
    POST_FIRST_RESULT_SETTLE_MS = 3_000
    POST_MORE_EXPAND_SETTLE_MS = 300
    SIGNAL_POLL_INTERVAL_MS = 1_000
    SIGNAL_POLL_MAX_ATTEMPTS = 20

    # --- Scroll tuning (event-driven) -----------------------------------
    SCROLL_STEP_PX = 3_000
    WINDOW_SCROLL_STEP_PX = 1_500
    SCROLL_MAX_ITERATIONS = 25
    SCROLL_WAIT_FOR_NEW_CARD_MS = 2_500
    SCROLL_MAX_STAGNANT = 3

    MORE_BUTTON_CLICK_TIMEOUT_MS = 800

    # --- Consent selectors (in case the user's session was not accepted) ---
    CONSENT_SELECTORS = (
        'button:has-text("Accept all")',
        'button:has-text("Reject all")',
        'button[aria-label*="Accept" i]',
        'form[action*="consent"] button',
    )

    def __init__(
        self,
        headless: bool = True,
        max_reviews: int = 150,
        proxy: Optional[dict] = None,
        cdp_url: str = "http://localhost:9222",
    ) -> None:
        self.headless = headless
        self.max_reviews = max_reviews
        self.proxy = proxy
        self.cdp_url = cdp_url
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._existing_page: Optional[Page] = None
        self._owns_browser = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "GoogleMapsScraper":
        self._playwright = await async_playwright().start()

        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self.cdp_url
            )
            logger.info(f"Connected to Chrome via CDP ({self.cdp_url}).")

            if self._browser.contexts:
                self._context = self._browser.contexts[0]
                logger.info("Reusing existing browser context.")
            else:
                self._context = await self._browser.new_context(
                    viewport={"width": 1366, "height": 900}
                )
                logger.info("Created new browser context.")

            self._owns_browser = False

            self._existing_page = self._pick_existing_page()
            if self._existing_page is not None:
                logger.info(
                    f"Reusing existing tab: {self._existing_page.url[:80]}"
                )
            else:
                logger.info(
                    "No existing tab found — a new tab will be created "
                    "on the first scrape."
                )

        except Exception as exc:
            logger.warning(f"CDP failed ({exc}), launching Chromium.")
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                proxy=self.proxy,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1366, "height": 900}
            )
            self._owns_browser = True
            self._existing_page = None

        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_browser and self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Browser close warning: {exc}")
        if self._playwright is not None:
            await self._playwright.stop()
        logger.debug("Scraper shutdown complete.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def scrape(self, target: ResolvedTarget) -> list[ReviewRecord]:
        assert self._context is not None

        reused = (
            self._existing_page is not None
            and not self._existing_page.is_closed()
        )
        if reused:
            page = self._existing_page
            logger.debug(f"[{target.cafe.id}] Reusing existing tab.")
        else:
            page = await self._context.new_page()
            await stealth_async(page)
            logger.debug(f"[{target.cafe.id}] Created new tab.")

        try:
            await page.bring_to_front()
            logger.debug(f"[{target.cafe.id}] Opening URL: {target.url}")
            await page.goto(
                target.url,
                timeout=self.PAGE_LOAD_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(self.POST_GOTO_SETTLE_MS)

            await self._handle_consent(page, target.cafe)

            if not await self._open_reviews_tab(page, target.cafe):
                logger.warning(
                    f"[{target.cafe.id}] Could not open Reviews tab or "
                    f"place did not match — skipping."
                )
                return []

            # ---- Validate that this is the right place ---------------
            if not await self._validate_place_match(page, target.cafe):
                # _validate_place_match already logged the reason.
                return []

            logger.info(
                f"[{target.cafe.id}] Reviews tab opened. Scrolling..."
            )
            await self._scroll_reviews_container(page)
            await self._expand_all_more_buttons(page)

            reviews = await self._extract_reviews(page, target.cafe)
            if reviews:
                logger.success(
                    f"[{target.cafe.id}] {target.cafe.name} → "
                    f"Extracted {len(reviews)} reviews"
                )
            return reviews

        except Exception as exc:
            logger.exception(f"[{target.cafe.id}] Unexpected failure: {exc}")
            return []
        finally:
            if page is not self._existing_page:
                try:
                    await page.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Tab selection & consent
    # ------------------------------------------------------------------
    def _pick_existing_page(self) -> Optional[Page]:
        if not self._context or not self._context.pages:
            return None

        pages = list(self._context.pages)
        candidates = [
            p
            for p in pages
            if not p.is_closed() and not p.url.startswith(
                ("about:", "chrome://", "devtools://")
            )
        ]
        if not candidates:
            return None

        maps_tabs = [
            p for p in candidates if "google.com/maps" in p.url
        ]
        if maps_tabs:
            return maps_tabs[0]

        return candidates[0]

    async def _handle_consent(self, page: Page, cafe: CafeRecord) -> None:
        try:
            if "consent.google.com" not in page.url:
                return
            logger.debug(
                f"[{cafe.id}] Consent page detected — dismissing."
            )
            for sel in self.CONSENT_SELECTORS:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    logger.debug(f"[{cafe.id}] Clicked: {sel}")
                    await page.wait_for_timeout(
                        self.POST_TAB_CLICK_SETTLE_MS
                    )
                    return
        except Exception as exc:
            logger.debug(f"[{cafe.id}] Consent handler warning: {exc}")

    # ------------------------------------------------------------------
    # Reviews tab discovery (with strict page-type detection)
    # ------------------------------------------------------------------
    async def _open_reviews_tab(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        """
        Make sure the Reviews tab is open and its card list is loaded.

        Detection is based on DOM signals, not URL heuristics:
        - A `button[role="tab"]` means we are on a place page.
        - A `/maps/place/` link (or `div[role="feed"]`) means we are on
          a results list.

        We only click into the first result when a results list is
        detected; the subsequent _validate_place_match() call decides
        whether the opened place is actually the requested cafe.
        """
        # ---- Step 1: wait for a definitive page signal --------------
        signal: Optional[str] = None
        for _ in range(self.SIGNAL_POLL_MAX_ATTEMPTS):
            if await page.locator(self.SEL_PLACE_TAB).count() > 0:
                signal = "tabs"
                break
            if await page.locator(self.SEL_FEED).count() > 0:
                signal = "feed"
                break
            if await page.locator(self.SEL_RESULT_LINK).count() > 0:
                signal = "place_links"
                break
            await page.wait_for_timeout(self.SIGNAL_POLL_INTERVAL_MS)

        if signal is None:
            logger.warning(
                f"[{cafe.id}] No place tab, feed, or result links after "
                f"waiting — cannot determine page type."
            )
            return False

        logger.debug(f"[{cafe.id}] Page signal: {signal}")

        # ---- Step 2a: place page ------------------------------------
        if signal == "tabs":
            clicked = await self._click_reviews_tab(page, cafe)
            if not clicked:
                await page.wait_for_timeout(
                    self.POST_TAB_CLICK_SETTLE_MS
                )
                clicked = await self._click_reviews_tab(page, cafe)

            if not clicked:
                logger.warning(
                    f"[{cafe.id}] Place page detected but Reviews tab "
                    f"could not be clicked."
                )
                return False

            return await self._wait_for_review_cards(page, cafe)

        # ---- Step 2b: results list ----------------------------------
        first_link = page.locator(self.SEL_FIRST_RESULT).first
        if await first_link.count() == 0:
            logger.warning(
                f"[{cafe.id}] Results list detected but no clickable "
                f"result link found."
            )
            return False

        try:
            await first_link.click()
            await page.wait_for_timeout(
                self.POST_FIRST_RESULT_SETTLE_MS
            )
        except Exception as exc:
            logger.warning(
                f"[{cafe.id}] Failed to click first result: {exc}"
            )
            return False

        # After clicking, the page should now be a place page.
        if await page.locator(self.SEL_PLACE_TAB).count() == 0:
            logger.warning(
                f"[{cafe.id}] After clicking first result, no place "
                f"tab appeared."
            )
            return False

        clicked = await self._click_reviews_tab(page, cafe)
        if not clicked:
            logger.warning(
                f"[{cafe.id}] Reviews tab could not be clicked after "
                f"navigating to first result."
            )
            return False

        return await self._wait_for_review_cards(page, cafe)

    async def _click_reviews_tab(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        tab = page.locator(self.SEL_REVIEWS_TAB).first
        if await tab.count() == 0:
            return False

        try:
            selected = await tab.get_attribute("aria-selected")
        except Exception:
            selected = None
        if selected == "true":
            logger.debug(f"[{cafe.id}] Reviews tab already active.")
            return True

        if not await tab.is_visible():
            logger.debug(f"[{cafe.id}] Reviews tab not visible yet.")
            return False

        try:
            await tab.click()
            logger.debug(f"[{cafe.id}] Clicked Reviews tab.")
            await page.wait_for_timeout(self.POST_TAB_CLICK_SETTLE_MS)
            return True
        except Exception as exc:
            logger.debug(f"[{cafe.id}] Reviews tab click failed: {exc}")
            return False

    async def _wait_for_review_cards(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        for _ in range(self.SIGNAL_POLL_MAX_ATTEMPTS):
            try:
                count = await self._count_review_cards(page)
            except Exception:
                count = 0
            if count > 0:
                logger.debug(
                    f"[{cafe.id}] Review feed ready: {count} cards."
                )
                return True
            await page.wait_for_timeout(self.SIGNAL_POLL_INTERVAL_MS)
        logger.warning(
            f"[{cafe.id}] Review cards never appeared after clicking tab."
        )
        return False

    # ------------------------------------------------------------------
    # Place validation (STRICT — must match DB by coords or name)
    # ------------------------------------------------------------------
    async def _validate_place_match(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        """
        Verify that the currently-open place page is the cafe record.

        Primary check: compare URL coordinates against DB coordinates.
        A match requires distance <= MAX_MATCH_DISTANCE_M (100m).

        Fallback (only when URL has no coordinates): compare normalized
        names. This is rare — practically every Google Maps place URL
        carries `/@LAT,LNG,Zz`.

        Returns False (and logs a warning) on mismatch, so the caller
        skips extraction instead of storing wrong-cafe reviews.
        """
        current_url = page.url
        coords = extract_coords_from_url(current_url)

        # ---- Primary: coordinate-based match ------------------------
        if (
            coords is not None
            and cafe.latitude is not None
            and cafe.longitude is not None
        ):
            url_lat, url_lng = coords
            dist = haversine_m(
                cafe.latitude, cafe.longitude, url_lat, url_lng
            )
            if dist <= MAX_MATCH_DISTANCE_M:
                logger.debug(
                    f"[{cafe.id}] Place matched by coords "
                    f"(dist={dist:.1f}m)."
                )
                return True
            logger.warning(
                f"[{cafe.id}] Place does NOT match cafe coords: "
                f"db=({cafe.latitude:.5f},{cafe.longitude:.5f}) "
                f"page=({url_lat:.5f},{url_lng:.5f}) "
                f"dist={dist:.1f}m > {MAX_MATCH_DISTANCE_M}m. "
                f"Skipping to avoid storing wrong reviews."
            )
            return False

        # ---- Fallback: name-based match ----------------------------
        page_name = ""
        try:
            h1 = page.locator(self.SEL_PLACE_H1).first
            if await h1.count() > 0:
                page_name = (await h1.inner_text()).strip()
        except Exception:
            pass

        if page_name and names_look_same(cafe.name, page_name):
            logger.debug(
                f"[{cafe.id}] Place matched by name fallback: "
                f"db={cafe.name!r} page={page_name!r}"
            )
            return True

        logger.warning(
            f"[{cafe.id}] Place does NOT match (no coords in URL, "
            f"name mismatch): db={cafe.name!r} page={page_name!r}. "
            f"Skipping."
        )
        return False

    # ------------------------------------------------------------------
    # Review-item locator helpers
    # ------------------------------------------------------------------
    async def _review_item_locator(self, page: Page):
        primary = page.locator(self.SEL_REVIEW_ITEM)
        if await primary.count() > 0:
            return primary
        return page.locator(self.SEL_REVIEW_ITEM_FALLBACK)

    async def _count_review_cards(self, page: Page) -> int:
        locator = await self._review_item_locator(page)
        return await locator.count()

    # ------------------------------------------------------------------
    # Scrolling & expansion
    # ------------------------------------------------------------------
    async def _scroll_reviews_container(self, page: Page) -> None:
        last_count = 0
        stagnant = 0

        for _ in range(self.SCROLL_MAX_ITERATIONS):
            await page.evaluate(
                """(step) => {
                    const card = document.querySelector(
                        'div.jftiEf, div[data-review-id]'
                    );
                    if (card) {
                        let el = card;
                        while (
                            el && el.scrollHeight <= el.clientHeight + 4
                        ) {
                            el = el.parentElement;
                        }
                        if (el) { el.scrollBy(0, step); return; }
                    }
                    window.scrollBy(0, step);
                }""",
                self.SCROLL_STEP_PX,
            )

            try:
                await page.wait_for_function(
                    """(prev) => {
                        return document.querySelectorAll(
                            'div.jftiEf, div[data-review-id]'
                        ).length > prev;
                    }""",
                    arg=last_count,
                    timeout=self.SCROLL_WAIT_FOR_NEW_CARD_MS,
                )
            except Exception:
                stagnant += 1
                if stagnant >= self.SCROLL_MAX_STAGNANT:
                    logger.debug(
                        f"Scroll stalled after {stagnant} attempts."
                    )
                    break
                continue

            current_count = await self._count_review_cards(page)
            if current_count >= self.max_reviews:
                logger.debug(
                    f"Reached review cap ({self.max_reviews})."
                )
                break

            stagnant = 0
            last_count = current_count

    async def _expand_all_more_buttons(self, page: Page) -> None:
        selectors = [
            "button.w8nwRe.kyuRq",
            'button[aria-label*="More" i]',
            'button[aria-label*="بیشتر" i]',
        ]
        try:
            clicked = await page.evaluate(
                """(sels) => {
                    const btns = document.querySelectorAll(sels.join(','));
                    let n = 0;
                    for (const b of btns) {
                        try {
                            b.scrollIntoView({block: 'nearest'});
                            b.click();
                            n++;
                        } catch (e) {}
                    }
                    return n;
                }""",
                selectors,
            )
            logger.debug(
                f"Expanded {clicked} 'More' buttons in one pass."
            )
        except Exception as exc:
            logger.debug(f"Batch 'More' click failed: {exc}")

        await page.wait_for_timeout(self.POST_MORE_EXPAND_SETTLE_MS)

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    async def _extract_reviews(
        self, page: Page, cafe: CafeRecord
    ) -> list[ReviewRecord]:
        items = await self._review_item_locator(page)
        total = min(await items.count(), self.max_reviews)
        records: list[ReviewRecord] = []
        seen: set[tuple[str, str, str]] = set()

        for i in range(total):
            item = items.nth(i)
            try:
                author = (
                    await self._safe_text(item, self.SEL_AUTHOR)
                ) or "Anonymous"
                rating = (
                    await self._safe_attr(
                        item, self.SEL_RATING, "aria-label"
                    )
                ) or ""
                date = (await self._safe_text(item, self.SEL_DATE)) or ""
                text = (await self._safe_text(item, self.SEL_TEXT)) or ""

                key = (author, date, text)
                if key in seen:
                    logger.debug(f"Skipping duplicate review #{i}: {key[0]}")
                    continue
                seen.add(key)

                records.append(
                    ReviewRecord(
                        cafe_id=cafe.id,
                        cafe_name=cafe.name,
                        city=cafe.city,
                        author=author,
                        rating=rating,
                        date=date,
                        text=text,
                    )
                )
            except Exception as exc:
                logger.debug(f"Skip item #{i}: {exc}")

        return records

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    async def _safe_text(parent, selector: str) -> Optional[str]:
        try:
            loc = parent.locator(selector).first
            return (await loc.inner_text(timeout=1500)).strip()
        except Exception:
            return None

    @staticmethod
    async def _safe_attr(
        parent, selector: str, attr: str
    ) -> Optional[str]:
        try:
            loc = parent.locator(selector).first
            return await loc.get_attribute(attr, timeout=1500)
        except Exception:
            return None
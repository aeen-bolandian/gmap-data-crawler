"""
src/scraper.py
==============
Phase 2 — Review Extraction Engine.

Key insight 1: clicking a result link in Google Maps opens a SIDE
PANEL that has no Reviews tab. To get the Reviews tab we must
navigate to the result's href directly (page.goto), which loads the
full /maps/place/ page.

Key insight 2: Google Maps search results sometimes render the
result as a plain `a[href*="/maps/place/"]` link (no feed wrapper).
We accept both layouts.

Key insight 3: The coordinates in the clicked result's href are the
ONES WE TRUST — not the page URL, because Google Maps keeps the URL
on /maps/search/... even after opening a place panel.
"""

from __future__ import annotations

import asyncio
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
_COORD_RE_URL = re.compile(r"/@(-?\d+\.\d+),(-?\d+\.\d+)")
_COORD_RE_PLACE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")

MAX_MATCH_DISTANCE_M = 300.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
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
    m = _COORD_RE_URL.search(url) or _COORD_RE_PLACE.search(url)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------
_STRIP_TOKENS = (
    "cafe", "کافه", "coffee", "restaurant", "رستوران", "کافهرستوران",
    "کافه رستوران", "کافې", "کافە",
)


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name).lower()
    s = s.replace("\u064A", "\u06CC").replace("\u0643", "\u06A9")
    s = re.sub(r"[\u064B-\u065F\u0670]", "", s)
    s = s.replace("\u200C", " ").replace("\u200D", " ")
    for tok in _STRIP_TOKENS:
        s = s.strip()
        if s.startswith(tok + " "):
            s = s[len(tok) + 1 :]
        if s.endswith(" " + tok):
            s = s[: -len(tok) - 1]
    return re.sub(r"\s+", " ", s).strip()


def names_look_same(a: str, b: str) -> bool:
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
# Shared config
# ---------------------------------------------------------------------------
class ScraperConfig:
    SEL_REVIEW_ITEM = "div.jftiEf"
    SEL_REVIEW_ITEM_FALLBACK = "div[data-review-id]"

    SEL_MORE_BUTTON = (
        'button.w8nwRe.kyuRq, '
        'button[aria-label*="More" i], '
        'button[aria-label*="بیشتر" i]'
    )
    SEL_AUTHOR = ".d4r55, button[data-href*='/maps/contrib/'] .d4r55"
    SEL_RATING = (
        'span[role="img"][aria-label*="star" i], '
        'span[role="img"][aria-label*="ستاره" i], '
        'span.kvMYJc'
    )
    SEL_DATE = "span.rsqaWe"
    SEL_TEXT = "span.wiI7pd"

    SEL_PLACE_H1 = "h1"
    SEL_PLACE_TAB = 'button[role="tab"]'
    SEL_REVIEWS_TAB = (
        'button[role="tab"]:has-text("Reviews"), '
        'button[role="tab"]:has-text("نظرات"), '
        'button[aria-label*="Reviews for" i], '
        'button[aria-label*="نظرات" i]'
    )
    SEL_FEED = 'div[role="feed"]'
    SEL_RESULT_LINK = 'a[href*="/maps/place/"]'

    MAX_RESULTS_TO_TRY = 20

    PAGE_LOAD_TIMEOUT_MS = 60_000
    POST_GOTO_SETTLE_MS = 5_000
    POST_TAB_CLICK_SETTLE_MS = 2_000
    POST_MORE_EXPAND_SETTLE_MS = 300
    PLACE_PAGE_WAIT_MS = 30_000
    REVIEW_CARDS_WAIT_MS = 30_000

    SCROLL_STEP_PX = 3_000
    SCROLL_MAX_ITERATIONS = 25
    SCROLL_WAIT_FOR_NEW_CARD_MS = 2_500
    SCROLL_MAX_STAGNANT = 3

    CONSENT_SELECTORS = (
        'button:has-text("Accept all")',
        'button:has-text("Reject all")',
        'button[aria-label*="Accept" i]',
        'form[action*="consent"] button',
    )


# ---------------------------------------------------------------------------
# ScraperWorker
# ---------------------------------------------------------------------------
class ScraperWorker:
    def __init__(
        self,
        context: BrowserContext,
        worker_id: int,
        page_pool: asyncio.Queue,
        max_reviews: int = 150,
    ) -> None:
        self.context = context
        self.worker_id = worker_id
        self.max_reviews = max_reviews
        self._pool = page_pool
        self._page: Optional[Page] = None
        self._owns_page = False
        self._last_clicked_coords: Optional[tuple[float, float]] = None

    async def ensure_page(self) -> Page:
        if self._page is not None and not self._page.is_closed():
            return self._page
        try:
            self._page = self._pool.get_nowait()
            self._owns_page = False
            logger.debug(f"[w{self.worker_id}] Claimed existing tab.")
            return self._page
        except asyncio.QueueEmpty:
            pass
        self._page = await self.context.new_page()
        await stealth_async(self._page)
        self._owns_page = True
        logger.warning(
            f"[w{self.worker_id}] Page pool empty — created new tab."
        )
        return self._page

    async def close(self) -> None:
        if self._page is None or self._page.is_closed():
            self._page = None
            return
        if self._owns_page:
            try:
                await self._page.close()
            except Exception:
                pass
        else:
            try:
                self._pool.put_nowait(self._page)
            except Exception:
                pass
        self._page = None

    async def reset_page(self) -> None:
        await self.close()
        self._page = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def scrape(
        self, target: ResolvedTarget
    ) -> list[ReviewRecord]:
        page = await self.ensure_page()
        cafe = target.cafe
        self._last_clicked_coords = None

        try:
            await page.bring_to_front()
            logger.debug(
                f"[w{self.worker_id}][{cafe.id}] Opening URL: {target.url}"
            )
            await page.goto(
                target.url,
                timeout=ScraperConfig.PAGE_LOAD_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(
                ScraperConfig.POST_GOTO_SETTLE_MS
            )

            await self._handle_consent(page, cafe)

            if not await self._open_correct_place(page, cafe):
                logger.warning(
                    f"[w{self.worker_id}][{cafe.id}] Could not find "
                    f"matching place — skipping."
                )
                return []

            logger.info(
                f"[w{self.worker_id}][{cafe.id}] Reviews tab opened. "
                f"Scrolling..."
            )
            await self._scroll_reviews_container(page)
            await self._expand_all_more_buttons(page)

            reviews = await self._extract_reviews(page, cafe)
            if reviews:
                logger.success(
                    f"[w{self.worker_id}][{cafe.id}] {cafe.name} → "
                    f"Extracted {len(reviews)} reviews"
                )
            return reviews

        except Exception as exc:
            logger.exception(
                f"[w{self.worker_id}][{cafe.id}] Unexpected failure: {exc}"
            )
            return []

    # ------------------------------------------------------------------
    # Consent
    # ------------------------------------------------------------------
    async def _handle_consent(
        self, page: Page, cafe: CafeRecord
    ) -> None:
        try:
            if "consent.google.com" not in page.url:
                return
            for sel in ScraperConfig.CONSENT_SELECTORS:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(
                        ScraperConfig.POST_TAB_CLICK_SETTLE_MS
                    )
                    return
        except Exception as exc:
            logger.debug(
                f"[w{self.worker_id}][{cafe.id}] Consent warning: {exc}"
            )

    # ------------------------------------------------------------------
    # Open the correct place
    # ------------------------------------------------------------------
    async def _detect_page_type(self, page: Page) -> Optional[str]:
        """
        Return one of "tabs", "links", "feed", or None.
        Polls for up to PLACE_PAGE_WAIT_MS.
        """
        import time
        start = time.monotonic()
        deadline = start + ScraperConfig.PLACE_PAGE_WAIT_MS / 1000

        while time.monotonic() < deadline:
            try:
                if await page.locator(ScraperConfig.SEL_PLACE_TAB).count() > 0:
                    return "tabs"
                if await page.locator(ScraperConfig.SEL_RESULT_LINK).count() > 0:
                    return "links"
                if await page.locator(ScraperConfig.SEL_FEED).count() > 0:
                    return "feed"
            except Exception:
                pass
            await page.wait_for_timeout(500)
        return None

    async def _open_correct_place(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        signal = await self._detect_page_type(page)
        if signal is None:
            logger.warning(
                f"[w{self.worker_id}][{cafe.id}] No place tab, feed, or "
                f"result link appeared within "
                f"{ScraperConfig.PLACE_PAGE_WAIT_MS // 1000}s."
            )
            return False

        logger.debug(
            f"[w{self.worker_id}][{cafe.id}] Page signal: {signal}"
        )

        # --- Place page: use it directly (validate + click reviews) ---
        if signal == "tabs":
            if not await self._validate_place_match(page, cafe):
                return False
            return await self._click_and_wait_reviews(page, cafe)

        # --- Results list: find the result whose coords match ---------
        if not await self._try_results_list(page, cafe):
            return False

        # After navigating to the result's place URL, we should now be
        # on a place page with tabs. Wait for tabs and validate.
        try:
            await page.wait_for_selector(
                ScraperConfig.SEL_PLACE_TAB,
                timeout=ScraperConfig.PLACE_PAGE_WAIT_MS,
                state="attached",
            )
        except Exception:
            logger.warning(
                f"[w{self.worker_id}][{cafe.id}] After navigating to "
                f"result URL, no place tab appeared."
            )
            return False

        if not await self._validate_place_match(page, cafe):
            return False

        return await self._click_and_wait_reviews(page, cafe)

    async def _try_results_list(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        """
        Find the result whose href coords match the cafe, then navigate
        to that URL with page.goto (NOT link.click, which opens a side
        panel without a Reviews tab).
        """
        if cafe.latitude is None or cafe.longitude is None:
            return False

        links = page.locator(ScraperConfig.SEL_RESULT_LINK)
        count = await links.count()
        logger.debug(
            f"[w{self.worker_id}][{cafe.id}] Trying "
            f"{min(count, ScraperConfig.MAX_RESULTS_TO_TRY)} result links "
            f"(of {count}) for coordinate match."
        )

        for i in range(min(count, ScraperConfig.MAX_RESULTS_TO_TRY)):
            try:
                link = links.nth(i)
                href = await link.get_attribute("href") or ""
                coords = extract_coords_from_url(href)
                if not coords:
                    continue
                dist = haversine_m(
                    cafe.latitude, cafe.longitude, *coords
                )
                if dist <= MAX_MATCH_DISTANCE_M:
                    logger.debug(
                        f"[w{self.worker_id}][{cafe.id}] Result #{i} "
                        f"matches ({dist:.1f}m) — navigating to href."
                    )
                    self._last_clicked_coords = coords
                    await page.goto(
                        href,
                        timeout=ScraperConfig.PAGE_LOAD_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )
                    await page.wait_for_timeout(
                        ScraperConfig.POST_GOTO_SETTLE_MS
                    )
                    return True
            except Exception as exc:
                logger.debug(
                    f"[w{self.worker_id}][{cafe.id}] Result #{i} failed: "
                    f"{exc}"
                )
                continue

        logger.warning(
            f"[w{self.worker_id}][{cafe.id}] No result matched the cafe "
            f"coordinates within {MAX_MATCH_DISTANCE_M}m."
        )
        return False

    async def _click_and_wait_reviews(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        clicked = await self._click_reviews_tab(page, cafe)
        if not clicked:
            await page.wait_for_timeout(
                ScraperConfig.POST_TAB_CLICK_SETTLE_MS
            )
            clicked = await self._click_reviews_tab(page, cafe)
        if not clicked:
            logger.warning(
                f"[w{self.worker_id}][{cafe.id}] Reviews tab could not "
                f"be clicked."
            )
            return False
        return await self._wait_for_review_cards(page, cafe)

    async def _click_reviews_tab(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        tab = page.locator(ScraperConfig.SEL_REVIEWS_TAB).first
        if await tab.count() == 0:
            return False
        try:
            selected = await tab.get_attribute("aria-selected")
        except Exception:
            selected = None
        if selected == "true":
            return True
        if not await tab.is_visible():
            return False
        try:
            await tab.click()
            await page.wait_for_timeout(
                ScraperConfig.POST_TAB_CLICK_SETTLE_MS
            )
            return True
        except Exception:
            return False

    async def _wait_for_review_cards(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        try:
            await page.wait_for_selector(
                ScraperConfig.SEL_REVIEW_ITEM,
                timeout=ScraperConfig.REVIEW_CARDS_WAIT_MS,
                state="attached",
            )
            count = await self._count_review_cards(page)
            logger.debug(
                f"[w{self.worker_id}][{cafe.id}] Review feed ready: "
                f"{count} cards."
            )
            return True
        except Exception:
            logger.warning(
                f"[w{self.worker_id}][{cafe.id}] Review cards never appeared."
            )
            return False

    # ------------------------------------------------------------------
    # Place validation
    # ------------------------------------------------------------------
    async def _validate_place_match(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        # If we navigated to a result whose coords already matched, trust them.
        if self._last_clicked_coords is not None:
            url_lat, url_lng = self._last_clicked_coords
            dist = haversine_m(
                cafe.latitude, cafe.longitude, url_lat, url_lng
            )
            if dist <= MAX_MATCH_DISTANCE_M:
                logger.debug(
                    f"[w{self.worker_id}][{cafe.id}] Place matched by "
                    f"result coords (dist={dist:.1f}m)."
                )
                return True
            logger.warning(
                f"[w{self.worker_id}][{cafe.id}] Result coords do NOT "
                f"match: dist={dist:.1f}m."
            )
            return False

        # Otherwise read coords from page URL.
        coords = extract_coords_from_url(page.url)
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
                    f"[w{self.worker_id}][{cafe.id}] Place matched by "
                    f"URL coords (dist={dist:.1f}m)."
                )
                return True
            logger.warning(
                f"[w{self.worker_id}][{cafe.id}] Place does NOT match: "
                f"db=({cafe.latitude:.5f},{cafe.longitude:.5f}) "
                f"page=({url_lat:.5f},{url_lng:.5f}) dist={dist:.1f}m."
            )
            return False

        # Name fallback.
        page_name = ""
        try:
            h1 = page.locator(ScraperConfig.SEL_PLACE_H1).first
            if await h1.count() > 0:
                page_name = (await h1.inner_text()).strip()
        except Exception:
            pass

        if page_name and names_look_same(cafe.name, page_name):
            return True

        logger.warning(
            f"[w{self.worker_id}][{cafe.id}] Place does NOT match (no "
            f"coords, name mismatch)."
        )
        return False

    # ------------------------------------------------------------------
    # Review-item helpers
    # ------------------------------------------------------------------
    async def _review_item_locator(self, page: Page):
        primary = page.locator(ScraperConfig.SEL_REVIEW_ITEM)
        if await primary.count() > 0:
            return primary
        return page.locator(ScraperConfig.SEL_REVIEW_ITEM_FALLBACK)

    async def _count_review_cards(self, page: Page) -> int:
        locator = await self._review_item_locator(page)
        return await locator.count()

    # ------------------------------------------------------------------
    # Scrolling & expansion
    # ------------------------------------------------------------------
    async def _scroll_reviews_container(self, page: Page) -> None:
        last_count = 0
        stagnant = 0
        for _ in range(ScraperConfig.SCROLL_MAX_ITERATIONS):
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
                ScraperConfig.SCROLL_STEP_PX,
            )
            try:
                await page.wait_for_function(
                    """(prev) => {
                        return document.querySelectorAll(
                            'div.jftiEf, div[data-review-id]'
                        ).length > prev;
                    }""",
                    arg=last_count,
                    timeout=ScraperConfig.SCROLL_WAIT_FOR_NEW_CARD_MS,
                )
            except Exception:
                stagnant += 1
                if stagnant >= ScraperConfig.SCROLL_MAX_STAGNANT:
                    break
                continue
            current_count = await self._count_review_cards(page)
            if current_count >= self.max_reviews:
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
                f"[w{self.worker_id}] Expanded {clicked} 'More' buttons."
            )
        except Exception as exc:
            logger.debug(
                f"[w{self.worker_id}] Batch 'More' click failed: {exc}"
            )
        await page.wait_for_timeout(
            ScraperConfig.POST_MORE_EXPAND_SETTLE_MS
        )

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
                    await self._safe_text(item, ScraperConfig.SEL_AUTHOR)
                ) or "Anonymous"
                rating = (
                    await self._safe_attr(
                        item, ScraperConfig.SEL_RATING, "aria-label"
                    )
                ) or ""
                date = (
                    await self._safe_text(item, ScraperConfig.SEL_DATE)
                ) or ""
                text = (
                    await self._safe_text(item, ScraperConfig.SEL_TEXT)
                ) or ""
                key = (author, date, text)
                if key in seen:
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
                logger.debug(
                    f"[w{self.worker_id}][{cafe.id}] Skip item #{i}: {exc}"
                )
        return records

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


# ---------------------------------------------------------------------------
# GoogleMapsScraper
# ---------------------------------------------------------------------------
class GoogleMapsScraper:
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
        self._owns_browser = False
        self._page_pool: Optional[asyncio.Queue] = None

    @property
    def context(self) -> BrowserContext:
        assert self._context is not None
        return self._context

    @property
    def page_pool(self) -> asyncio.Queue:
        assert self._page_pool is not None
        return self._page_pool

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

        self._page_pool = asyncio.Queue()
        for p in self._context.pages:
            if p.is_closed():
                continue
            if p.url.startswith(("about:", "chrome://", "devtools://")):
                continue
            if "google.com/maps" in p.url:
                self._page_pool.put_nowait(p)
        pool_size = self._page_pool.qsize()
        if pool_size:
            logger.info(
                f"Reusing {pool_size} existing Maps tab(s) from Chrome."
            )
        else:
            logger.warning(
                "No existing Maps tabs found. Workers will create fresh "
                "tabs, which often load blank in a CDP session."
            )
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
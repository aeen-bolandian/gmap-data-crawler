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
"""

from __future__ import annotations

import asyncio
import random
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
# Scraper
# ---------------------------------------------------------------------------
class GoogleMapsScraper:
    # --- Selectors ---------------------------------------------------------
    # `data-review-id` is the most stable anchor Google exposes.
    SEL_REVIEW_ITEM = "div.jftiEf, div[data-review-id]"
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

    # --- Timing configuration -------------------------------------------
    # Centralized here so later perf optimizations only touch numbers,
    # not control flow. Values match the v1.0.0 hardcoded defaults.
    PAGE_LOAD_TIMEOUT_MS = 60_000
    POST_GOTO_SETTLE_MS = 5_000
    POST_TAB_CLICK_SETTLE_MS = 2_000
    POST_FIRST_RESULT_SETTLE_MS = 3_000
    SIGNAL_POLL_INTERVAL_MS = 1_000
    SIGNAL_POLL_MAX_ATTEMPTS = 20
    SCROLL_STEP_PX = 3_000
    WINDOW_SCROLL_STEP_PX = 1_500
    SCROLL_MAX_ITERATIONS = 25
    SCROLL_SLEEP_MIN = 1.5
    SCROLL_SLEEP_MAX = 2.5
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

            # Pick the most useful existing tab to reuse.
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
        # Only close the browser if we launched it; a CDP browser belongs
        # to the user and must survive our shutdown.
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

        # Reuse the existing (logged-in) tab if possible.
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
            # Give Google Maps JS a moment to hydrate.
            await page.wait_for_timeout(self.POST_GOTO_SETTLE_MS)

            # Handle a consent page if it appears mid-flight.
            await self._handle_consent(page, target.cafe)

            if not await self._open_reviews_tab(page, target.cafe):
                logger.warning(
                    f"[{target.cafe.id}] Could not switch to Reviews tab."
                )
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
            # Never close the tab the user gave us.
            if page is not self._existing_page:
                try:
                    await page.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Tab selection & consent
    # ------------------------------------------------------------------
    def _pick_existing_page(self) -> Optional[Page]:
        """
        Pick the best existing tab to reuse.

        Preference order:
        1. A tab already on google.com/maps with a place card loaded.
        2. A tab already on google.com/maps.
        3. The first non-blank tab.
        """
        if not self._context or not self._context.pages:
            return None

        pages = list(self._context.pages)

        # Skip obviously useless tabs (about:blank, chrome://, etc.).
        candidates = [
            p
            for p in pages
            if not p.is_closed() and not p.url.startswith(
                ("about:", "chrome://", "devtools://")
            )
        ]
        if not candidates:
            return None

        # Prefer a Maps tab.
        maps_tabs = [
            p for p in candidates if "google.com/maps" in p.url
        ]
        if maps_tabs:
            return maps_tabs[0]

        return candidates[0]

    async def _handle_consent(self, page: Page, cafe: CafeRecord) -> None:
        """Dismiss Google's consent banner if it appeared."""
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
    # Reviews tab discovery
    # ------------------------------------------------------------------
    async def _open_reviews_tab(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        # Quick exit: if we already have review cards, we're done.
        if await page.locator(self.SEL_REVIEW_ITEM).count() > 0:
            return True

        # Wait for one of the known signals.
        signals = [
            "h1",                         # place card
            'div[role="feed"]',           # search results
            'button[role="tab"]',         # review tab present
            "div.jftiEf",                 # review cards (already loaded)
        ]

        for _ in range(self.SIGNAL_POLL_MAX_ATTEMPTS):
            found_signal: Optional[str] = None
            for sel in signals:
                try:
                    if await page.locator(sel).count() > 0:
                        found_signal = sel
                        break
                except Exception:
                    continue
            if found_signal is not None:
                logger.debug(
                    f"[{cafe.id}] Signal found: {found_signal}"
                )
                break
            await page.wait_for_timeout(self.SIGNAL_POLL_INTERVAL_MS)

        # If we have a place card, click the Reviews tab.
        if await page.locator("h1").count() > 0:
            review_tab = page.locator(
                'button[role="tab"]:has-text("Reviews"), '
                'button[role="tab"]:has-text("نظرات"), '
                'button[aria-label*="Reviews for" i]'
            ).first
            if (
                await review_tab.count() > 0
                and await review_tab.is_visible()
            ):
                await review_tab.click()
                logger.debug(f"[{cafe.id}] Clicked Reviews tab.")
                await page.wait_for_timeout(
                    self.POST_TAB_CLICK_SETTLE_MS
                )

        # If we have a search feed, click the first result.
        elif await page.locator('div[role="feed"]').count() > 0:
            first_link = page.locator(
                'div[role="feed"] a[href*="/maps/place/"], a.hfpxzc'
            ).first
            if await first_link.count() > 0:
                await first_link.click()
                logger.debug(f"[{cafe.id}] Clicked first result.")
                await page.wait_for_timeout(
                    self.POST_FIRST_RESULT_SETTLE_MS
                )
                review_tab = page.locator(
                    'button[role="tab"]:has-text("Reviews"), '
                    'button[role="tab"]:has-text("نظرات")'
                ).first
                if await review_tab.count() > 0:
                    await review_tab.click()
                    await page.wait_for_timeout(
                        self.POST_TAB_CLICK_SETTLE_MS
                    )

        # Confirm review cards are present.
        for _ in range(5):
            if await page.locator(self.SEL_REVIEW_ITEM).count() > 0:
                return True
            await page.wait_for_timeout(self.SIGNAL_POLL_INTERVAL_MS)

        # Fallback: click the rating summary.
        rating_btn = page.locator(
            'button[aria-label*="reviews" i], '
            'button[aria-label*="نظرات" i]'
        ).first
        if await rating_btn.count() > 0:
            try:
                await rating_btn.click()
                await page.wait_for_timeout(
                    self.POST_TAB_CLICK_SETTLE_MS
                )
                return (
                    await page.locator(self.SEL_REVIEW_ITEM).count() > 0
                )
            except Exception:
                pass

        return False

    # ------------------------------------------------------------------
    # Scrolling & expansion
    # ------------------------------------------------------------------
    async def _scroll_reviews_container(self, page: Page) -> None:
        last_count = 0
        stagnant = 0

        for _ in range(self.SCROLL_MAX_ITERATIONS):
            # Walk up to the first ancestor that actually overflows; this
            # avoids the brittle `div.m6QErb` class dependency.
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

            await asyncio.sleep(
                random.uniform(
                    self.SCROLL_SLEEP_MIN, self.SCROLL_SLEEP_MAX
                )
            )
            current_count = await page.locator(
                self.SEL_REVIEW_ITEM
            ).count()

            if current_count >= self.max_reviews:
                logger.debug(
                    f"Reached review cap ({self.max_reviews})."
                )
                break

            if current_count == last_count:
                stagnant += 1
                if stagnant >= 3:
                    break
            else:
                stagnant = 0
                last_count = current_count

    async def _expand_all_more_buttons(self, page: Page) -> None:
        buttons = page.locator(self.SEL_MORE_BUTTON)
        count = await buttons.count()
        limit = min(count, self.max_reviews)
        for i in range(limit):
            try:
                await buttons.nth(i).click(
                    timeout=self.MORE_BUTTON_CLICK_TIMEOUT_MS
                )
                await asyncio.sleep(0.05)
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    async def _extract_reviews(
        self, page: Page, cafe: CafeRecord
    ) -> list[ReviewRecord]:
        items = page.locator(self.SEL_REVIEW_ITEM)
        total = min(await items.count(), self.max_reviews)
        records: list[ReviewRecord] = []
        seen: set[tuple[str, str, str]] = set()  # (author, date, text)

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
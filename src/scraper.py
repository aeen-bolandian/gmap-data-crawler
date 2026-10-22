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

    # --- Place-page detection --------------------------------------------
    # A "real" place page always has an <h1> and a Reviews tab. The
    # preview cards that appear before opening the Reviews tab also
    # match SEL_REVIEW_ITEM, so we cannot use that alone to decide
    # whether the place page is ready.
    SEL_PLACE_H1 = "h1"
    SEL_PLACE_TAB = 'button[role="tab"]'
    SEL_REVIEWS_TAB = (
        'button[role="tab"]:has-text("Reviews"), '
        'button[role="tab"]:has-text("نظرات"), '
        'button[aria-label*="Reviews for" i], '
        'button[aria-label*="نظرات" i]'
    )
    SEL_FEED = 'div[role="feed"]'
    SEL_FIRST_RESULT = 'div[role="feed"] a[href*="/maps/place/"], a.hfpxzc'

    # --- Timing configuration -------------------------------------------
    # Centralized here so later perf optimizations only touch numbers,
    # not control flow. Values match the v1.0.0 hardcoded defaults.
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
    # Max time to wait for ONE new review card after a scroll step.
    # If a new card shows up faster, we continue immediately.
    SCROLL_WAIT_FOR_NEW_CARD_MS = 2_500
    # Stop after N consecutive scrolls that produce no new cards.
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
        """
        Make sure the Reviews tab is open and its card list is loaded.

        Important: preview review cards (`SEL_REVIEW_ITEM`) exist on the
        place page *before* the Reviews tab is clicked. So we cannot use
        them alone to decide the tab is open. We require a place-card
        <h1> and either an already-active Reviews tab or a successful
        click on it.
        """
        # ---- Step 1: wait for a real page signal --------------------
        signals = [
            self.SEL_PLACE_H1,   # place card (preferred)
            self.SEL_FEED,       # search results list (fallback)
        ]
        for _ in range(self.SIGNAL_POLL_MAX_ATTEMPTS):
            for sel in signals:
                try:
                    if await page.locator(sel).count() > 0:
                        logger.debug(
                            f"[{cafe.id}] Page signal found: {sel}"
                        )
                        break
                except Exception:
                    continue
            else:
                await page.wait_for_timeout(self.SIGNAL_POLL_INTERVAL_MS)
                continue
            break

        # ---- Step 2a: we are on a place page ------------------------
        if await page.locator(self.SEL_PLACE_H1).count() > 0:
            clicked = await self._click_reviews_tab(page, cafe)
            if not clicked:
                # Sometimes the place page needs a moment before the tab
                # becomes clickable. Retry once.
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

            # Confirm we now have real review cards (not previews).
            return await self._wait_for_review_cards(page, cafe)

        # ---- Step 2b: we are on a results list ----------------------
        if await page.locator(self.SEL_FEED).count() > 0:
            logger.debug(
                f"[{cafe.id}] Results list detected — clicking first result."
            )
            first_link = page.locator(self.SEL_FIRST_RESULT).first
            if await first_link.count() > 0:
                try:
                    await first_link.click()
                    await page.wait_for_timeout(
                        self.POST_FIRST_RESULT_SETTLE_MS
                    )
                except Exception as exc:
                    logger.debug(
                        f"[{cafe.id}] First result click failed: {exc}"
                    )
                    return False

                if await page.locator(self.SEL_PLACE_H1).count() > 0:
                    clicked = await self._click_reviews_tab(page, cafe)
                    if clicked:
                        return await self._wait_for_review_cards(
                            page, cafe
                        )

        logger.warning(
            f"[{cafe.id}] No place page and no results list after "
            f"waiting — nothing to scrape."
        )
        return False

    async def _click_reviews_tab(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        """Click the Reviews tab if it is not already active. Returns True
        when the tab is (or becomes) the active tab."""
        tab = page.locator(self.SEL_REVIEWS_TAB).first
        if await tab.count() == 0:
            return False

        # `aria-selected="true"` means the tab is already active.
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
        """Wait until the review feed is rendered with its scroll container."""
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
    # Review-item locator helpers
    # ------------------------------------------------------------------
    async def _review_item_locator(self, page: Page):
        """
        Return a locator for review cards.

        Prefer `div.jftiEf` because it is the card class itself. Only
        fall back to `div[data-review-id]` when no `jftiEf` element is
        present — on some UI variants the wrapper also carries
        `data-review-id`, so matching both via a comma-selector would
        count every review twice.
        """
        primary = page.locator(self.SEL_REVIEW_ITEM)
        if await primary.count() > 0:
            return primary
        return page.locator(self.SEL_REVIEW_ITEM_FALLBACK)

    async def _count_review_cards(self, page: Page) -> int:
        """Convenience wrapper — one await instead of two."""
        locator = await self._review_item_locator(page)
        return await locator.count()

    # ------------------------------------------------------------------
    # Scrolling & expansion
    # ------------------------------------------------------------------
    async def _scroll_reviews_container(self, page: Page) -> None:
        """
        Scroll the reviews pane until either the review cap is reached
        or no new cards appear after a few scroll attempts.

        Unlike the v1.0.0 implementation, this does NOT sleep for a
        fixed 1.5-2.5s after each scroll. Instead it waits up to
        SCROLL_WAIT_FOR_NEW_CARD_MS for a new review card to appear,
        returning immediately when one does. On fast loads this cuts
        the scroll phase from ~60s to ~15s for a 150-review cafe.
        """
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

            # Wait for at least one NEW card to appear (event-driven).
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
                # Timeout — no new cards after this scroll step.
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
        """
        Click every 'More' button in the review feed in a single JS
        round-trip.

        The previous implementation called `.click()` once per button
        through Playwright, which costs ~50-100ms per button and could
        take up to 2 minutes for 150 reviews. A single page.evaluate()
        collapses that to a few hundred milliseconds.
        """
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

        # Give the DOM a moment to swap truncated text with full text.
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
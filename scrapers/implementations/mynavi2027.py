"""Mynavi 2027 新卒 (job.mynavi.jp) scraper.

Targets new-graduate design/creative positions from Mynavi's
shinsotsu (new graduate) recruitment platform.
"""

from __future__ import annotations

import logging

from playwright.async_api import Page, async_playwright
from playwright_stealth import Stealth

from models.enums import SourcePlatform
from models.job_posting import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger("job_hunter.scraper.mynavi2027")

_BASE_URL = "https://job.mynavi.jp"
_OCC_CODES = ["415", "580", "620"]
_MAX_PAGES = 2          # pages per occupation (up to 100 cards each)
_NAV_TIMEOUT = 30_000   # ms — page navigation timeout
_RENDER_DELAY_MS = 1_500  # ms — wait after DOM ready for JS render

_OCC_LABELS = {
    "415": "WEBデザイナー",
    "580": "グラフィックデザイナー",
    "620": "広告デザイナー",
}

# Selector that confirms job cards are present on the page.
_CARD_SELECTOR = 'h3 a[href*="/corp"][href*="employment"]'


class Mynavi2027Scraper(BaseScraper):
    """Scrape new-graduate design jobs from Mynavi 2027."""

    _user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    )

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.MYNAVI_2027

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def fetch_jobs(self) -> list[JobPosting]:
        """Scrape each occupation code with pagination."""
        all_jobs: list[JobPosting] = []
        seen_urls: set[str] = set()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self._headless)
            try:
                context = await browser.new_context(
                    locale="ja-JP",
                    timezone_id="Asia/Tokyo",
                    user_agent=self._user_agent,
                    viewport={"width": 1280, "height": 900},
                )
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)

                for occ in _OCC_CODES:
                    url = f"{_BASE_URL}/27/pc/search/occ{occ}.html"
                    logger.info("Scraping Mynavi2027 occ=%s", occ)
                    try:
                        occ_jobs = await self._scrape_occ(page, occ, url, seen_urls)
                        all_jobs.extend(occ_jobs)
                        logger.info(
                            "Mynavi2027 occ=%s done",
                            occ,
                            extra={"jobs": len(occ_jobs)},
                        )
                    except Exception:
                        logger.exception("Mynavi2027 occ=%s failed", occ)
                        continue
            finally:
                await browser.close()

        logger.info("Mynavi2027 scrape complete", extra={"total": len(all_jobs)})
        return all_jobs

    # ------------------------------------------------------------------
    # Per-occupation scraping with pagination
    # ------------------------------------------------------------------

    async def _scrape_occ(
        self,
        page: Page,
        occ: str,
        base_url: str,
        seen_urls: set[str],
    ) -> list[JobPosting]:
        jobs: list[JobPosting] = []

        await page.goto(base_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        await self._wait_for_cards(page)
        jobs.extend(await self._parse_page_with_context(page, occ, seen_urls))

        if _MAX_PAGES <= 1:
            return jobs

        for page_num in range(2, _MAX_PAGES + 1):
            nav_locator = page.locator(f'ul.pagingLink a:has-text("{page_num}")')
            if await nav_locator.count() == 0:
                logger.debug("No pagination link for page %d — stopping", page_num)
                break

            logger.debug("Mynavi2027 occ=%s → page %d", occ, page_num)
            await nav_locator.first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=_NAV_TIMEOUT)
            await self._wait_for_cards(page)

            page_jobs = await self._parse_page_with_context(page, occ, seen_urls)
            if not page_jobs:
                break
            jobs.extend(page_jobs)

        return jobs

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_cards(self, page: Page) -> None:
        await page.wait_for_selector(_CARD_SELECTOR, state="attached", timeout=10_000)
        await page.wait_for_timeout(_RENDER_DELAY_MS)

    # ------------------------------------------------------------------
    # Card extraction (batch via page.evaluate)
    # ------------------------------------------------------------------

    async def _parse_page_with_context(
        self,
        page: Page,
        occ_code: str,
        seen_urls: set[str],
    ) -> list[JobPosting]:
        """Extract company cards from the current page via one JS round-trip.

        This is intentionally NOT ``parse_page(page)`` — it requires
        extra context (occupation code and dedup set) that the base
        class contract does not provide.
        """
        occ_label = _OCC_LABELS.get(occ_code, occ_code)

        raw_items: list[dict] = await page.evaluate(
            """(occLabel) => {
                const results = [];
                const seen = new Set();
                const links = document.querySelectorAll(
                    'h3 a[href*="/corp"][href*="employment"]'
                );
                for (const link of links) {
                    try {
                        const href = link.getAttribute('href');
                        if (!href) continue;

                        const idMatch = href.match(/corp(\\d+)/);
                        if (!idMatch) continue;
                        const corpId = idMatch[1];
                        if (seen.has(corpId)) continue;
                        seen.add(corpId);

                        const company = link.textContent.trim();

                        // Find parent card container
                        const card = link.closest('.boxSearchbox')
                                  || link.closest('li')
                                  || link.closest('[class*="corp"]')
                                  || link.closest('[class*="company"]');

                        // Extract card text, collapse whitespace
                        const cardText = card ? card.innerText.replace(/\\s+/g, ' ').trim() : '';

                        // Build title: company name + excerpt from card description
                        const title = company + " | " + cardText.substring(0, 400);

                        const location = /東京/.test(cardText) ? 'Tokyo' : 'Japan';

                        const url = href.startsWith('http')
                            ? href.split('?')[0]
                            : 'https://job.mynavi.jp' + href.split('?')[0];

                        results.push({
                            corpId,
                            title,
                            company,
                            url,
                            location,
                        });
                    } catch (e) { /* skip malformed card */ }
                }
                return results;
            }""",
            occ_label,
        )

        cards: list[JobPosting] = []
        for item in raw_items:
            try:
                url = str(item["url"])

                if url in seen_urls:
                    continue
                seen_urls.add(url)

                # Title filter checks the full card text for relevance.
                # ``matches`` honours a custom --keyword when one was given,
                # otherwise falls back to the built-in design filter.
                title = str(item["title"])
                if not self.matches(title):
                    logger.debug("Skipping non-matching card: %s", str(item["company"]))
                    continue

                # Store a clean title: Company (Occupation Category)
                clean_title = f"{str(item['company'])[:100]} ({occ_label})"

                cards.append(JobPosting(
                    title=clean_title,
                    company=str(item["company"])[:250],
                    url=url,  # type: ignore[arg-type]
                    location=str(item["location"])[:250],
                    source_platform=self.platform,
                ))
            except Exception:
                logger.debug("Failed to map card", exc_info=True)
                continue

        return cards
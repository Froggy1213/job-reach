"""Mynavi 2027 新卒 (job.mynavi.jp) — new-graduate design board.

Important behavioural note, carried over from the original project and worth
repeating because it surprises people: Mynavi is **occupation-code driven,
new-graduate only, and nationwide**. It ignores ``--location`` entirely and
can only best-effort-filter an arbitrary keyword, which is why the relevance
check runs against the whole card text rather than the visible title.

For any general or mid-career search, use Wantedly instead.
"""

from __future__ import annotations

from typing import Any

from ..domain import JobPosting, SourcePlatform
from ..errors import ScraperError
from ..logging_setup import get_logger
from .base import BaseScraper

logger = get_logger("scrapers.mynavi")

BASE_URL = "https://job.mynavi.jp"
CARD_SELECTOR = 'h3 a[href*="/corp"][href*="employment"]'

#: Occupation codes for the design categories this board exposes.
OCCUPATIONS: dict[str, str] = {
    "415": "WEBデザイナー",
    "580": "グラフィックデザイナー",
    "620": "広告デザイナー",
}

MAX_PAGES_PER_OCCUPATION = 2
CARD_WAIT_MS = 10_000
RENDER_SETTLE_MS = 1_500

_CARD_SCRIPT = """() => {
    const results = [];
    const seen = new Set();
    const links = document.querySelectorAll('h3 a[href*="/corp"][href*="employment"]');

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
            const card = link.closest('.boxSearchbox')
                      || link.closest('li')
                      || link.closest('[class*="corp"]')
                      || link.closest('[class*="company"]');
            const cardText = card ? card.innerText.replace(/\\s+/g, ' ').trim() : '';

            results.push({
                company,
                cardText,
                // The card text is where the role actually lives on this board,
                // so it is what the relevance filter must see.
                matchedText: company + ' | ' + cardText.substring(0, 400),
                location: /東京/.test(cardText) ? 'Tokyo' : 'Japan',
                url: href.startsWith('http')
                    ? href.split('?')[0]
                    : 'https://job.mynavi.jp' + href.split('?')[0],
            });
        } catch (e) { /* skip malformed card */ }
    }
    return results;
}"""


class Mynavi2027Scraper(BaseScraper):
    """Scrape new-graduate design postings from Mynavi 2027."""

    url_encodes_keyword = False  # no server-side keyword search on this board

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.MYNAVI_2027

    async def fetch_jobs(self) -> list[JobPosting]:
        """Scrape every configured occupation, tolerating per-occupation failure."""
        jobs: list[JobPosting] = []
        seen_urls: set[str] = set()
        failures: list[str] = []

        async with self.browser_page() as page:
            for code, label in OCCUPATIONS.items():
                url = f"{BASE_URL}/27/pc/search/occ{code}.html"
                try:
                    jobs.extend(await self._scrape_occupation(page, code, label, url, seen_urls))
                except Exception as exc:  # noqa: BLE001 — one category must not kill the run
                    logger.warning(
                        "Mynavi occupation failed",
                        extra={"occupation": code, "error": str(exc)},
                    )
                    failures.append(code)

        if failures and len(failures) == len(OCCUPATIONS):
            raise ScraperError(
                "every Mynavi occupation failed (site layout may have changed): "
                + ", ".join(failures)
            )
        logger.info("Mynavi scrape complete", extra={"jobs": len(jobs), "failed": failures})
        return jobs

    async def _scrape_occupation(
        self,
        page: Any,
        code: str,
        label: str,
        url: str,
        seen_urls: set[str],
    ) -> list[JobPosting]:
        """Scrape one occupation code, following the pager while it exists."""
        found: list[JobPosting] = []

        await self.goto(page, url)
        await self._wait_for_cards(page)
        found.extend(self._parse_cards(await page.evaluate(_CARD_SCRIPT), label, seen_urls))

        for page_number in range(2, MAX_PAGES_PER_OCCUPATION + 1):
            pager = page.locator(f'ul.pagingLink a:has-text("{page_number}")')
            if await pager.count() == 0:
                break
            await pager.first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
            await self._wait_for_cards(page)

            page_jobs = self._parse_cards(await page.evaluate(_CARD_SCRIPT), label, seen_urls)
            if not page_jobs:
                break
            found.extend(page_jobs)

        return found

    async def _wait_for_cards(self, page: Any) -> None:
        await page.wait_for_selector(CARD_SELECTOR, state="attached", timeout=CARD_WAIT_MS)
        await page.wait_for_timeout(RENDER_SETTLE_MS)

    def _parse_cards(
        self, raw_items: list[dict[str, Any]], label: str, seen_urls: set[str]
    ) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        for item in raw_items:
            try:
                url = str(item["url"])
                if url in seen_urls:
                    continue
                if not self.matches(str(item.get("matchedText") or item.get("company") or "")):
                    continue
                seen_urls.add(url)
                company = str(item.get("company") or "Unknown")
                jobs.append(
                    JobPosting(
                        title=f"{company[:100]} ({label})",
                        company=company,
                        url=url,
                        location=str(item.get("location") or "Japan"),
                        source_platform=self.platform,
                    )
                )
            except Exception:  # noqa: BLE001 — malformed card, keep going
                logger.debug("skipping malformed Mynavi card")
        return jobs

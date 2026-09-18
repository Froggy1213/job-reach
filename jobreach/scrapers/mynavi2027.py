"""Mynavi 2027 新卒 (job.mynavi.jp) — new-graduate design board.

Important behavioural note, carried over from the original project and worth
repeating because it surprises people: Mynavi is **occupation-code driven,
new-graduate only, and nationwide**. It ignores ``--location`` entirely and
can only best-effort-filter an arbitrary keyword, which is why the relevance
check runs against the whole card text rather than the visible title.

For any general or mid-career search, use Wantedly instead.

Implementation note: this board renders its results with JavaScript, so it needs
a browser — but not *this* process's browser. The page work is expressed as a
step list and handed to :meth:`BaseScraper.fetch`, which runs it either on
Scrapling (in its own interpreter) or on the plugin's Playwright venv. The
pagination step is what makes the vocabulary worth having: ``click`` with
``optional=True`` *is* "follow the pager while it exists", and it reads the same
on either backend.
"""

from __future__ import annotations

from typing import Any

from ..domain import JobPosting, SourcePlatform
from ..errors import ScraperError
from ..fetchers import click, evaluate, scroll, wait, wait_load, wait_selector
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

        for code, label in OCCUPATIONS.items():
            try:
                jobs.extend(await self._scrape_occupation(code, label, seen_urls))
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
        self, code: str, label: str, seen_urls: set[str]
    ) -> list[JobPosting]:
        """Scrape one occupation code, following the pager while it exists."""
        url = f"{BASE_URL}/27/pc/search/occ{code}.html"
        steps: list[dict[str, Any]] = [
            scroll(800, times=2, settle_ms=RENDER_SETTLE_MS),
            evaluate(_CARD_SCRIPT, "page1"),
        ]
        for page_number in range(2, MAX_PAGES_PER_OCCUPATION + 1):
            steps += [
                # Optional: on the last page there is simply no such link, and
                # that is not an error.
                click(f'ul.pagingLink a:has-text("{page_number}")', optional=True, settle_ms=800),
                wait_load("domcontentloaded"),
                wait_selector(CARD_SELECTOR, timeout_ms=CARD_WAIT_MS),
                wait(RENDER_SETTLE_MS),
                evaluate(_CARD_SCRIPT, f"page{page_number}"),
            ]

        result = await self.fetch(
            url, steps=steps, wait_selector=CARD_SELECTOR, timeout_ms=max(self.timeout_ms, 45_000)
        )

        found: list[JobPosting] = []
        for key in ("page1", *(f"page{n}" for n in range(2, MAX_PAGES_PER_OCCUPATION + 1))):
            cards = result.get(key)
            if not isinstance(cards, list):
                continue
            found.extend(self._parse_cards(cards, label, seen_urls))
        return found

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
                        description=str(item["cardText"]).strip() if item.get("cardText") else None,
                        # The title above is this scraper's own construction from
                        # the occupation code, so it says nothing about the job —
                        # every card under 415 carries デザイナー whatever the
                        # company does. Saying so is what stops a relevance
                        # filter from reading its own search term back as
                        # evidence and keeping the whole board.
                        title_is_synthetic=True,
                    )
                )
            except Exception:  # noqa: BLE001 — malformed card, keep going
                logger.debug("skipping malformed Mynavi card")
        return jobs

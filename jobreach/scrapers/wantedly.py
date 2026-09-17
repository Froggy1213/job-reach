"""Wantedly (ウォンテッドリー) — the general-purpose Japanese board.

Wantedly supports real server-side search, so this is the board that answers
arbitrary keyword queries in any language: ``--keyword "frontend engineer"``
goes into its ``q=`` parameter, ``--location`` into ``locations=``. With no
keyword the scraper falls back to Wantedly's design occupations, which is the
project's original default feed.

Markup notes (the fragile parts, kept deliberately explicit):

* Cards are ``<a href="/projects/{id}">``. The company link is a **sibling**
  of the project link, not a descendant — so the parser walks up to the card
  root before querying for it.
* Titles live in a nested ``<h2>/<h3>``, not always a direct child of the link.
* Location is only present as free text inside the card, so it is recovered by
  scanning for Tokyo ward names in Japanese and romaji.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlencode

from ..domain import JobPosting, SourcePlatform
from ..errors import ScraperError
from ..logging_setup import get_logger
from .base import BaseScraper

logger = get_logger("scrapers.wantedly")

BASE_URL = "https://www.wantedly.com"
DESIGN_OCCUPATIONS = "ui_ux_designer,web_designer,graphic_designer"

#: Two pages of ~50 cards is the sweet spot: enough coverage, bounded runtime.
MAX_PAGES = 2
PAGE_DELAY_SECONDS = 3.0
CARD_SELECTOR_TIMEOUT_MS = 12_000
RENDER_SETTLE_MS = 3_300

_CARD_SCRIPT = """() => {
    const results = [];
    const seen = new Set();
    const links = document.querySelectorAll('a[href^="/projects/"]');

    for (const link of links) {
        try {
            const href = link.getAttribute('href');
            if (!href) continue;
            // /projects/{digits} only — skip sub-pages like /projects/123/members
            if (!/^\\/projects\\/\\d+(\\/|\\?|$)/.test(href)) continue;

            const projectId = href.match(/\\/projects\\/(\\d+)/)[1];
            if (seen.has(projectId)) continue;
            seen.add(projectId);

            const linkText = link.textContent.trim();
            if (linkText.length < 10) continue;

            // The company <a> is a sibling of the project <a>, so query from
            // the shared card container rather than from the link itself.
            const card = link.closest('article')
                      || link.closest('[class*="Card"]')
                      || link.closest('[class*="card"]')
                      || link.closest('[class*="project"]')
                      || link.closest('li')
                      || link.parentElement?.parentElement?.parentElement
                      || link;

            let title = '';
            for (const tag of ['h2', 'h3', 'h4', 'h1']) {
                const heading = link.querySelector(tag);
                if (heading) { title = heading.textContent.trim(); break; }
            }
            if (!title || title.length < 3) title = linkText.substring(0, 200);

            let company = 'Unknown';
            const companyLink = card.querySelector('a[href^="/companies/"]');
            if (companyLink) {
                const text = companyLink.textContent.trim();
                if (text) company = text;
            }
            if (company === 'Unknown') {
                for (const img of card.querySelectorAll('img')) {
                    const alt = (img.getAttribute('alt') || '').trim();
                    if (alt.length >= 2 && !/^(logo|image|photo|icon|project)$/i.test(alt)) {
                        company = alt;
                        break;
                    }
                }
            }

            const cardText = card.textContent || '';
            const kanjiWards = ['渋谷','新宿','港区','千代田','目黒','品川','世田谷','中央区',
                                '文京','台東','墨田','江東','豊島','六本木','代々木','恵比寿',
                                '表参道','大手町','丸の内','秋葉原','赤坂','虎ノ門'];
            const romajiWards = ['Shibuya','Shinjuku','Minato','Chiyoda','Meguro',
                                 'Roppongi','Ebisu','Akasaka','Ginza','Harajuku'];

            let location = 'Tokyo';
            for (const ward of kanjiWards) {
                if (cardText.includes(ward)) { location = 'Tokyo, ' + ward; break; }
            }
            if (location === 'Tokyo') {
                for (const ward of romajiWards) {
                    if (cardText.includes(ward)) { location = 'Tokyo, ' + ward; break; }
                }
            }
            if (/フルリモート|完全リモート|Full Remote|remote/i.test(cardText)) {
                location = location === 'Tokyo' ? 'Remote (Tokyo base)' : location + ' / Remote';
            }

            results.push({
                title,
                company,
                url: 'https://www.wantedly.com' + href.split('?')[0],
                location,
            });
        } catch (e) { /* skip malformed card */ }
    }
    return results;
}"""


class WantedlyScraper(BaseScraper):
    """Scrape the Wantedly project feed."""

    url_encodes_keyword = True  # Wantedly filters by q= server-side

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.WANTEDLY

    async def fetch_jobs(self) -> list[JobPosting]:
        """Walk up to :data:`MAX_PAGES` result pages and merge the cards."""
        jobs: list[JobPosting] = []
        seen: set[str] = set()

        async with self.browser_page() as page:
            for page_number in range(1, MAX_PAGES + 1):
                url = self.build_page_url(page_number)
                try:
                    await self.goto(page, url)
                    await page.wait_for_selector(
                        'a[href^="/projects/"]',
                        state="attached",
                        timeout=CARD_SELECTOR_TIMEOUT_MS,
                    )
                    # React lazy-loads cards as they scroll into view.
                    await page.evaluate("window.scrollBy(0, 600)")
                    await page.wait_for_timeout(1_500)
                    await page.evaluate("window.scrollBy(0, 600)")
                    await page.wait_for_timeout(RENDER_SETTLE_MS)

                    page_jobs = self._parse_cards(await page.evaluate(_CARD_SCRIPT))
                except Exception as exc:  # noqa: BLE001 — one bad page ≠ failed run
                    if page_number == 1:
                        raise ScraperError(f"Wantedly page 1 failed: {exc}") from exc
                    logger.warning("Wantedly page failed, stopping", extra={"page": page_number})
                    break

                fresh = [job for job in page_jobs if job.url not in seen]
                if not fresh:
                    break  # pagination exhausted
                seen.update(job.url for job in fresh)
                jobs.extend(fresh)

                if page_number < MAX_PAGES:
                    await asyncio.sleep(PAGE_DELAY_SECONDS)

        logger.info("Wantedly scrape complete", extra={"jobs": len(jobs)})
        return jobs

    def _parse_cards(self, raw_items: list[dict[str, Any]]) -> list[JobPosting]:
        """Map the JS extraction output onto domain objects, skipping bad rows."""
        jobs: list[JobPosting] = []
        for item in raw_items:
            try:
                if not self.matches(str(item["title"])):
                    continue
                jobs.append(
                    JobPosting(
                        title=item["title"],
                        company=item.get("company") or "Unknown",
                        url=item["url"],
                        location=item.get("location") or "Tokyo",
                        source_platform=self.platform,
                    )
                )
            except Exception:  # noqa: BLE001 — malformed card, keep going
                logger.debug("skipping malformed Wantedly card")
        return jobs

    def build_page_url(self, page_number: int) -> str:
        """Build the search URL for *page_number*.

        A keyword becomes ``q=``; without one, Wantedly's design occupation
        codes are used. ``location`` maps to the ``locations`` slug, and the
        sentinel ``any`` omits the filter entirely.
        """
        params: dict[str, str | int] = {"type": "mixed", "page": page_number}
        if self.keyword:
            params["q"] = self.keyword
        else:
            params["occupations"] = DESIGN_OCCUPATIONS

        location = (self.location or "tokyo").lower()
        if location not in {"any", "all", ""}:
            params["locations"] = location

        return f"{BASE_URL}/projects?{urlencode(params)}"

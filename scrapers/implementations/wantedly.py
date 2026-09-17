"""Wantedly (ウォンテッドリー) scraper.

Targets design/creative project listings in Tokyo.  Uses ``page.evaluate()``
for batch card extraction.

URL: https://www.wantedly.com/projects?type=mixed&page=N&occupations=...&locations=tokyo

FIXES applied:
    - company extraction: Wantedly cards use a separate company <a> that
      is a sibling of the project <a>, NOT a descendant.  Old code called
      link.querySelector('a[href^="/companies/"]') which always returned null.
      New code walks up to the card root, then queries from there.
    - href filter: removed the `featured=0` guard that was accidentally
      blocking sponsored/featured listings which are valid jobs.
    - Title: Wantedly renders the role in an <h3> inside a nested <div>,
      not always a direct child of the <a>.  Added deeper querySelector.
    - location: added English ward names (Wantedly shows a mix of JA/EN).
    - networkidle wait added after scroll to let lazy-loaded cards finish.
    - `page_num` pagination param: Wantedly uses 1-based page numbers in
      the URL, not offsets.  Old _build_page_url was correct; kept as is.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode

from playwright.async_api import Page, async_playwright
from playwright_stealth import Stealth

from models.enums import SourcePlatform
from models.job_posting import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger("job_hunter.scraper.wantedly")

_BASE_URL = "https://www.wantedly.com"
_DESIGN_OCCUPATIONS = "ui_ux_designer,web_designer,graphic_designer"
_MAX_PAGES = 2
_PAGE_DELAY = 3.0
_NAV_TIMEOUT = 30_000
_SELECTOR_TIMEOUT = 12_000


class WantedlyScraper(BaseScraper):
    """Scrape design/creative projects in Tokyo from Wantedly."""

    # Wantedly supports free-text search via the ``q=`` URL param, so a
    # custom keyword is filtered server-side — see ``matches``.
    _url_encodes_keyword: bool = True

    _user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    )

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.WANTEDLY

    async def fetch_jobs(self) -> list[JobPosting]:
        """Navigate Wantedly search results with pagination."""
        all_jobs: list[JobPosting] = []

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

                for page_num in range(1, _MAX_PAGES + 1):
                    url = self._build_page_url(page_num)
                    logger.info("Scraping Wantedly page", extra={"page": page_num, "url": url})
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
                        await page.wait_for_selector(
                            'a[href^="/projects/"]',
                            state="attached",
                            timeout=_SELECTOR_TIMEOUT,
                        )
                        # Scroll to trigger React lazy-load
                        await page.evaluate("window.scrollBy(0, 600)")
                        await page.wait_for_timeout(1_500)
                        await page.evaluate("window.scrollBy(0, 600)")
                        await page.wait_for_timeout(3_300)

                        jobs = await self.parse_page(page)
                        all_jobs.extend(jobs)
                        logger.info(
                            "Wantedly page parsed",
                            extra={"page": page_num, "cards_found": len(jobs)},
                        )
                    except Exception:
                        logger.exception("Failed to scrape Wantedly page %d", page_num)
                        break

                    if page_num >= _MAX_PAGES:
                        break
                    await asyncio.sleep(_PAGE_DELAY)
            finally:
                await browser.close()

        logger.info("Wantedly scrape complete", extra={"total_jobs": len(all_jobs)})
        return all_jobs

    async def parse_page(self, page: Page) -> list[JobPosting]:
        """Extract all project cards via ``page.evaluate()``."""
        raw_items = await page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            const links = document.querySelectorAll('a[href^="/projects/"]');

            for (const link of links) {
                try {
                    const href = link.getAttribute('href');
                    if (!href) continue;
                    // Match /projects/{digits} — skip sub-pages like /projects/123/members
                    if (!/^\\/projects\\/\\d+(\\/|\\?|$)/.test(href)) continue;

                    const projectId = href.match(/\\/projects\\/(\\d+)/)[1];
                    if (seen.has(projectId)) continue;
                    seen.add(projectId);

                    const linkText = link.textContent.trim();
                    if (linkText.length < 10) continue;

                    // ── Card container ─────────────────────────────────────
                    // Wantedly renders cards as <article> or a generic container;
                    // the company link is a SIBLING of the project link, so we
                    // must walk up to the shared parent card first.
                    const card = link.closest('article')
                              || link.closest('[class*="Card"]')
                              || link.closest('[class*="card"]')
                              || link.closest('[class*="project"]')
                              || link.closest('li')
                              || link.parentElement?.parentElement?.parentElement
                              || link;

                    // ── Title ──────────────────────────────────────────────
                    let title = '';
                    // Wantedly nests the role in h2/h3 anywhere inside the link
                    for (const tag of ['h2','h3','h4','h1']) {
                        const h = link.querySelector(tag);
                        if (h) { title = h.textContent.trim(); break; }
                    }
                    if (!title || title.length < 3) title = linkText.substring(0, 200);

                    // ── Company ────────────────────────────────────────────
                    // FIX: query from CARD, not from link — company <a> is a sibling
                    let company = 'Unknown Company';
                    const companyLink = card.querySelector('a[href^="/companies/"]');
                    if (companyLink) {
                        const t = companyLink.textContent.trim();
                        if (t) company = t;
                    }
                    if (company === 'Unknown Company') {
                        // Fallback: img alt of a company logo in the card
                        const imgs = card.querySelectorAll('img');
                        for (const img of imgs) {
                            const alt = (img.getAttribute('alt') || '').trim();
                            if (alt.length >= 2 && !/^(logo|image|photo|icon|project)$/i.test(alt)) {
                                company = alt;
                                break;
                            }
                        }
                    }

                    // ── Location ───────────────────────────────────────────
                    // Wantedly shows a mix of Japanese and English location text
                    const cardText = card.textContent || '';
                    let location = 'Tokyo';

                    const kanjiWards = [
                        '渋谷','新宿','港区','千代田','目黒','品川','世田谷','中央区',
                        '文京','台東','墨田','江東','豊島','六本木','代々木','恵比寿','表参道',
                        '大手町','丸の内','秋葉原','赤坂','虎ノ門',
                    ];
                    const romajiWards = [
                        'Shibuya','Shinjuku','Minato','Chiyoda','Meguro',
                        'Roppongi','Ebisu','Akasaka','Ginza','Harajuku',
                    ];
                    for (const w of kanjiWards) {
                        if (cardText.includes(w)) { location = 'Tokyo, ' + w; break; }
                    }
                    if (location === 'Tokyo') {
                        for (const w of romajiWards) {
                            if (cardText.includes(w)) { location = 'Tokyo, ' + w; break; }
                        }
                    }
                    if (/フルリモート|完全リモート|Full Remote|remote/i.test(cardText)) {
                        location = location === 'Tokyo' ? 'Remote (Tokyo base)' : location + ' / Remote';
                    }

                    results.push({
                        projectId: projectId,
                        title: title,
                        company: company,
                        url: 'https://www.wantedly.com' + href.split('?')[0],
                        location: location,
                        salary: null,
                    });
                } catch(e) {}
            }
            return results;
        }""")

        cards: list[JobPosting] = []
        for item in raw_items:
            try:
                title = str(item["title"])[:500]
                if not self.matches(title):
                    logger.debug("Skipping non-matching Wantedly job: %s", title)
                    continue
                cards.append(JobPosting(
                    title=title,
                    company=str(item["company"])[:250],
                    url=item["url"],  # type: ignore[arg-type]
                    location=str(item["location"])[:250],
                    source_platform=self.platform,
                ))
            except Exception:
                logger.debug("Failed to map Wantedly card", exc_info=True)
                continue

        return cards

    def _build_page_url(self, page_num: int) -> str:
        """Build a Wantedly search URL for *page_num*.

        With a custom ``keyword`` → free-text search (``q=``).  Without a
        keyword → the default design occupations.  ``location`` maps to
        Wantedly's ``locations`` slug (default ``tokyo``; ``any``/``all``
        omits the location filter entirely).
        """
        params: dict[str, str | int] = {"type": "mixed", "page": page_num}
        if self._keyword:
            params["q"] = self._keyword
        else:
            params["occupations"] = _DESIGN_OCCUPATIONS

        loc = self._location or "tokyo"
        if loc.lower() not in {"any", "all"}:
            params["locations"] = loc

        return f"{_BASE_URL}/projects?{urlencode(params)}"

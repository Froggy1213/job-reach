"""Indeed Japan (jp.indeed.com) — the largest Japanese board, finally scrapable.

This board used to be **ingest-only**. Cloudflare refused headless clients, so
the honest answer was "a real browser fetches the cards, the agent feeds them to
``job_ingest``" — and that workflow still exists, because it is the right
fallback when automation is blocked. What changed is that it is no longer the
*only* way in: a stealth browser (Scrapling's patched Chromium, which solves
turnstile/interstitial challenges) reads the search page successfully, headless,
in about seven seconds.

Verified against the live site before this scraper was written:

* ``div.job_seen_beacon`` / ``div.cardOutline`` — one wrapper per card (16);
* ``a[data-jk]`` (class ``jcs-JobTitle``) — the title link, carrying the listing
  id in ``data-jk``, so **``jk`` is the dedup key** and the URL is rebuilt as
  ``/viewjob?jk=…`` rather than trusting the click-tracking href;
* ``[data-testid="company-name"]``, ``[data-testid="text-location"]``,
  ``.salary-snippet-container`` — company, location and salary, all best-effort
  (the layout shifts, so empty values are tolerated rather than fatal).

Three behavioural notes worth keeping:

* ``l=`` takes free text, so :data:`LOCATION_SLUGS` maps the plugin's slugs
  (``tokyo``) onto Japanese place names (``東京``) — Indeed's own market names.
* ``&hl=ja`` is always set: without it Indeed may serve ``www.indeed.com``,
  whose listings are American. The response's final host is checked and a
  non-Japanese host is a hard failure, never a silent import of US jobs.
* A challenge page (``Just a moment``, ``Ray ID``, …) means **zero results**.
  The scraper reports it as blocked; it must never look like "no jobs today".
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ..domain import JobPosting, SourcePlatform
from ..errors import ScraperError
from ..logging_setup import get_logger
from .base import BaseScraper

logger = get_logger("scrapers.indeed")

BASE_URL = "https://jp.indeed.com"
SEARCH_PATH = "/jobs"
VIEW_PATH = "/viewjob"

#: Query used when the caller supplies no keyword — this plugin's built-in
#: design feed, in the language the board indexes best.
DEFAULT_QUERY = "デザイナー"

#: Indeed's own market names for the slugs the plugin uses elsewhere. Anything
#: not listed is passed through untouched, so ``location="大阪"`` works too.
LOCATION_SLUGS: dict[str, str] = {
    "tokyo": "東京",
    "osaka": "大阪",
    "nagoya": "名古屋",
    "kyoto": "京都",
    "fukuoka": "福岡",
    "yokohama": "横浜",
    "kobe": "神戸",
    "sapporo": "札幌",
    "sendai": "仙台",
}

#: Sentinels meaning "no location filter".
ANYWHERE = {"any", "all", "", "japan"}

#: Pages of 15 cards. Two pages is 30 listings — plenty for a design search and
#: still a single browser session.
MAX_PAGES = 2
RESULTS_PER_PAGE = 15

#: Cards selector, used both as the readiness check and as the block probe: if
#: it never appears, the page was a challenge, not a result list.
CARD_SELECTOR = "a[data-jk], div.job_seen_beacon"

#: The extraction runs in the page. Kept as one script so the same code works on
#: both backends (Scrapling driver and the Playwright fallback).
CARD_SCRIPT = """() => {
    const out = [];
    const seen = new Set();

    const anchors = document.querySelectorAll('a[data-jk], a.jcs-JobTitle, a[href*="jk="]');
    for (const link of anchors) {
        try {
            let jk = link.getAttribute('data-jk') || '';
            if (!jk) {
                const m = (link.getAttribute('href') || '').match(/[?&]jk=([0-9a-zA-Z]+)/);
                jk = m ? m[1] : '';
            }
            if (!jk || seen.has(jk)) continue;

            const href = link.getAttribute('href') || '';
            // Skip cross-market links: an absolute US url is not a Japanese job.
            if (href.includes('www.indeed.com') || href.includes('//www.')) continue;
            seen.add(jk);

            const card = link.closest('div.job_seen_beacon, div.cardOutline, li, div[data-jk]')
                      || link.parentElement;
            const text = (selector) => {
                const el = card ? card.querySelector(selector) : null;
                return el ? el.textContent.replace(/\\s+/g, ' ').trim() : '';
            };

            const titleEl = link.querySelector('span[title]') || link;
            const title = (titleEl.getAttribute('title') || titleEl.textContent || '')
                .replace(/\\s+/g, ' ').trim();

            // Indeed renders every "attribute snippet" in the same slot class:
            // salary, employment type, remote flags. The money-looking one is
            // the salary; the rest are dropped rather than mislabelled.
            const attributes = card
                ? [...card.querySelectorAll('[data-testid="attribute_snippet_testid"], .salary-snippet-container')]
                    .map(el => (el.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean)
                : [];
            const salary = attributes.find(text => /[0-9][0-9,]*\\s*円|月給|年収|時給|日給/.test(text)) || '';

            out.push({
                jk,
                title,
                company: text('[data-testid="company-name"]') || text('.companyName'),
                location: text('[data-testid="text-location"]') || text('.companyLocation'),
                salary,
                attributes,
                snippet: card ? (card.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 400) : '',
                url: 'https://jp.indeed.com/viewjob?jk=' + jk,
            });
        } catch (e) { /* skip malformed card */ }
    }
    return out;
}"""


class IndeedScraper(BaseScraper):
    """Search Indeed Japan with a stealth browser."""

    url_encodes_keyword = True  # Indeed filters by q= server-side

    #: Stealthy first (Cloudflare), then a plain dynamic browser — a challenge
    #: that beats one often yields to the other, and the retry is free.
    fetch_modes = ("stealthy", "dynamic")

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.INDEED

    @property
    def query(self) -> str:
        """The ``q=`` value: the caller's keyword, or the design feed default."""
        return self.keyword or DEFAULT_QUERY

    @property
    def place(self) -> str:
        """The ``l=`` value: a Japanese place name, or ``""`` for nationwide."""
        location = (self.location or "tokyo").strip()
        if location.lower() in ANYWHERE:
            return ""
        return LOCATION_SLUGS.get(location.lower(), location)

    def build_search_url(self, page_number: int = 1) -> str:
        """The search URL for *page_number* (``start=`` is zero-based)."""
        params: dict[str, Any] = {"q": self.query, "hl": "ja"}
        if self.place:
            params["l"] = self.place
        if page_number > 1:
            params["start"] = (page_number - 1) * RESULTS_PER_PAGE
        return f"{BASE_URL}{SEARCH_PATH}?{urlencode(params)}"

    async def fetch_jobs(self) -> list[JobPosting]:
        """Walk the result pages, tolerating one bad page after the first."""
        jobs: list[JobPosting] = []
        seen: set[str] = set()

        for page_number in range(1, MAX_PAGES + 1):
            url = self.build_search_url(page_number)
            try:
                cards = await self.evaluate_page(
                    url,
                    CARD_SCRIPT,
                    key="cards",
                    wait_selector=CARD_SELECTOR,
                    scrolls=2,
                    settle_ms=2_500,
                    mode="stealthy",
                )
            except ScraperError as exc:
                if page_number == 1:
                    raise
                logger.warning("Indeed page failed, stopping", extra={"page": page_number, "error": str(exc)})
                break

            page_jobs = self._parse_cards(cards if isinstance(cards, list) else [])
            fresh = [job for job in page_jobs if job.url not in seen]
            if not fresh:
                break  # pagination exhausted (or the page was short)
            seen.update(job.url for job in fresh)
            jobs.extend(fresh)

            if len(page_jobs) < RESULTS_PER_PAGE:
                break

        logger.info("Indeed search complete", extra={"jobs": len(jobs)})
        return jobs

    def _parse_cards(self, raw_items: list[dict[str, Any]]) -> list[JobPosting]:
        """Map extracted cards onto domain objects, skipping malformed ones."""
        jobs: list[JobPosting] = []
        for item in raw_items:
            try:
                url = str(item["url"])
                if not url.startswith(BASE_URL):
                    # A redirect to the US market would mean American listings.
                    logger.warning("dropping non-Japanese Indeed card", extra={"url": url})
                    continue
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                if not self.matches(title):
                    continue
                jobs.append(
                    JobPosting(
                        title=title,
                        company=str(item.get("company") or "Unknown").strip(),
                        url=url,
                        location=str(item.get("location") or self.place or "Japan").strip(),
                        source_platform=self.platform,
                        salary=str(item["salary"]).strip() if item.get("salary") else None,
                        description=str(item["snippet"]).strip() if item.get("snippet") else None,
                    )
                )
            except Exception:  # noqa: BLE001 — malformed card, keep going
                logger.debug("skipping malformed Indeed card")
        return jobs

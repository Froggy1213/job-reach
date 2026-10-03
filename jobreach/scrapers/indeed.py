"""Indeed Japan (jp.indeed.com) — the largest Japanese board, finally scrapable.

This board used to be **ingest-only**. Cloudflare refused headless clients, so
the honest answer was "a real browser fetches the cards, the agent feeds them to
``job_ingest``" — and that workflow still exists, because it is the right
fallback when automation is blocked. What changed is that it is no longer the
*only* way in: a stealth browser (Scrapling's patched Chromium, which solves
turnstile/interstitial challenges) reads the search page successfully, headless.

How long that takes is **entirely a property of the network the request comes
from**, and the spread is large enough to matter: a residential connection has
been measured at single-digit seconds, while from a blocked or datacentre IP the
stealthy fetch does not complete at all — it was still hanging at 240 s in
testing. That is why the mode chain in :attr:`fetch_modes` exists, why the whole
fetch has an explicit budget (:data:`FETCH_BUDGET_MS`), and why ``job_ingest``
stays documented as the fallback rather than a legacy path.

Verified against the live site before this scraper was written:

* ``div.job_seen_beacon`` / ``div.cardOutline`` — one wrapper per card (16);
* ``a[data-jk]`` (class ``jcs-JobTitle``) — the title link, carrying the listing
  id in ``data-jk``, so **``jk`` is the dedup key** and the URL is rebuilt as
  ``/viewjob?jk=…`` rather than trusting the click-tracking href;
* ``[data-testid="company-name"]``, ``[data-testid="text-location"]``,
  ``.salary-snippet-container`` — company, location and salary, all best-effort
  (the layout shifts, so empty values are tolerated rather than fatal).

Four behavioural notes worth keeping:

* ``l=`` takes free text, so :data:`LOCATION_SLUGS` maps the plugin's slugs
  (``tokyo``) onto Japanese place names (``東京``) — Indeed's own market names.
* ``&hl=ja`` is always set: without it Indeed may serve ``www.indeed.com``,
  whose listings are American. The response's final host is checked and a
  non-Japanese host is a hard failure, never a silent import of US jobs.
* A challenge page (``Just a moment``, ``Ray ID``, …) means **zero results**.
  The scraper reports it as blocked; it must never look like "no jobs today".
* Every result page is read in **one** browser session, reached by navigating to
  the next ``start=`` URL rather than by clicking the pager — see
  :meth:`IndeedScraper.page_steps` for what that buys and what it costs.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ..domain import JobPosting, SourcePlatform
from ..fetchers import (
    FetchResult,
    evaluate,
    goto,
    scroll,
    wait_selector,
)
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

#: Pages of 15 cards. Two pages is 30 listings — plenty for a design search, and
#: both are now read in **one** browser session rather than two (see
#: :meth:`IndeedScraper.page_steps`).
MAX_PAGES = 2
RESULTS_PER_PAGE = 15

#: Cards selector, used both as the readiness check and as the block probe: if
#: it never appears, the page was a challenge, not a result list.
CARD_SELECTOR = "a[data-jk], div.job_seen_beacon"

#: Page work. The scrolls are what make Indeed hydrate the cards below the first
#: screen; the settle is how long that hydration gets. Both numbers are inherited
#: from the per-page implementation this replaced and are worth re-measuring
#: against the live site — they are the only part of a run that is pure waiting.
SCROLL_PX = 800
SCROLL_PASSES = 2
SETTLE_MS = 2_500

#: How long the selector after a navigation may take to appear.
CARD_WAIT_MS = 15_000

#: Ceiling for the whole fetch, in milliseconds — both navigations and both
#: hydration waits. It is also the budget the driver subprocess is killed at
#: (plus ``scrapling.DRIVER_GRACE``), so it has to fit every page: a page 2 that
#: eats the budget would take page 1 down with it. Per page this is *less* than
#: the old per-page call allowed (45 s each), so the worst case does not grow.
FETCH_BUDGET_MS = 90_000

#: One cards key and one landed-URL key per page. The URL is what tells a page
#: that never loaded apart from a page that genuinely held no more listings —
#: the two are indistinguishable from the cards alone, because everything after
#: page 1 is deliberately skippable.
PAGE_KEYS: tuple[str, ...] = tuple(f"cards_page{n}" for n in range(1, MAX_PAGES + 1))
URL_KEYS: tuple[str, ...] = tuple(f"url_page{n}" for n in range(1, MAX_PAGES + 1))

#: Reads the document URL, so a navigation that silently did not happen is
#: visible in the results instead of looking like an empty page.
LOCATION_JS = "() => location.href"


def _skippable(step: dict[str, Any]) -> dict[str, Any]:
    """Mark *step* as one that may fail without failing the whole fetch.

    Every step after the first page carries this. The per-page implementation
    this replaced caught ``ScraperError`` for page 2 and broke out of its loop,
    keeping page 1's listings; with both pages in one session that tolerance has
    to come from the step vocabulary instead. Both backends already honour
    ``optional`` for any step type — see ``drivers/scrapling_driver.run_steps``
    and ``BaseScraper._run_playwright``.
    """
    return {**step, "optional": True}

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

    def page_steps(self) -> list[dict[str, Any]]:
        """Every result page as one step list, for **one** browser session.

        The implementation this replaced called ``evaluate_page`` once per page,
        and each call launched its own browser: two Chromium launches — and, on
        the stealthy backend, two Cloudflare solves — for a single search. Here
        the pages are one step list in one session.

        Paging is done by navigating to the next ``start=`` URL rather than by
        clicking Indeed's pager. The URL is the one this scraper already builds
        (and the same one the old second page opened), so nothing here depends on
        the pager's markup surviving Indeed's next redesign. The trade is that
        the navigation always happens: a keyword with fewer than one page of
        results still pays one extra hop *inside the same browser*, where the old
        code's ``len(page_jobs) < RESULTS_PER_PAGE`` check would have stopped.
        That is one navigation against a whole browser launch, and it is the
        right way round.

        Everything after the first page is :func:`_skippable`, so a page 2 that
        is challenged or slow still leaves page 1's listings intact. The first
        page is not: a board whose first page failed has failed.
        """
        steps: list[dict[str, Any]] = [
            scroll(SCROLL_PX, times=SCROLL_PASSES, settle_ms=SETTLE_MS),
            evaluate(CARD_SCRIPT, PAGE_KEYS[0]),
            # Page 1's landed URL is the baseline every later navigation is
            # compared against — see _warn_about_pages_that_never_loaded.
            evaluate(LOCATION_JS, URL_KEYS[0]),
        ]
        for page_number in range(2, MAX_PAGES + 1):
            steps.extend(
                _skippable(step)
                for step in (
                    # ``goto`` already waits for ``domcontentloaded`` on both
                    # backends, so there is no separate ``wait_load`` step here:
                    # what the cards actually need is the selector wait below.
                    goto(self.build_search_url(page_number)),
                    wait_selector(CARD_SELECTOR, timeout_ms=CARD_WAIT_MS),
                    # The same hydration work as page 1, so page 2 yields as many
                    # cards as it did when it had a browser of its own.
                    scroll(SCROLL_PX, times=SCROLL_PASSES, settle_ms=SETTLE_MS),
                    evaluate(CARD_SCRIPT, PAGE_KEYS[page_number - 1]),
                    evaluate(LOCATION_JS, URL_KEYS[page_number - 1]),
                )
            )
        return steps

    async def fetch_jobs(self) -> list[JobPosting]:
        """Read every result page in a single browser session."""
        result = await self.fetch(
            self.build_search_url(1),
            steps=self.page_steps(),
            wait_selector=CARD_SELECTOR,
            timeout_ms=max(self.timeout_ms, FETCH_BUDGET_MS),
        )

        jobs: list[JobPosting] = []
        seen: set[str] = set()
        for key in PAGE_KEYS:
            cards = result.get(key)
            if not isinstance(cards, list):
                continue
            fresh = [job for job in self._parse_cards(cards) if job.url not in seen]
            seen.update(job.url for job in fresh)
            jobs.extend(fresh)

        self._warn_about_pages_that_never_loaded(result)
        logger.info("Indeed search complete", extra={"jobs": len(jobs)})
        return jobs

    @staticmethod
    def _warn_about_pages_that_never_loaded(result: FetchResult) -> None:
        """Log the navigations that left the browser where it already was.

        Each page's landed URL is compared against **page 1's**, not against the
        URL this scraper asked for. That keeps the check independent of how
        Indeed spells its own pagination: a canonicalised or re-parameterised
        page 2 is still a page 2, while a navigation that never happened leaves
        the document exactly where it started.

        Without this, a challenged page 2 is indistinguishable from "the board
        had no more listings in the second page": the steps are skippable by
        design, so the failure is swallowed and the run just looks short. The
        landed URL is the only evidence that separates the two, and it is the
        only signal the old per-page loop had as well.
        """
        started_at = str(result.get(URL_KEYS[0]) or "")
        if not started_at:
            return  # nothing to compare against; say nothing rather than guess
        for page_number in range(2, MAX_PAGES + 1):
            landed = str(result.get(URL_KEYS[page_number - 1]) or "")
            if landed and landed == started_at:
                logger.warning(
                    "Indeed page did not load; reporting the pages that did",
                    extra={"page": page_number, "url": landed},
                )

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

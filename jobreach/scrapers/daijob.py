"""Daijob (daijob.com) — bilingual / foreign-capital jobs in Japan.

Daijob is the board for postings that expect English (or another foreign
language) alongside Japanese, so it complements Wantedly and Green: same market,
different employers. Its search pages are **server-rendered HTML**, so this
scraper reads them over plain HTTP with :mod:`jobreach.webclient` — no browser,
no JavaScript (``needs_browser = False``).

Verified against the live site:

* ``/jobs/search?keyword=…&la=102&ac=<prefecture>&page=…`` — 20 cards per page,
  ``ac=118`` is 東京都 and ``ac=134`` is 大阪府 (see :data:`PREFECTURE_CODES`);
* one listing is ``<article class="job-card">`` containing the title link
  (``a#_job``), the company link, and a ``<dl>`` of structured fields:
  勤務地 (location), 年収 (salary), 仕事内容 (description), 英語能力;
* a card may list several locations at once (``… / アジア 日本 大阪府``), which
  is why the location is compacted rather than taken verbatim.

The English edition (``/en/jobs/search``) returns the same postings with English
titles; the Japanese edition is used because its titles are the ones the
relevance filter and the user's notes are written for.
"""

from __future__ import annotations

import asyncio

from ..domain import JobPosting, SourcePlatform
from ..errors import ScraperError
from ..htmlextract import cards, definition, element_text, first_link, links
from ..logging_setup import get_logger
from ..webclient import HttpError, build_url, request_text
from .base import BaseScraper

logger = get_logger("scrapers.daijob")

BASE_URL = "https://www.daijob.com"
SEARCH_URL = f"{BASE_URL}/jobs/search"

#: One listing per ``<article class="job-card">``.
CARD_TAG = "article"
CARD_CLASS = "job-card"

#: Listings per result page (the site's own page size).
PER_PAGE = 20

#: Pages walked per run.
MAX_PAGES = 2

#: Daijob's own location codes. ``la=102`` scopes the search to Japan; the
#: ``ac`` value picks the prefecture. Only the two codes that were verified
#: against the live site are listed — Daijob's filter matches postings that
#: *include* the prefecture among their offices, so an approximate code is
#: worse than no filter. Any other location can be passed as a raw number.
PREFECTURE_CODES: dict[str, int] = {
    "tokyo": 118,
    "東京": 118,
    "東京都": 118,
    "osaka": 134,
    "大阪": 134,
    "大阪府": 134,
}

#: Sentinels meaning "no location filter".
ANYWHERE = {"any", "all", "", "japan"}

REQUEST_TIMEOUT = 30.0
PAGE_DELAY_SECONDS = 0.4

#: The location cell repeats the continent and country before the prefecture;
#: they carry no information once the search is scoped to Japan.
_LOCATION_PREFIXES = ("アジア", "日本", "Asia", "Japan")


class DaijobScraper(BaseScraper):
    """Search Daijob's server-rendered listings."""

    url_encodes_keyword = True  # keyword= filters server-side
    needs_browser = False  # plain HTML over HTTP
    http_note = "server-rendered HTML — no browser involved"

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.DAIJOB

    @property
    def prefecture_code(self) -> int | None:
        """Daijob's prefecture code for the requested location, if known."""
        location = (self.location or "tokyo").strip().lower()
        if location in ANYWHERE:
            return None
        if location.isdigit():
            return int(location)
        return PREFECTURE_CODES.get(location)

    def build_search_url(self, page_number: int = 1) -> str:
        """The search URL for *page_number* — used by diagnostics and tests."""
        code = self.prefecture_code
        return build_url(
            SEARCH_URL,
            {
                "keyword": self.keyword,
                "la": 102 if code else None,
                "ac": code,
                "page": page_number,
            },
        )

    async def fetch_jobs(self) -> list[JobPosting]:
        """Walk the result pages until they run out or the cap is reached."""
        jobs: list[JobPosting] = []
        seen: set[str] = set()

        for page_number in range(1, MAX_PAGES + 1):
            try:
                html = await asyncio.to_thread(self._fetch_page, page_number)
            except HttpError as exc:
                if page_number == 1:
                    raise ScraperError(f"Daijob search request failed: {exc}") from exc
                logger.warning("Daijob page failed, stopping", extra={"page": page_number})
                break

            page_jobs = self._parse_cards(self.card_chunks(html))
            fresh = [job for job in page_jobs if job.url not in seen]
            if not fresh:
                break
            seen.update(job.url for job in fresh)
            jobs.extend(fresh)

            if len(page_jobs) < PER_PAGE:
                break
            await asyncio.sleep(PAGE_DELAY_SECONDS)

        logger.info("Daijob search complete", extra={"jobs": len(jobs)})
        return jobs

    # -- HTTP ----------------------------------------------------------------

    def _fetch_page(self, page_number: int) -> str:
        return request_text(self.build_search_url(page_number), timeout=REQUEST_TIMEOUT)

    @staticmethod
    def card_chunks(html: str) -> list[str]:
        """Split a result page into one HTML chunk per listing."""
        return cards(html, tag=CARD_TAG, class_contains=CARD_CLASS)

    # -- parsing -------------------------------------------------------------

    def _parse_cards(self, chunks: list[str]) -> list[JobPosting]:
        """Map card HTML onto domain objects, skipping malformed ones."""
        jobs: list[JobPosting] = []
        for chunk in chunks:
            try:
                link = first_link(chunk, href_contains="/jobs/detail/", attr="id", attr_value="_job")
                if link is None:
                    continue
                href, title = link
                title = title.strip()
                if not title:
                    continue
                if not self.matches(title):
                    continue
                jobs.append(
                    JobPosting(
                        title=title,
                        company=self._company(chunk),
                        url=f"{BASE_URL}{href.split('?')[0]}",
                        location=self._location(chunk),
                        source_platform=self.platform,
                        salary=definition(chunk, "年収") or None,
                        description=definition(chunk, "仕事内容") or None,
                    )
                )
            except Exception:  # noqa: BLE001 — malformed card, keep going
                logger.debug("skipping malformed Daijob card")
        return jobs

    @staticmethod
    def _company(chunk: str) -> str:
        """The company name sits in the card header, above the title.

        The header's first link is the *logo* (an image inside an ``<a>``, so it
        carries no text); the name itself is the utility-classed line beside it,
        with a text-bearing link as the fallback.
        """
        header = chunk.split('id="_job"', 1)[0]
        text = element_text(header, class_contains="fw-bolder")
        if text:
            return text
        for _href, label in links(header, href_contains="/jobs/detail/"):
            if label:
                return label
        for _href, label in links(header, href_contains="/jobs/companyintro/"):
            if label:
                return label
        return "Unknown"

    @staticmethod
    def _location(chunk: str) -> str:
        """Compact the location cell down to the meaningful place names."""
        raw = definition(chunk, "勤務地")
        if not raw:
            return "Japan"
        parts: list[str] = []
        for alternative in raw.split("/"):
            tokens = [token for token in alternative.split() if token]
            while tokens and tokens[0] in _LOCATION_PREFIXES:
                tokens.pop(0)
            cleaned = " ".join(tokens).strip()
            if cleaned and cleaned not in parts:
                parts.append(cleaned)
        return " / ".join(parts) or "Japan"

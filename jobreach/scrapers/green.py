"""Green (green-japan.com) — IT/Web/ゲーム industry jobs, Tokyo-centric.

Green is a Next.js application, which is normally bad news for a scraper — the
listing page renders client-side. But the results are also serialised into the
page's ``__NEXT_DATA__`` payload, so the whole search comes back as **typed
JSON**: title, company, area, salary, description, publication date. Reading it
with :mod:`jobreach.webclient` means this board needs no browser at all
(:attr:`needs_browser = False`), like Wantedly.

Verified against the live site:

* ``/search?keyword=…&area_ids=…&page=…`` really filters server-side, and
  ``area_ids`` takes Green's own prefecture ids (13 = 東京都, 27 = 大阪府 …) —
  see :data:`AREA_IDS`, discovered by probing the live endpoint;
* 20 offers per page at
  ``props.pageProps.defaultSearchJobOfferData.jobOffers``;
* each offer carries ``jobOfferUrl`` (``/company/<id>/job/<id>``),
  ``company.name``, ``areaName``, ``salary``, ``clientBusiness.introduction``
  (the job description) and ``jobOfferUpdatedAtTimestamp`` (unix seconds).

Salary is unusually good here: Green puts a real figure in the card
(``1000万円〜``), which the envelope carries through to the note.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from ..domain import JobPosting, SourcePlatform
from ..errors import ScraperError
from ..htmlextract import next_data
from ..logging_setup import get_logger
from ..webclient import HttpError, build_url, request_text
from .base import BaseScraper

logger = get_logger("scrapers.green")

BASE_URL = "https://www.green-japan.com"
SEARCH_URL = f"{BASE_URL}/search"

#: Offers per result page (the site's own page size).
PER_PAGE = 20

#: Pages walked per run. Green has hundreds of matches for a broad keyword;
#: two pages is the same coverage as the other boards and keeps the note short.
MAX_PAGES = 2

#: The plugin's location slugs → Green's prefecture ids. Confirmed against the
#: live endpoint (each id was probed and returns only that prefecture); an
#: unknown slug searches nationwide, and a raw number passes through untouched.
AREA_IDS: dict[str, int] = {
    "hokkaido": 1, "北海道": 1, "sapporo": 1, "札幌": 1,
    "tokyo": 13, "東京": 13, "東京都": 13,
    "kanagawa": 14, "神奈川": 14, "yokohama": 14, "横浜": 14,
    "ishikawa": 17, "石川": 17,
    "aichi": 23, "愛知": 23, "nagoya": 23, "名古屋": 23,
    "kyoto": 26, "京都": 26,
    "osaka": 27, "大阪": 27, "大阪府": 27,
    "hiroshima": 34, "広島": 34,
    "fukuoka": 40, "福岡": 40,
}

#: Sentinels meaning "no area filter".
ANYWHERE = {"any", "all", "", "japan"}

REQUEST_TIMEOUT = 30.0
PAGE_DELAY_SECONDS = 0.4


class GreenScraper(BaseScraper):
    """Search Green through the JSON payload its pages embed."""

    url_encodes_keyword = True  # the keyword really is applied server-side
    needs_browser = False  # __NEXT_DATA__ is plain JSON: no browser involved
    http_note = "JSON embedded in the page — no browser involved"

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.GREEN

    @property
    def area_id(self) -> int | None:
        """Green's prefecture id for the requested location, if known."""
        location = (self.location or "tokyo").strip().lower()
        if location in ANYWHERE:
            return None
        if location.isdigit():
            return int(location)
        return AREA_IDS.get(location)

    def build_search_url(self, page_number: int = 1) -> str:
        """The search URL for *page_number* — used by diagnostics and tests."""
        return build_url(
            SEARCH_URL,
            {"keyword": self.keyword, "area_ids": self.area_id, "page": page_number},
        )

    async def fetch_jobs(self) -> list[JobPosting]:
        """Walk the result pages until they run out or the cap is reached."""
        jobs: list[JobPosting] = []
        seen: set[str] = set()

        for page_number in range(1, MAX_PAGES + 1):
            try:
                offers = await asyncio.to_thread(self._fetch_page, page_number)
            except HttpError as exc:
                if page_number == 1:
                    raise ScraperError(f"Green search request failed: {exc}") from exc
                logger.warning("Green page failed, stopping", extra={"page": page_number})
                break

            fresh = [job for job in self._parse_offers(offers) if job.url not in seen]
            if not fresh:
                break
            seen.update(job.url for job in fresh)
            jobs.extend(fresh)

            if len(offers) < PER_PAGE:
                break
            await asyncio.sleep(PAGE_DELAY_SECONDS)

        logger.info("Green search complete", extra={"jobs": len(jobs)})
        return jobs

    # -- HTTP ----------------------------------------------------------------

    def _fetch_page(self, page_number: int) -> list[dict[str, Any]]:
        """Fetch one result page and return its offer records."""
        html = request_text(self.build_search_url(page_number), timeout=REQUEST_TIMEOUT)
        payload = next_data(html)
        if payload is None:
            raise ScraperError(
                "Green served a page without its embedded search payload "
                "(the site's frontend probably changed)"
            )
        return self.offers_in(payload)

    @staticmethod
    def offers_in(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Dig the offer list out of the Next.js payload."""
        try:
            offers = payload["props"]["pageProps"]["defaultSearchJobOfferData"]["jobOffers"]
        except (KeyError, TypeError) as exc:
            raise ScraperError(
                f"Green's embedded payload has no job offers at the expected path: {exc}"
            ) from exc
        if not isinstance(offers, list):
            raise ScraperError("Green's embedded payload is not a list of offers")
        return [offer for offer in offers if isinstance(offer, dict)]

    # -- parsing -------------------------------------------------------------

    def _parse_offers(self, offers: list[dict[str, Any]]) -> list[JobPosting]:
        """Map Green offers onto domain objects, skipping malformed ones."""
        jobs: list[JobPosting] = []
        for offer in offers:
            try:
                title = str(offer.get("name") or "").strip()
                path = str(offer.get("jobOfferUrl") or "").strip()
                if not title or not path:
                    continue
                if not self.matches(title):
                    continue
                company = offer.get("company") or {}
                business = offer.get("clientBusiness") or {}
                jobs.append(
                    JobPosting(
                        title=title,
                        company=str(company.get("name") or "Unknown").strip(),
                        url=f"{BASE_URL}{path}",
                        location=str(offer.get("areaName") or "Japan").strip(),
                        source_platform=self.platform,
                        salary=str(offer["salary"]).strip() if offer.get("salary") else None,
                        description=self._description(offer, business),
                        posted_at=self._timestamp(offer.get("jobOfferUpdatedAtTimestamp")),
                    )
                )
            except Exception:  # noqa: BLE001 — malformed offer, keep going
                logger.debug("skipping malformed Green offer")
        return jobs

    @staticmethod
    def _description(offer: dict[str, Any], business: dict[str, Any]) -> str | None:
        """The most useful body text Green exposes for a listing."""
        parts = [
            str(business.get("name") or "").strip(),
            str(business.get("introduction") or "").strip(),
        ]
        joined = "\n".join(part for part in parts if part)
        return joined or None

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        """Green sends publication time as unix seconds."""
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            return None

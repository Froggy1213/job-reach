"""Wantedly (ウォンテッドリー) — the general-purpose Japanese board.

Wantedly is the board that answers arbitrary keyword queries in any language,
and it turns out to answer them over plain JSON: ``/api/v1/projects`` returns
real server-side search results — title, company, location, description,
publication date — with no JavaScript involved. Reading it with
:mod:`jobreach.webclient` means this scraper needs **no browser at all**, which
is why :attr:`WantedlyScraper.needs_browser` is ``False``: the CLI will never
refuse to run Wantedly because a browser stack is missing.

That is a correction, not just an optimisation. The HTML search page
(``/projects?q=…``) **discards** query parameters it no longer recognises and
redirects to the generic feed, so a keyword search through the page silently
returned unrelated listings — and because the old scraper declared
``url_encodes_keyword = True`` the client-side filter did not catch them either.
The API endpoint honours ``q=`` for real, so server-side filtering is now what
it always claimed to be.

Parameters that matter:

* ``q`` — free-text query; Japanese terms return Japanese listings.
* ``areas`` — location slug (``tokyo``, ``osaka``, …); ``any`` omits the filter.
* ``page`` — 1-based; 10 results per page, ``_metadata.total_pages`` caps it.

The fragile markup work of the previous version (company links being siblings of
project links, titles in nested headings, ward names recovered from free text)
is gone: these fields now come from the board's own data model instead of from a
heuristic over rendered HTML.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from ..domain import JobPosting, SourcePlatform
from ..errors import ScraperError
from ..logging_setup import get_logger
from ..webclient import HttpError, build_url, get_json
from .base import BaseScraper

logger = get_logger("scrapers.wantedly")

BASE_URL = "https://www.wantedly.com"
API_URL = f"{BASE_URL}/api/v1/projects"

#: Public project page — what a listing URL is built from.
PROJECT_URL = f"{BASE_URL}/projects"

#: Wantedly serves 10 records per page through this endpoint (a larger
#: ``per_page`` is accepted and ignored, so pagination is the only lever).
PER_PAGE = 10

#: Pages to walk. A keyword search earns one more page than the generic feed,
#: which is mostly noise and gets filtered by title anyway.
MAX_PAGES_KEYWORD = 3
MAX_PAGES_DEFAULT = 2

#: Location sentinels that mean "do not filter by area".
ANYWHERE = {"any", "all", "", "japan"}

REQUEST_TIMEOUT = 30.0

#: Politeness pause between pages — the whole search is a few HTTP calls.
PAGE_DELAY_SECONDS = 0.4


class WantedlyScraper(BaseScraper):
    """Search Wantedly's project API."""

    url_encodes_keyword = True  # the API filters by q= server-side
    needs_browser = False  # JSON over HTTP: no browser, no venv, no Chromium

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.WANTEDLY

    @property
    def area_slug(self) -> str | None:
        """Location slug for ``areas=``, or ``None`` to search nationwide."""
        location = (self.location or "tokyo").lower()
        return None if location in ANYWHERE else location

    def build_api_url(self, page_number: int = 1) -> str:
        """The API URL for *page_number* — used by diagnostics and tests."""
        return build_url(
            API_URL, {"q": self.keyword, "areas": self.area_slug, "page": page_number}
        )

    async def fetch_jobs(self) -> list[JobPosting]:
        """Walk the result pages until they run out or the cap is reached."""
        jobs: list[JobPosting] = []
        seen: set[str] = set()
        max_pages = MAX_PAGES_KEYWORD if self.keyword else MAX_PAGES_DEFAULT

        for page_number in range(1, max_pages + 1):
            try:
                payload = await asyncio.to_thread(self._fetch_page, page_number)
            except HttpError as exc:
                if page_number == 1:
                    raise ScraperError(f"Wantedly API request failed: {exc}") from exc
                logger.warning("Wantedly page failed, stopping", extra={"page": page_number})
                break

            fresh = [
                job for job in self._parse_projects(self._records(payload)) if job.url not in seen
            ]
            if not fresh:
                break
            seen.update(job.url for job in fresh)
            jobs.extend(fresh)

            if page_number >= self._total_pages(payload):
                break
            await asyncio.sleep(PAGE_DELAY_SECONDS)

        logger.info("Wantedly search complete", extra={"jobs": len(jobs)})
        return jobs

    # -- HTTP ----------------------------------------------------------------

    def _fetch_page(self, page_number: int) -> dict[str, Any]:
        """Fetch one API page (runs in a worker thread — callers ``await`` it)."""
        payload = get_json(
            API_URL,
            params={"q": self.keyword, "areas": self.area_slug, "page": page_number},
            headers={"Referer": f"{BASE_URL}/projects"},
            timeout=REQUEST_TIMEOUT,
        )
        if not isinstance(payload, dict):
            raise ScraperError(
                f"Wantedly API returned {type(payload).__name__}, expected an object"
            )
        return payload

    @staticmethod
    def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """List the project records out of an API response."""
        records = payload.get("data")
        if not isinstance(records, list):
            raise ScraperError("Wantedly API response has no `data` array")
        return [record for record in records if isinstance(record, dict)]

    @staticmethod
    def _total_pages(payload: dict[str, Any]) -> int:
        """Total page count, or 1 when the API omits the metadata."""
        metadata = payload.get("_metadata") or {}
        try:
            return max(1, int(metadata.get("total_pages") or 1))
        except (TypeError, ValueError):
            return 1

    # -- parsing -------------------------------------------------------------

    def _parse_projects(self, records: list[dict[str, Any]]) -> list[JobPosting]:
        """Map API records onto domain objects, skipping malformed ones."""
        jobs: list[JobPosting] = []
        for record in records:
            try:
                title = str(record.get("title") or "").strip()
                project_id = record.get("id")
                if not title or project_id is None:
                    continue
                if not self.matches(title):
                    continue
                company = record.get("company") or {}
                jobs.append(
                    JobPosting(
                        title=title,
                        company=str(company.get("name") or "Unknown").strip(),
                        url=f"{PROJECT_URL}/{project_id}",
                        location=self._location(record),
                        source_platform=self.platform,
                        description=self._description(record),
                        posted_at=self._published_at(record),
                    )
                )
            except Exception:  # noqa: BLE001 — malformed record, keep going
                logger.debug("skipping malformed Wantedly project")
        return jobs

    @staticmethod
    def _location(record: dict[str, Any]) -> str:
        """Best available location string for a project."""
        parts = [
            str(record.get("location") or "").strip(),
            str(record.get("location_suffix") or "").strip(),
        ]
        joined = " ".join(part for part in parts if part)
        if not joined:
            return "Japan"
        if "オンライン" in joined or "リモート" in joined:
            return f"Remote ({joined})"
        return joined

    @staticmethod
    def _description(record: dict[str, Any]) -> str | None:
        """Project description, when the API included one.

        Worth keeping: ``validation="llm"`` classifies far better with the body
        text than with a title alone.
        """
        text = str(record.get("description") or "").strip()
        return text or None

    @staticmethod
    def _published_at(record: dict[str, Any]) -> datetime | None:
        """Publication timestamp, when it parses (the API sends ISO-8601)."""
        raw = str(record.get("published_at") or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

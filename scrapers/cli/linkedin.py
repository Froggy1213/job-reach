"""LinkedIn Jobs scraper via ``opencli linkedin`` CLI.

Relies on the OpenCLI Chrome extension.  No Playwright needed — the CLI
handles browser automation (Chrome must be running with the extension).

CLI reference (from memory):
    opencli linkedin search "<keyword>" --location "Tokyo" --limit N -f json

Output format (per record):
    {title, company, location, url, salary, listed}
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import HttpUrl

from models.enums import SourcePlatform
from models.job_posting import JobPosting
from scrapers.cli.base import CliScraper

logger = logging.getLogger("job_hunter.scraper.linkedin")

_DEFAULT_LOCATION = "Tokyo"
_DEFAULT_LIMIT = 25


class LinkedInScraper(CliScraper):
    """Scrape LinkedIn Jobs in Tokyo via opencli."""

    _cli_base = ["opencli", "linkedin", "search"]
    _url_encodes_keyword = True  # LinkedIn server-side search

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.LINKEDIN

    def _build_cli_args(self) -> list[str]:
        keyword = self._keyword or "designer"
        location = self._location or _DEFAULT_LOCATION
        return [
            *self._cli_base,
            keyword,
            "--location", location,
            "--limit", str(_DEFAULT_LIMIT),
            "-f", "json",
        ]

    def _parse_cli_output(self, raw: list[dict[str, Any]]) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        for rec in raw:
            try:
                url = rec.get("url", "")
                if not url:
                    continue
                # Validate URL — pydantic will raise if invalid
                try:
                    parsed_url = HttpUrl(url)
                except Exception:
                    logger.debug("Skipping record with invalid URL", extra={"url": url})
                    continue

                jobs.append(JobPosting(
                    title=str(rec.get("title", "")).strip(),
                    company=str(rec.get("company", "Unknown")).strip(),
                    url=parsed_url,
                    location=str(rec.get("location", _DEFAULT_LOCATION)).strip(),
                    source_platform=SourcePlatform.LINKEDIN,
                    salary=str(rec["salary"]).strip() if rec.get("salary") else None,
                ))
            except Exception as exc:
                logger.debug("Skipping malformed LinkedIn record", extra={"error": str(exc)})
                continue
        return jobs

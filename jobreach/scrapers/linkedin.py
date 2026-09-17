"""LinkedIn Jobs, via the ``opencli linkedin`` CLI.

LinkedIn has no usable public search API and hates scrapers, but the user
already has a logged-in Chrome session with the OpenCLI extension. Driving
that CLI means we inherit the real session instead of fighting a login wall —
and it costs zero Playwright code.

This is also the only board that can fail for a *human* reason: if Chrome is
not running, ``opencli`` hangs or errors. The error message says so explicitly
so the Hermes agent reports something actionable instead of a timeout.
"""

from __future__ import annotations

from typing import Any

from ..domain import JobPosting, SourcePlatform
from ..logging_setup import get_logger
from .cli_base import CliScraper

logger = get_logger("scrapers.linkedin")

DEFAULT_LOCATION = "Tokyo"
DEFAULT_LIMIT = 25


class LinkedInScraper(CliScraper):
    """Search LinkedIn Jobs through the OpenCLI Chrome bridge."""

    cli_base = ("opencli", "linkedin", "search")
    cli_timeout = 120
    url_encodes_keyword = True  # LinkedIn ranks and filters server-side

    install_hint = (
        "LinkedIn search needs the OpenCLI Chrome extension with Chrome running:\n"
        "  1. start Chrome (the extension must be loaded)\n"
        "  2. verify with: opencli linkedin whoami\n"
        "If that hangs, Chrome is not running."
    )

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.LINKEDIN

    def build_cli_args(self) -> list[str]:
        return [
            *self.cli_base,
            self.keyword or "designer",
            "--location", self.location or DEFAULT_LOCATION,
            "--limit", str(DEFAULT_LIMIT),
            "-f", "json",
        ]

    def parse_cli_output(self, records: list[dict[str, Any]]) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        for record in records:
            url = str(record.get("url") or "").strip()
            if not url:
                continue
            try:
                jobs.append(
                    JobPosting(
                        title=str(record.get("title") or "").strip() or "Untitled role",
                        company=str(record.get("company") or "Unknown").strip(),
                        url=url,
                        location=str(record.get("location") or DEFAULT_LOCATION).strip(),
                        source_platform=self.platform,
                        salary=(str(record["salary"]).strip() if record.get("salary") else None),
                    )
                )
            except Exception:  # noqa: BLE001 — malformed record, keep going
                logger.debug("skipping malformed LinkedIn record")
        return jobs

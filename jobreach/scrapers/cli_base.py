"""Scraper for boards reachable through an external CLI.

Some boards are already solved by a CLI that drives the user's real browser
(``opencli`` for LinkedIn, for instance). Shelling out to it is both simpler
and more robust than reimplementing the same automation with Playwright, and
it reuses the user's existing logged-in session.

Subclasses set :attr:`cli_base`, build the argument list, and map the CLI's
JSON records onto :class:`~jobreach.domain.JobPosting`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from abc import abstractmethod
from collections.abc import Sequence
from typing import Any

from ..domain import JobPosting
from ..errors import MissingDependencyError, ScraperError
from ..logging_setup import get_logger
from ..proc import run_captured
from .base import BaseScraper

logger = get_logger("scrapers.cli")


class CliScraper(BaseScraper):
    """Base class for scrapers that fetch listings by running a CLI tool."""

    #: Command prefix, e.g. ``["opencli", "linkedin", "search"]``.
    cli_base: Sequence[str] = ()

    #: Hard timeout for one CLI invocation, in seconds.
    cli_timeout: int = 120

    #: Hint shown when the CLI is missing, so failures are actionable.
    install_hint: str = ""

    @abstractmethod
    def build_cli_args(self) -> list[str]:
        """Return the full argv for this run."""

    @abstractmethod
    def parse_cli_output(self, records: list[dict[str, Any]]) -> list[JobPosting]:
        """Map raw JSON records from the CLI onto domain objects."""

    async def fetch_jobs(self) -> list[JobPosting]:
        """Run the CLI, decode its JSON, and apply the relevance filter."""
        args = self.build_cli_args()
        executable = args[0] if args else ""
        if executable and shutil.which(executable) is None:
            raise MissingDependencyError(
                f"the {executable!r} command is not on PATH",
                hint=self.install_hint or f"install {executable} and try again",
            )

        logger.info("running CLI scraper", extra={"command": " ".join(args)})
        try:
            proc = await asyncio.to_thread(
                run_captured, args, timeout=self.cli_timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise ScraperError(
                f"{executable} timed out after {self.cli_timeout}s "
                "(is the browser it drives actually running?)"
            ) from exc
        except FileNotFoundError as exc:
            raise MissingDependencyError(
                f"the {executable!r} command is not on PATH",
                hint=self.install_hint or f"install {executable} and try again",
            ) from exc

        if proc.returncode != 0:
            raise ScraperError(
                f"{executable} exited {proc.returncode}: {proc.stderr.strip()[:400]}"
            )
        try:
            raw = json.loads(proc.stdout or "null")
        except json.JSONDecodeError as exc:
            raise ScraperError(
                f"{executable} returned invalid JSON: {exc}; "
                f"first 200 chars: {proc.stdout[:200]!r}"
            ) from exc

        records = _records_from(raw)
        jobs = [job for job in self.parse_cli_output(records) if self.matches(job.title)]
        logger.info(
            "CLI scraper finished",
            extra={"platform": self.platform.value, "parsed": len(jobs)},
        )
        return jobs


def _records_from(raw: Any) -> list[dict[str, Any]]:
    """Unwrap the several shapes a CLI may return into a list of records."""
    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict):
        records = raw.get("jobs") or raw.get("data") or raw.get("results") or []
    else:
        raise ScraperError(f"CLI returned unexpected JSON type: {type(raw).__name__}")
    if not isinstance(records, list):
        raise ScraperError(f"CLI returned non-list records: {type(records).__name__}")
    return [record for record in records if isinstance(record, dict)]

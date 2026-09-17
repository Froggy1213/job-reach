"""Base class for CLI-based scrapers.

Subclasses call an external CLI tool via subprocess.run and parse its stdout
(typically JSON).  No Playwright is involved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from abc import abstractmethod
from typing import Any

from core.exceptions import ScraperError
from models.job_posting import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger("job_hunter.scraper.cli")


class CliScraper(BaseScraper):
    """Abstract scraper that fetches jobs via an external CLI command.

    Subclasses MUST:
        - Set ``_cli_base`` (list of str — the base command)
        - Implement ``_build_cli_args() -> list[str]``
        - Implement ``_parse_cli_output(raw: list[dict]) -> list[JobPosting]``
    """

    _cli_base: list[str] = []
    _cli_timeout: int = 120

    # ---- Subclass contract ------------------------------------------------

    @abstractmethod
    def _build_cli_args(self) -> list[str]:
        """Build the full CLI argument list."""
        ...

    @abstractmethod
    def _parse_cli_output(self, raw: list[dict[str, Any]]) -> list[JobPosting]:
        """Parse CLI's JSON stdout into JobPosting domain models."""
        ...

    # ---- BaseScraper overrides --------------------------------------------

    async def parse_page(self, page):
        raise NotImplementedError("CLI scrapers do not use Playwright")

    async def fetch_jobs(self) -> list[JobPosting]:
        """Run CLI, parse JSON, filter by title."""
        args = self._build_cli_args()
        logger.info("Running CLI scraper", extra={"command": " ".join(args)})

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                args,
                capture_output=True,
                text=True,
                timeout=self._cli_timeout,
            )
            if proc.returncode != 0:
                raise ScraperError(
                    f"CLI exited {proc.returncode}: {proc.stderr.strip()[:500]}"
                )
            raw = json.loads(proc.stdout)
        except subprocess.TimeoutExpired as exc:
            raise ScraperError(f"CLI timed out after {self._cli_timeout}s: {exc}")
        except json.JSONDecodeError as exc:
            raise ScraperError(f"CLI returned invalid JSON: {exc}")

        # Support both top-level list and {"jobs": [...]} wrapper
        if isinstance(raw, dict):
            records = raw.get("jobs", raw.get("data", raw.get("results", [])))
        elif isinstance(raw, list):
            records = raw
        else:
            raise ScraperError(f"CLI returned unexpected type: {type(raw)}")

        if not isinstance(records, list):
            raise ScraperError(f"CLI returned non-list records: {type(records)}")

        jobs = self._parse_cli_output(records)
        filtered = [j for j in jobs if self.matches(j.title)]
        logger.info(
            "CLI scraper done",
            extra={"platform": self.platform.value, "total": len(jobs), "filtered": len(filtered)},
        )
        return filtered

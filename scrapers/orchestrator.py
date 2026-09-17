"""Scraper orchestrator -- runs all registered scrapers concurrently.

The orchestrator is the entry point for scraping operations.  It:
1. Runs every registered scraper in parallel via ``asyncio.gather``.
2. Catches per-scraper exceptions so one failure doesn't kill others.
3. Deduplicates results by URL before persisting.
4. Returns a count of *new* jobs per platform.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Sequence

from database.repository import JobRepository, normalize_url
from models.enums import SourcePlatform
from models.job_posting import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger("job_hunter.orchestrator")


@dataclass
class ScrapeResult:
    """Result of a full scrape run across all platforms.

    Attributes:
        counts: Number of *new* jobs per platform.
        new_jobs: The actual ``JobPosting`` objects that were persisted,
            in the order they were scraped.
    """

    counts: dict[SourcePlatform, int] = field(default_factory=dict)
    new_jobs: list[JobPosting] = field(default_factory=list)


class ScraperOrchestrator:
    """Coordinates concurrent scraping across multiple job boards.

    Args:
        scrapers: The list of ``BaseScraper`` strategies to execute.
        repository: The ``JobRepository`` used for deduplication checks
            and persistence.
    """

    def __init__(
        self,
        scrapers: Sequence[BaseScraper],
        repository: JobRepository,
    ) -> None:
        self._scrapers = list(scrapers)
        self._repository = repository

    @property
    def platforms(self) -> list[SourcePlatform]:
        """Return the platforms covered by registered scrapers."""
        return [s.platform for s in self._scrapers]

    async def run_all(self) -> ScrapeResult:
        """Execute all scrapers in parallel and persist new listings.

        Each scraper runs in its own task.  Exceptions from individual
        scrapers are caught, logged, and do NOT propagate -- a failing
        scraper never blocks the others.

        Deduplication: a single bulk query checks which scraped URLs
        already exist in the repository before persisting.  This avoids
        the N+1 problem of checking URLs one-by-one in a loop.

        Returns:
            A ``ScrapeResult`` with counts of new jobs per platform and
            the list of ``JobPosting`` objects that were persisted.
        """
        logger.info(
            "Starting scrape run",
            extra={"platforms": [p.value for p in self.platforms]},
        )

        results = await asyncio.gather(
            *(self._run_one(s) for s in self._scrapers),
            return_exceptions=True,
        )

        counts: dict[SourcePlatform, int] = {}
        all_new_jobs: list[JobPosting] = []
        for scraper, result in zip(self._scrapers, results):
            if isinstance(result, Exception):
                logger.error(
                    "Scraper failed: %s — %s",
                    scraper.platform.value,
                    result,
                    extra={
                        "platform": scraper.platform.value,
                        "error": str(result),
                    },
                )
                counts[scraper.platform] = 0
            else:
                scount, sjobs = result
                counts[scraper.platform] = scount
                all_new_jobs.extend(sjobs)

        total = sum(counts.values())
        logger.info("Scrape run complete", extra={"new_jobs": total, "by_platform": counts})
        return ScrapeResult(counts=counts, new_jobs=all_new_jobs)

    async def _run_one(self, scraper: BaseScraper) -> tuple[int, list[JobPosting]]:
        """Execute a single scraper, deduplicate, and persist.

        Uses URL normalization + a single bulk query to check existing
        URLs, then batch-inserts all new jobs in one transaction.

        Returns a tuple of (count of new jobs, list of new JobPosting objects).
        """
        jobs = await scraper.fetch_jobs()
        logger.debug(
            "Scraper returned",
            extra={"platform": scraper.platform.value, "count": len(jobs)},
        )

        # Normalize URLs for consistent deduplication.
        urls = [normalize_url(str(job.url)) for job in jobs]
        existing_urls = await self._repository.get_existing_urls(urls)

        new_jobs: list[JobPosting] = []
        for job, norm_url in zip(jobs, urls):
            if norm_url in existing_urls:
                logger.debug("Skipping duplicate", extra={"url": norm_url})
                continue
            new_jobs.append(job)

        # Batch insert all new jobs in one transaction.
        if new_jobs:
            await self._repository.save_many(new_jobs)

        logger.info(
            "Scraper finished",
            extra={
                "platform": scraper.platform.value,
                "total": len(jobs),
                "new": len(new_jobs),
            },
        )
        return len(new_jobs), new_jobs


"""Tests for the ScraperOrchestrator."""

from __future__ import annotations

import pytest
from pydantic import HttpUrl

from models.enums import SourcePlatform
from models.job_posting import JobPosting
from scrapers.base import BaseScraper
from scrapers.orchestrator import ScraperOrchestrator


class DummyScraper(BaseScraper):
    """A dummy scraper for testing that returns pre-configured jobs."""

    def __init__(self, platform: SourcePlatform, jobs: list[JobPosting]) -> None:
        super().__init__(headless=True)
        self._platform = platform
        self._jobs = jobs
        self.fetch_call_count = 0

    @property
    def platform(self) -> SourcePlatform:
        return self._platform

    async def fetch_jobs(self) -> list[JobPosting]:
        self.fetch_call_count += 1
        return self._jobs


class FailingScraper(BaseScraper):
    """A dummy scraper that raises an exception."""

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.WANTEDLY

    async def fetch_jobs(self) -> list[JobPosting]:
        from core.exceptions import ScraperError

        raise ScraperError("Test failure")


def _make_job(url: str, platform: SourcePlatform) -> JobPosting:
    return JobPosting(
        title="Test Designer",
        company="Test Co",
        url=HttpUrl(url),
        location="Tokyo",
        source_platform=platform,
    )


@pytest.mark.asyncio
async def test_orchestrator_deduplicates_urls(repository):
    """The orchestrator should save only jobs with unseen URLs."""
    # Pre-populate repository with one job
    existing_job = _make_job("https://example.com/1", SourcePlatform.WANTEDLY)
    await repository.save_many([existing_job])

    # Scraper returns the existing job AND a new job
    scraper_jobs = [
        existing_job,
        _make_job("https://example.com/2", SourcePlatform.WANTEDLY),
    ]
    scraper = DummyScraper(SourcePlatform.WANTEDLY, scraper_jobs)
    orchestrator = ScraperOrchestrator([scraper], repository)

    result = await orchestrator.run_all()

    # Should only return the new job
    assert result.counts[SourcePlatform.WANTEDLY] == 1
    assert len(result.new_jobs) == 1
    assert str(result.new_jobs[0].url) == "https://example.com/2"


@pytest.mark.asyncio
async def test_orchestrator_handles_failing_scraper(repository):
    """A failing scraper should not prevent others from completing."""
    good_scraper = DummyScraper(
        SourcePlatform.MYNAVI_2027,
        [_make_job("https://example.com/mynavi", SourcePlatform.MYNAVI_2027)],
    )
    bad_scraper = FailingScraper()

    orchestrator = ScraperOrchestrator([good_scraper, bad_scraper], repository)
    result = await orchestrator.run_all()

    # Good scraper succeeded
    assert result.counts[SourcePlatform.MYNAVI_2027] == 1
    # Bad scraper returned 0 (and logged the error)
    assert result.counts[SourcePlatform.WANTEDLY] == 0
    assert len(result.new_jobs) == 1

"""Integration tests for SQLAlchemyJobRepository.

These tests use an in-memory SQLite database, so they exercise the
real SQLAlchemy path without requiring an external database server.
"""

from datetime import datetime, timezone

import pytest
from pydantic import HttpUrl

from models.enums import SourcePlatform
from models.job_posting import JobPosting


def _make_job(title: str = "Software Engineer", url: str = "https://example.com/jobs/1", platform: SourcePlatform = SourcePlatform.WANTEDLY) -> JobPosting:
    """Factory helper for creating test JobPosting instances."""
    return JobPosting(
        title=title,
        company="Test株式会社",
        url=HttpUrl(url),
        location="Tokyo",
        source_platform=platform,
    )


@pytest.mark.asyncio
async def test_save_and_exists(repository):
    """After saving, exists() should return True for that URL."""
    job = _make_job(url="https://example.com/jobs/save-test")
    assert not await repository.exists("https://example.com/jobs/save-test")

    saved = await repository.save(job)
    assert saved.title == job.title
    assert saved.scraped_at is not None

    assert await repository.exists("https://example.com/jobs/save-test")


@pytest.mark.asyncio
async def test_get_all_returns_newest_first(repository):
    """get_all() should return jobs ordered by scraped_at descending."""
    job1 = _make_job(title="First", url="https://example.com/jobs/1")
    job2 = _make_job(title="Second", url="https://example.com/jobs/2")

    await repository.save(job1)
    await repository.save(job2)

    jobs = await repository.get_all()
    assert len(jobs) == 2
    # Second saved should appear first (newer scraped_at)
    assert jobs[0].title == "Second"
    assert jobs[1].title == "First"


@pytest.mark.asyncio
async def test_get_by_source_filter(repository):
    """get_by_source() should only return jobs for the given platform."""
    job = _make_job(url="https://example.com/jobs/src-filter")
    await repository.save(job)

    # Should find it for WANTEDLY
    jobs = await repository.get_by_source(SourcePlatform.WANTEDLY)
    assert len(jobs) == 1
    assert jobs[0].source_platform == SourcePlatform.WANTEDLY


@pytest.mark.asyncio
async def test_save_duplicate_url_raises(repository):
    """Saving two jobs with the same URL should raise RepositoryError."""
    job1 = _make_job(url="https://example.com/jobs/dup")
    await repository.save(job1)

    job2 = _make_job(title="Different title", url="https://example.com/jobs/dup")
    from core.exceptions import RepositoryError

    with pytest.raises(RepositoryError):
        await repository.save(job2)


@pytest.mark.asyncio
async def test_exists_returns_false_for_unknown_url(repository):
    """exists() should return False for URLs not in the database."""
    assert not await repository.exists("https://nonexistent.example.com/job")


@pytest.mark.asyncio
async def test_get_all_empty(repository):
    """get_all() should return an empty list when no jobs are stored."""
    jobs = await repository.get_all()
    assert jobs == []


@pytest.mark.asyncio
async def test_get_jobs_page(repository):
    """get_jobs_page() should return correctly paginated results."""
    for i in range(10):
        await repository.save(_make_job(title=f"Job {i}", url=f"https://example.com/{i}"))

    page_1 = await repository.get_jobs_page(limit=5, offset=0)
    assert len(page_1) == 5
    # Since they are ordered by scraped_at DESC, the last inserted should be first.
    assert page_1[0].title == "Job 9"

    page_2 = await repository.get_jobs_page(limit=5, offset=5)
    assert len(page_2) == 5
    assert page_2[0].title == "Job 4"

    page_3 = await repository.get_jobs_page(limit=5, offset=10)
    assert len(page_3) == 0


@pytest.mark.asyncio
async def test_count_jobs(repository):
    """count_jobs() should return the total number of jobs, with optional filtering."""
    await repository.save(_make_job(url="https://ex.com/1", platform=SourcePlatform.WANTEDLY))
    await repository.save(_make_job(url="https://ex.com/2", platform=SourcePlatform.WANTEDLY))
    await repository.save(_make_job(url="https://ex.com/3", platform=SourcePlatform.MYNAVI_2027))

    assert await repository.count_jobs() == 3
    assert await repository.count_jobs(SourcePlatform.WANTEDLY) == 2
    assert await repository.count_jobs(SourcePlatform.MYNAVI_2027) == 1


@pytest.mark.asyncio
async def test_save_many(repository):
    """save_many() should insert multiple jobs in one transaction."""
    jobs = [
        _make_job(url="https://ex.com/batch/1"),
        _make_job(url="https://ex.com/batch/2"),
    ]
    saved = await repository.save_many(jobs)
    assert len(saved) == 2
    assert await repository.count_jobs() == 2


@pytest.mark.asyncio
async def test_get_existing_urls(repository):
    """get_existing_urls() should correctly identify which URLs already exist."""
    await repository.save(_make_job(url="https://ex.com/exists"))

    urls_to_check = [
        "https://ex.com/exists",
        "https://ex.com/new",
    ]
    existing = await repository.get_existing_urls(urls_to_check)
    assert existing == {"https://ex.com/exists"}


# --- Subscriber Repository Tests ---

from database.sqlalchemy_repository import SQLAlchemySubscriberRepository

@pytest.fixture
def subscriber_repo(repository):
    # repository is an instance of SQLAlchemyJobRepository which has `_session_factory`
    return SQLAlchemySubscriberRepository(repository._session_factory)


@pytest.mark.asyncio
async def test_add_and_remove_subscriber(subscriber_repo):
    """It should correctly add, retrieve, and remove subscribers."""
    # Add
    assert await subscriber_repo.add_subscriber(12345) is True
    assert await subscriber_repo.add_subscriber(12345) is False  # Already exists

    # Retrieve
    subs = await subscriber_repo.get_all_subscribers()
    assert 12345 in subs

    # Remove
    assert await subscriber_repo.remove_subscriber(12345) is True
    assert await subscriber_repo.remove_subscriber(12345) is False  # Not found

    subs_after = await subscriber_repo.get_all_subscribers()
    assert 12345 not in subs_after


"""Pipeline: ingest path, dedup racing, filtering, persistence, reporting.

The scrape path is exercised through ``_finish`` rather than by launching a
browser: the orchestration logic (dedup, new flags, ordering, error isolation)
is what needs testing, and it is identical for scraped and ingested listings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobreach.config import Sources
from jobreach.pipeline import SearchRequest, ingest, query, stats
from jobreach.store import SQLiteJobRepository


@pytest.fixture()
def repo(db_path: Path):
    with SQLiteJobRepository(db_path) as repository:
        yield repository


def record(url: str, **overrides: Any) -> dict[str, Any]:
    payload = {"title": "UI Designer", "company": "Acme", "url": url, "location": "Tokyo"}
    payload.update(overrides)
    return payload


def request(**overrides: Any) -> SearchRequest:
    payload: dict[str, Any] = {"location": None, "sources": Sources.parse("indeed")}
    payload.update(overrides)
    return SearchRequest(**payload)


def test_ingest_reports_new_then_not_new(repo: SQLiteJobRepository):
    first = ingest([record("https://i.test/1")], request(), repo)
    assert first["summary"]["total"] == 1
    assert first["summary"]["new"] == 1
    assert first["summary"]["saved"] == 1
    assert first["jobs"][0]["is_new"] is True

    second = ingest([record("https://i.test/1")], request(), repo)
    assert second["summary"]["new"] == 0
    assert second["summary"]["saved"] == 0
    assert second["jobs"][0]["is_new"] is False


def test_ingest_skips_invalid_records_without_failing(repo: SQLiteJobRepository):
    result = ingest(
        [record("https://i.test/1"), {"title": "no url"}, "nonsense"],
        request(),
        repo,
    )
    assert result["summary"]["total"] == 1
    assert "ingest_skipped" in result["summary"]["errors"]


def test_ingest_deduplicates_within_one_batch(repo: SQLiteJobRepository):
    result = ingest(
        [record("https://i.test/1"), record("https://i.test/1/#frag")],
        request(),
        repo,
    )
    assert result["summary"]["total"] == 1
    assert result["summary"]["new"] == 1


def test_save_false_leaves_the_store_untouched(repo: SQLiteJobRepository):
    ingest([record("https://i.test/1")], request(save=False), repo)
    assert repo.count() == 0


def test_new_only_hides_known_listings(repo: SQLiteJobRepository):
    ingest([record("https://i.test/1")], request(), repo)
    result = ingest(
        [record("https://i.test/1"), record("https://i.test/2")],
        request(new_only=True),
        repo,
    )
    assert result["summary"]["total"] == 2
    assert result["summary"]["shown"] == 1
    assert result["jobs"][0]["url"] == "https://i.test/2"


def test_limit_caps_shown_but_not_total(repo: SQLiteJobRepository):
    result = ingest(
        [record(f"https://i.test/{i}") for i in range(5)], request(limit=2), repo
    )
    assert result["summary"]["total"] == 5
    assert result["summary"]["shown"] == 2


def test_new_listings_are_ordered_first(repo: SQLiteJobRepository):
    ingest([record("https://i.test/old", title="Old")], request(), repo)
    result = ingest(
        [record("https://i.test/old", title="Old"), record("https://i.test/new", title="New")],
        request(),
        repo,
    )
    assert [job["title"] for job in result["jobs"]] == ["New", "Old"]


def test_validation_filters_by_profile(repo: SQLiteJobRepository):
    result = ingest(
        [
            record("https://i.test/1", title="UI Designer"),
            record("https://i.test/2", title="Sales Manager"),
        ],
        request(validation="local", validation_profile="designer"),
        repo,
    )
    assert result["summary"]["total"] == 1
    assert result["summary"]["filter"]["kept"] == 1
    assert result["summary"]["filter"]["rejected"] == 1


def test_by_platform_summary_counts_new(repo: SQLiteJobRepository):
    ingest([record("https://i.test/1")], request(), repo)
    result = ingest(
        [
            record("https://i.test/1"),
            record("https://i.test/2", source_platform="wantedly"),
        ],
        request(),
        repo,
    )
    by_platform = result["summary"]["by_platform"]
    assert by_platform["indeed"] == {"total": 1, "new": 0}
    assert by_platform["wantedly"] == {"total": 1, "new": 1}


def test_run_history_is_written(repo: SQLiteJobRepository):
    ingest([record("https://i.test/1")], request(), repo)
    payload = stats(repo)
    assert payload["last_run"]["mode"] == "ingest"
    assert payload["last_run"]["total"] == 1
    assert len(payload["recent_runs"]) == 1


def test_query_reads_back_stored_jobs(repo: SQLiteJobRepository):
    ingest(
        [record("https://i.test/1", title="UI Designer")], request(), repo
    )
    jobs = query(repo, limit=10)
    assert len(jobs) == 1
    assert jobs[0]["title"] == "UI Designer"


def test_query_filters_by_source(repo: SQLiteJobRepository):
    ingest([record("https://i.test/1")], request(), repo)
    assert query(repo, source="indeed", limit=10)
    assert not query(repo, source="wantedly", limit=10)


def test_scrape_request_rejects_indeed():
    """`indeed` is ingest-only; asking for it as a scraper must be explicit."""
    import asyncio

    from jobreach.pipeline import search

    with SQLiteJobRepository(":memory:") as repo:
        result = asyncio.run(search(request(), repo))
    assert "sources" in result["summary"]["errors"]

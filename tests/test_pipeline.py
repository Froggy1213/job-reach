"""Pipeline: ingest path, dedup racing, filtering, persistence, reporting.

The scrape path is exercised through ``_finish`` rather than by launching a
browser: the orchestration logic (dedup, new flags, ordering, error isolation)
is what needs testing, and it is identical for scraped and ingested listings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jobreach import pipeline
from jobreach.config import Sources
from jobreach.domain import JobPosting
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
    # Distinct titles on purpose: identical cards are one listing now (see the
    # content-dedupe section below), which is not what this test is about.
    result = ingest(
        [record(f"https://i.test/{i}", title=f"Role {i}") for i in range(5)],
        request(limit=2),
        repo,
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


def test_validation_reaches_the_listing_body(repo: SQLiteJobRepository):
    """The body text has to survive the trip to the filter.

    It did not: the payload carried only title/company/location/url, so a board
    whose titles are synthesised from an occupation code (Mynavi) fed the filter
    nothing but its own search term and every listing came back "keep".
    """
    result = ingest(
        [record("https://i.test/1", title="UI Designer", description="ゲーム企画の募集です")],
        request(validation="local", validation_profile="designer"),
        repo,
    )
    assert result["summary"]["total"] == 0
    assert result["summary"]["filter"]["rejected"] == 1


def test_a_synthetic_title_does_not_survive_the_filter(repo: SQLiteJobRepository, monkeypatch):
    """The whole Mynavi chain: the scraper's flag has to reach the filter.

    A card filed under 415 is titled "<company> (WEBデザイナー)" by the scraper
    itself, so the title names the very word the designer profile looks for. If
    that title is counted as evidence the listing keeps itself, and every card
    the board miscategorised comes back as a design job.
    """
    captured: dict[str, Any] = {}
    real_filter_jobs = pipeline.filter_jobs

    def spy(jobs, **kwargs):
        captured["jobs"] = jobs
        return real_filter_jobs(jobs, **kwargs)

    monkeypatch.setattr("jobreach.pipeline.filter_jobs", spy)
    job = JobPosting(
        title="Acme (WEBデザイナー)",
        company="Acme",
        url="https://job.mynavi.jp/corp1",
        location="Japan",
        source_platform="mynavi2027",
        description="法人向け商材の営業。既存顧客のフォローが中心です。",
        title_is_synthetic=True,
    )
    result = pipeline._finish(
        [job],
        {},
        request(validation="local", validation_profile="designer"),
        repo,
        run_id=repo.start_run("search", None, None, ["mynavi2027"]),
        mode="search",
        sources=["mynavi2027"],
    )

    assert captured["jobs"][0]["description"] == job.description
    assert captured["jobs"][0]["title_is_synthetic"] is True
    assert result["summary"]["total"] == 0
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


def test_search_isolates_a_failing_board(monkeypatch: pytest.MonkeyPatch):
    """One broken board must never discard the boards that worked.

    The scrape path is stubbed at the registry (``build_scrapers``) rather than
    by launching a browser: what needs pinning is the orchestration — a failure
    is reported per board, in ``summary.errors``, while the other board's
    listings still reach the caller.
    """
    import asyncio

    from jobreach.domain import JobPosting, SourcePlatform
    from jobreach.pipeline import search
    from jobreach.scrapers.base import BaseScraper

    class Working(BaseScraper):
        @property
        def platform(self) -> SourcePlatform:
            return SourcePlatform.WANTEDLY

        async def fetch_jobs(self) -> list[JobPosting]:
            return [
                JobPosting(
                    title="UI Designer", company="Acme", url="https://w.test/1",
                    location="Tokyo", source_platform=self.platform,
                )
            ]

    class Broken(BaseScraper):
        @property
        def platform(self) -> SourcePlatform:
            return SourcePlatform.INDEED

        async def fetch_jobs(self) -> list[JobPosting]:
            from jobreach.errors import ScraperError

            raise ScraperError("the site served a bot challenge")

    def fake_build(names, **kwargs):
        return [Working(), Broken()]

    monkeypatch.setattr("jobreach.pipeline.build_scrapers", fake_build)

    with SQLiteJobRepository(":memory:") as repo:
        result = asyncio.run(search(request(sources=Sources.parse("wantedly,indeed")), repo))

    assert result["summary"]["total"] == 1
    assert [job["title"] for job in result["jobs"]] == ["UI Designer"]
    assert "bot challenge" in result["summary"]["errors"]["indeed"]


# --------------------------------------------------------------------------- #
# Envelope hygiene: the opt-in description, and the content collapse
# --------------------------------------------------------------------------- #

#: Two dates far enough apart that "the newest card wins" is not a race.
OLDER = datetime(2026, 1, 1, tzinfo=UTC)
NEWER = datetime(2026, 6, 1, tzinfo=UTC)


def posting(url: str, **overrides: Any) -> JobPosting:
    """A JobPosting for the collapse tests — same company/title unless overridden."""
    payload: dict[str, Any] = {
        "title": "UI Designer",
        "company": "TopEyes",
        "url": url,
        "location": "Tokyo",
        "source_platform": "indeed",
    }
    payload.update(overrides)
    return JobPosting(**payload)


def finish(
    jobs: list[JobPosting], repo: SQLiteJobRepository, **overrides: Any
) -> dict[str, Any]:
    """Drive ``_finish`` directly — the scrape path without a browser.

    Timestamps matter to the collapse, and ``ingest`` cannot set ``posted_at``,
    so these tests hand the pipeline real postings instead.
    """
    return pipeline._finish(
        jobs,
        {},
        request(**overrides),
        repo,
        run_id=repo.start_run("search", None, None, ["indeed"]),
        mode="search",
        sources=["indeed"],
    )


def test_the_description_is_returned_only_when_asked_for(repo: SQLiteJobRepository):
    """``detail`` is the whole point of the flag: opt-in, and expensive."""
    body = "Figma でプロダクトの UI を設計します。デザインシステムの運用も担当。"
    quiet = ingest([record("https://i.test/1", description=body)], request(), repo)
    assert "description" not in quiet["jobs"][0]

    told = ingest(
        [record("https://i.test/1", description=body)], request(detail=True), repo
    )
    assert told["jobs"][0]["description"] == body


def test_two_cards_of_one_vacancy_collapse_to_one_listing(repo: SQLiteJobRepository):
    result = finish(
        [
            posting("https://i.test/1", posted_at=OLDER),
            posting("https://i.test/2", posted_at=NEWER),
        ],
        repo,
    )

    summary = result["summary"]
    # `total` is still the rows the run found; the collapse is reported next to
    # it, not by quietly shrinking it.
    assert summary["total"] == 2
    assert summary["unique"] == 1
    assert summary["hidden_duplicates"] == 1
    assert summary["shown"] == 1
    assert summary["new"] == 1
    assert summary["saved"] == 2, "both URLs are stored — only the report collapses"

    (job,) = result["jobs"]
    assert job["url"] == "https://i.test/2", "the newest card is the representative"
    assert job["duplicates"] == 1
    assert job["duplicate_urls"] == ["https://i.test/1"]


def test_collapsing_ignores_case_width_and_spacing(repo: SQLiteJobRepository):
    """These boards spell one employer three ways; all three are one vacancy."""
    result = finish(
        [
            posting("https://i.test/1", company="TopEyes", title="UI Designer"),
            posting("https://i.test/2", company="ＴｏｐＥｙｅｓ", title="ui  designer"),
            posting("https://i.test/3", company=" topEyes ", title="UI Designer"),
        ],
        repo,
    )
    assert result["summary"]["total"] == 3
    assert result["summary"]["unique"] == 1
    assert result["summary"]["hidden_duplicates"] == 2
    assert result["jobs"][0]["duplicates"] == 2
    assert sorted(result["jobs"][0]["duplicate_urls"]) == ["https://i.test/1", "https://i.test/2"]


def test_different_boards_never_collapse(repo: SQLiteJobRepository):
    """Two boards carrying the same job are two sources, not a duplicate."""
    result = finish(
        [
            posting("https://i.test/1", source_platform="indeed"),
            posting("https://w.test/1", source_platform="wantedly"),
        ],
        repo,
    )
    assert result["summary"]["unique"] == 2
    assert all(job["duplicates"] == 0 for job in result["jobs"])
    assert all(job["duplicate_urls"] == [] for job in result["jobs"])


def test_a_new_card_makes_the_kept_listing_new(repo: SQLiteJobRepository):
    """``summary.new`` has to be reachable by counting ``is_new`` in ``jobs``."""
    finish([posting("https://i.test/old", posted_at=NEWER)], repo)  # stored
    result = finish(
        [
            posting("https://i.test/old", posted_at=NEWER),  # known
            posting("https://i.test/new", posted_at=OLDER),  # first time seen
        ],
        repo,
    )

    (job,) = result["jobs"]
    assert job["url"] == "https://i.test/old", "the newest card is still kept"
    assert job["is_new"] is True, "a kept listing that hides a new card is new"
    assert result["summary"]["new"] == 1
    assert result["summary"]["new"] == sum(1 for item in result["jobs"] if item["is_new"])


def test_dedupe_false_returns_every_row_untouched(repo: SQLiteJobRepository):
    result = finish(
        [
            posting("https://i.test/1", posted_at=OLDER),
            posting("https://i.test/2", posted_at=NEWER),
        ],
        repo,
        dedupe=False,
    )
    assert [job["url"] for job in result["jobs"]] == ["https://i.test/1", "https://i.test/2"]
    assert result["summary"]["total"] == 2
    assert result["summary"]["unique"] == 2
    assert result["summary"]["hidden_duplicates"] == 0
    for job in result["jobs"]:
        assert "duplicates" not in job, "a raw list must stay the shape it always was"
        assert "duplicate_urls" not in job


def test_the_limit_applies_to_the_collapsed_set(repo: SQLiteJobRepository):
    """``limit`` counts vacancies the caller reads, not cards on the board."""
    result = finish(
        [
            posting(f"https://i.test/{i}", title="UI Designer") for i in range(3)
        ]
        + [posting("https://i.test/x", title="Backend Engineer")],
        repo,
        limit=2,
    )
    assert result["summary"]["total"] == 4
    assert result["summary"]["unique"] == 2
    assert result["summary"]["shown"] == 2
    assert {job["title"] for job in result["jobs"]} == {"UI Designer", "Backend Engineer"}


def test_query_collapses_stored_rows_and_returns_the_body_on_request(
    repo: SQLiteJobRepository,
):
    """The ``job_list`` path: the store pages, then the page is collapsed."""
    finish(
        [
            posting("https://i.test/1", description="Figma で UI を設計します。"),
            posting("https://i.test/2", posted_at=NEWER, description="同じ求人の別カード。"),
        ],
        repo,
    )

    rows = query(repo, limit=10)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://i.test/2"
    assert rows[0]["duplicates"] == 1
    assert rows[0]["duplicate_urls"] == ["https://i.test/1"]
    assert "description" not in rows[0]

    detailed = query(repo, limit=10, detail=True)
    assert detailed[0]["description"] == "同じ求人の別カード。"

    raw = query(repo, limit=10, dedupe=False)
    assert len(raw) == 2
    assert all("duplicates" not in item for item in raw)


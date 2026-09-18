"""Persistence: dedup key, "new since last run", queries and run history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobreach.domain import JobPosting, SourcePlatform
from jobreach.store import SQLiteJobRepository, normalize_url


def job(url: str, **overrides: object) -> JobPosting:
    payload = {
        "title": "UI Designer",
        "company": "Acme",
        "url": url,
        "location": "Tokyo",
        "source_platform": "wantedly",
    }
    payload.update(overrides)
    return JobPosting(**payload)  # type: ignore[arg-type]


# --- normalize_url ---------------------------------------------------------


def test_normalize_lowercases_host_and_drops_fragment():
    assert normalize_url("HTTPS://Example.COM/Jobs/1/#top") == "https://example.com/Jobs/1"


def test_normalize_keeps_query_because_indeed_ids_live_there():
    url = "https://jp.indeed.com/viewjob?jk=abc123"
    assert normalize_url(url) == url


def test_normalize_strips_trailing_slash():
    assert normalize_url("https://x.test/a/") == "https://x.test/a"


def test_normalize_empty_path_becomes_root():
    assert normalize_url("https://x.test") == "https://x.test/"


# --- save / new flag -------------------------------------------------------


def test_first_save_reports_everything_as_new(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        assert repo.save_many([job("https://x.test/1"), job("https://x.test/2")]) == 2


def test_resaving_same_url_is_not_new(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        repo.save_many([job("https://x.test/1")])
        assert repo.save_many([job("https://x.test/1")]) == 0
        assert repo.count() == 1


def test_duplicate_inside_one_batch_counts_once(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        assert repo.save_many([job("https://x.test/1"), job("https://x.test/1")]) == 1


def test_url_normalisation_makes_cosmetic_duplicates_collide(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        assert repo.save_many([job("https://x.test/jobs/1")]) == 1
        assert repo.save_many([job("https://X.test/jobs/1/#apply")]) == 0
        assert repo.count() == 1


def test_existing_urls_is_normalised(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        repo.save_many([job("https://x.test/jobs/1")])
        found = repo.existing_urls(["https://x.test/jobs/1", "https://x.test/jobs/2"])
        assert found == {"https://x.test/jobs/1"}


def test_metadata_is_refreshed_on_resave(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        repo.save_many([job("https://x.test/1", title="Old title")])
        repo.save_many([job("https://x.test/1", title="New title")])
        assert repo.query(limit=1)[0].title == "New title"


def test_salary_is_not_overwritten_by_null(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        repo.save_many([job("https://x.test/1", salary="600万")])
        repo.save_many([job("https://x.test/1")])
        assert repo.query(limit=1)[0].salary == "600万"


# --- queries ---------------------------------------------------------------


def test_description_round_trips_and_stays_out_of_the_default_wire_format(
    db_path: Path,
):
    """The body is stored either way; it is only *reported* on request."""
    body = "Figma でプロダクトの UI を設計します。"
    with SQLiteJobRepository(db_path) as repo:
        repo.save_many([job("https://x.test/1", description=body)])
        (stored,) = repo.query(limit=1)

    assert stored.description == body, "the store keeps the body text"
    assert "description" not in stored.to_dict()
    assert stored.to_dict(detail=True)["description"] == body


def test_query_filters_by_source_and_text(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        repo.save_many(
            [
                job("https://x.test/1", title="UI Designer"),
                job(
                    "https://x.test/2",
                    title="Backend Engineer",
                    company="Foo",
                    source_platform=SourcePlatform.LINKEDIN,
                ),
            ]
        )
        assert len(repo.query(source=SourcePlatform.LINKEDIN)) == 1
        assert len(repo.query(text="designer")) == 1
        assert len(repo.query(text="foo")) == 1
        assert len(repo.query(text="nothing")) == 0


def test_query_pages(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        repo.save_many([job(f"https://x.test/{i}", title=f"Role {i}") for i in range(5)])
        assert len(repo.query(limit=2)) == 2
        assert len(repo.query(limit=2, offset=4)) == 1


def test_new_since_filters_by_first_seen(db_path: Path):
    future = datetime.now(UTC) + timedelta(days=1)
    with SQLiteJobRepository(db_path) as repo:
        repo.save_many([job("https://x.test/1")])
        assert repo.query(new_since=future) == []
        assert len(repo.query(new_since=datetime(2000, 1, 1, tzinfo=UTC))) == 1


# --- stats and runs --------------------------------------------------------


def test_stats_aggregates_by_platform(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        repo.save_many(
            [
                job("https://x.test/1"),
                job("https://x.test/2", source_platform=SourcePlatform.INDEED),
            ]
        )
        payload = repo.stats()
        assert payload["total"] == 2
        assert payload["by_platform"]["wantedly"] == 1
        assert payload["database"].endswith("jobs.db")


def test_run_history_is_recorded(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        run_id = repo.start_run("search", "designer", "tokyo", ["wantedly"])
        repo.finish_run(run_id, total=3, new=2, saved=2, errors={"linkedin": "boom"})
        (run,) = repo.recent_runs()
        assert (run.mode, run.total, run.new, run.saved) == ("search", 3, 2, 2)
        assert run.errors == {"linkedin": "boom"}
        assert run.sources == ["wantedly"]
        assert run.finished_at is not None


def test_stats_exposes_the_last_run(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        run_id = repo.start_run("ingest", None, None, ["indeed"])
        repo.finish_run(run_id, total=1, new=1, saved=1, errors={})
        assert repo.stats()["last_run"]["mode"] == "ingest"


def test_repository_reopens_existing_database(db_path: Path):
    with SQLiteJobRepository(db_path) as repo:
        repo.save_many([job("https://x.test/1")])
    with SQLiteJobRepository(db_path) as repo:
        assert repo.count() == 1

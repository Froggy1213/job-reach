"""The pipeline: fetch → filter → deduplicate → persist → report.

This module is the whole application, and it is deliberately transport-free:
the CLI, the Hermes tools, and the tests all call :func:`search`,
:func:`ingest` and :func:`query`. Nothing here knows about argparse, JSON
printing, or the agent.

Two entry points produce listings:

``search``
    Runs the scrapers for the selected boards concurrently, isolating each
    board's failure so one broken site never sinks a run.

``ingest``
    Takes listings the *agent* fetched with a real browser (the only way to
    read Indeed Japan, which Cloudflare blocks headless) and pushes them
    through the identical dedupe/persist/report path. Keeping one shared path
    is the point: ingested and scraped listings behave exactly alike
    afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import settings
from .config import DEFAULT_LOCATION, Sources
from .domain import JobPosting, SourcePlatform, resolve_platform
from .errors import JobReachError
from .filters import filter_jobs
from .logging_setup import get_logger
from .scrapers import build_scrapers
from .store import JobRepository, normalize_url, open_repository

logger = get_logger("pipeline")


def configured_sources() -> Sources:
    """Board selection for a request that names none.

    Routed through :func:`jobreach.settings.default_sources` rather than
    ``Sources.parse(None)`` so a configured ``default_sources`` reaches every
    no-argument caller — the CLI's ``search``/``monitor``, the Hermes tools and
    :func:`run_search` — from one place. ``Sources.parse(None)`` still means
    :data:`jobreach.config.DEFAULT_SOURCES`; that is the last fallback *inside*
    the settings resolver, not a second default of its own.
    """
    return Sources.parse(settings.default_sources())


@dataclass(slots=True)
class SearchRequest:
    """Everything one search run needs. Defaults mirror the CLI defaults."""

    keyword: str | None = None
    location: str | None = DEFAULT_LOCATION
    sources: Sources = field(default_factory=configured_sources)
    limit: int | None = None
    new_only: bool = False
    save: bool = True
    headless: bool = True
    timeout_ms: int = 30_000
    #: ``"off"``, ``"local"`` or ``"llm"``.
    validation: str = "off"
    validation_profile: str = "designer"
    #: LLM provider overrides for ``validation="llm"``. ``None`` resolves from
    #: the environment, which keeps the key, the endpoint and the model
    #: describing the same provider instead of sending one vendor's key to
    #: another vendor's host.
    llm_model: str | None = None
    llm_base_url: str | None = None


async def search(
    request: SearchRequest,
    repository: JobRepository,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Scrape the selected boards and return the standard result envelope."""
    sources = request.sources
    run_id = repository.start_run(
        "search", request.keyword, request.location, sources.as_list()
    )

    jobs, errors = await _run_scrapers(request)
    return _finish(
        jobs,
        errors,
        request,
        repository,
        run_id=run_id,
        mode="search",
        sources=sources.as_list(),
    )


def ingest(
    records: Iterable[dict[str, Any]],
    request: SearchRequest,
    repository: JobRepository,
    *,
    default_source: str = "indeed",
) -> dict[str, Any]:
    """Push externally-fetched listings through the standard pipeline."""
    jobs: list[JobPosting] = []
    skipped = 0
    errors: dict[str, str] = {}

    for index, record in enumerate(records):
        try:
            job = JobPosting.from_dict({**record, "source_platform": record.get("source_platform") or default_source})
        except Exception as exc:  # noqa: BLE001 — a bad row must not sink the batch
            skipped += 1
            logger.debug("skipping ingest record", extra={"index": index, "error": str(exc)})
            continue
        jobs.append(job)

    if skipped:
        errors["ingest_skipped"] = f"{skipped} record(s) were invalid and are not counted"

    sources = Sources.parse(sorted({job.platform for job in jobs}) or [default_source])
    run_id = repository.start_run("ingest", None, None, sources.as_list())
    return _finish(
        jobs,
        errors,
        request,
        repository,
        run_id=run_id,
        mode="ingest",
        sources=sources.as_list(),
    )


async def _run_scrapers(request: SearchRequest) -> tuple[list[JobPosting], dict[str, str]]:
    """Run every scraper concurrently, collecting per-board failures."""
    names = list(request.sources.scraped)
    if not names:
        return [], {"sources": "no scrapable board selected (indeed is ingest-only)"}

    scrapers = build_scrapers(
        names,
        keyword=request.keyword,
        location=request.location,
        headless=request.headless,
        timeout_ms=request.timeout_ms,
    )
    results = await asyncio.gather(
        *(scraper.fetch_jobs() for scraper in scrapers), return_exceptions=True
    )

    jobs: list[JobPosting] = []
    errors: dict[str, str] = {}
    for scraper, result in zip(scrapers, results, strict=True):
        board = scraper.platform.value
        if isinstance(result, BaseException):
            message = f"{type(result).__name__}: {result}"
            # A missing dependency carries the exact command that fixes it —
            # keep that hint, it is the difference between a dead end and a fix.
            hint = getattr(result, "hint", "")
            errors[board] = f"{message}\n{hint}" if hint else message
            logger.warning("scraper failed", extra={"platform": board, "error": str(result)})
        else:
            jobs.extend(result)
            logger.info("scraper ok", extra={"platform": board, "jobs": len(result)})
    return jobs, errors


def _finish(
    jobs: list[JobPosting],
    errors: dict[str, str],
    request: SearchRequest,
    repository: JobRepository,
    *,
    run_id: int,
    mode: str,
    sources: Sequence[str],
) -> dict[str, Any]:
    """Filter, dedupe, flag new, persist, and build the result envelope."""
    filter_stats: dict[str, Any] = {}

    if request.validation != "off" and jobs:
        # The description is not decoration here. A board may synthesise the
        # title from its own occupation code (Mynavi titles every card
        # "<company> (WEBデザイナー)"), so for those boards the body text is the
        # only evidence a filter has; leaving it out made validation a no-op
        # that answered "keep" for the whole board. The flag travels with it so
        # the filter knows when the title must not be counted as evidence.
        payload = [
            {"title": job.title, "company": job.company, "location": job.location,
             "url": job.url, "description": job.description,
             "title_is_synthetic": job.title_is_synthetic}
            for job in jobs
        ]
        outcome = filter_jobs(
            payload,
            profile=request.validation_profile,
            mode=request.validation,
            base_url=request.llm_base_url,
            model=request.llm_model,
        )
        keep_urls = {record["url"] for record in outcome.kept}
        jobs = [job for job in jobs if job.url in keep_urls]
        filter_stats = outcome.stats
        logger.info("validation applied", extra=filter_stats)

    # Deduplicate within the run on the normalized URL, keeping first sight.
    unique: dict[str, JobPosting] = {}
    for job in jobs:
        unique.setdefault(normalize_url(job.url), job)

    existing = repository.existing_urls(unique.keys())
    enriched = [
        job.with_new_flag(normalize_url(job.url) not in existing)
        for job in unique.values()
    ]
    new_jobs = [job for job in enriched if job.is_new]

    saved = 0
    if new_jobs and request.save:
        saved = repository.save_many(new_jobs)

    by_platform: dict[str, dict[str, int]] = {}
    for job in enriched:
        bucket = by_platform.setdefault(job.platform, {"total": 0, "new": 0})
        bucket["total"] += 1
        bucket["new"] += int(job.is_new)

    ordered = sorted(enriched, key=lambda j: (not j.is_new, j.platform, j.title.lower()))
    if request.new_only:
        ordered = [job for job in ordered if job.is_new]
    shown = ordered[: request.limit] if request.limit else ordered

    repository.finish_run(
        run_id,
        total=len(enriched),
        new=len(new_jobs),
        saved=saved,
        errors=errors,
    )

    return {
        "mode": mode,
        "query": {
            "keyword": request.keyword,
            "location": request.location,
            "sources": list(sources),
        },
        "summary": {
            "total": len(enriched),
            "new": len(new_jobs),
            "saved": saved,
            "shown": len(shown),
            "by_platform": by_platform,
            "errors": errors,
            "filter": filter_stats,
        },
        "jobs": [job.to_dict() for job in shown],
    }


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def query(
    repository: JobRepository,
    *,
    text: str | None = None,
    source: str | SourcePlatform | None = None,
    limit: int = 25,
    offset: int = 0,
    new_since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read stored listings, newest first."""
    platform = resolve_platform(source) if source else None
    return [
        job.to_dict()
        for job in repository.query(
            text=text, source=platform, limit=limit, offset=offset, new_since=new_since
        )
    ]


def stats(repository: JobRepository, *, recent_runs: int = 5) -> dict[str, Any]:
    """Store statistics plus the tail of the run history."""
    payload = repository.stats()
    payload["recent_runs"] = [
        run.to_dict() for run in repository.recent_runs(limit=recent_runs)
    ]
    return payload


def open_db(path: str | Path | None = None) -> JobRepository:
    """Open the repository at *path*, falling back to the configured default."""
    from .config import default_db_path

    return open_repository(path or default_db_path())


# --------------------------------------------------------------------------- #
# Convenience wrapper used by the Hermes tools and the CLI
# --------------------------------------------------------------------------- #


def run_search(
    *,
    keyword: str | None = None,
    location: str | None = DEFAULT_LOCATION,
    sources: str | list[str] | None = None,
    limit: int | None = None,
    new_only: bool = False,
    save: bool = True,
    headless: bool = True,
    timeout_ms: int = 30_000,
    validation: str = "off",
    validation_profile: str = "designer",
    llm_model: str | None = None,
    llm_base_url: str | None = None,
    db: str | Path | None = None,
) -> dict[str, Any]:
    """Synchronous façade over :func:`search` — one call, connection managed."""
    request = SearchRequest(
        keyword=keyword,
        location=location,
        sources=Sources.parse(sources) if sources is not None else configured_sources(),
        limit=limit,
        new_only=new_only,
        save=save,
        headless=headless,
        timeout_ms=timeout_ms,
        validation=validation,
        validation_profile=validation_profile,
        llm_model=llm_model,
        llm_base_url=llm_base_url,
    )
    repository = open_db(db)
    try:
        return asyncio.run(search(request, repository))
    except JobReachError:
        raise
    finally:
        repository.close()


def utcnow() -> datetime:
    """Timezone-aware "now" — kept here so callers import one symbol."""
    return datetime.now(UTC)


__all__ = [
    "SearchRequest",
    "configured_sources",
    "ingest",
    "open_db",
    "query",
    "run_search",
    "search",
    "stats",
    "utcnow",
]

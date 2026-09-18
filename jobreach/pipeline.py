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

Two output-shaping flags exist because the consumer of the envelope is a model
on a context budget: ``detail`` (off by default) adds each listing's stored body
text, and ``dedupe`` (on by default) collapses the cards that are one vacancy on
one board. Neither changes what is stored — only what is reported.
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
    #: Return each listing's stored body text. Off by default: descriptions
    #: dominate the size of the envelope, so the caller has to ask (see
    #: :meth:`jobreach.domain.JobPosting.to_dict`).
    detail: bool = False
    #: Collapse cards that are the same vacancy on the same board (see
    #: :func:`collapse_duplicates`). On by default — an employer listing one
    #: job thirty times is the common case on these boards, not an edge case.
    dedupe: bool = True


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


def _recency(job: JobPosting) -> tuple[int, float]:
    """Order two cards of the same vacancy by "which one is the freshest".

    A card that carries ``posted_at`` always beats one that does not, because
    boards expose that field inconsistently (LinkedIn and Mynavi never do) and
    a missing date would otherwise be read as "very old". The timestamp then
    decides; ``scraped_at`` is the fallback because it *is* ``first_seen_at``
    for a row read back from the store (see ``store._row_to_job``).
    """
    moment = job.posted_at or job.scraped_at
    return (1 if job.posted_at else 0, moment.timestamp())


def collapse_duplicates(
    jobs: Sequence[JobPosting],
) -> list[tuple[JobPosting, list[str]]]:
    """Collapse content duplicates: one entry per (company, title, board).

    The URL dedupe upstream cannot see these: one employer lists one vacancy as
    dozens of cards, each with its own URL, so a seven-board search of 140
    listings can be far fewer jobs. Cards are grouped on
    :attr:`jobreach.domain.JobPosting.content_key` (normalised, so spacing and
    case do not hide a group) and each group becomes one ``(representative,
    collapsed_urls)`` pair, in first-encounter order.

    The representative is the newest card — see :func:`_recency` — and a tie
    keeps the first card encountered, so a run whose cards share one timestamp
    is stable. Its ``is_new`` is the OR of the group's: a caller that counts the
    ``is_new`` entries of ``jobs`` has to be able to reach ``summary.new``, and
    a group is only "new" if no row of it was stored before.

    The other descriptive fields (location, salary, description) come from the
    representative too: it is the same vacancy, and the freshest card is the
    one least likely to carry stale text.
    """
    groups: dict[tuple[str, str, str], list[JobPosting]] = {}
    for job in jobs:
        groups.setdefault(job.content_key, []).append(job)

    collapsed: list[tuple[JobPosting, list[str]]] = []
    for group in groups.values():
        newest = 0
        for index in range(1, len(group)):
            if _recency(group[index]) > _recency(group[newest]):
                newest = index
        representative = group[newest]
        if not representative.is_new and any(job.is_new for job in group):
            representative = representative.with_new_flag(True)
        collapsed.append(
            (
                representative,
                [job.url for index, job in enumerate(group) if index != newest],
            )
        )
    return collapsed


def _wire(
    job: JobPosting, *, detail: bool, collapsed_urls: list[str] | None
) -> dict[str, Any]:
    """One job's wire dict, plus its collapse evidence when *collapsed_urls* is given.

    ``None`` means "this envelope was not collapsed" and adds nothing, so the
    raw list a ``dedupe=false`` caller asked for is byte-for-byte the shape it
    always was. A collapsed group always reports both keys, ``duplicates`` 0
    included, so a caller never has to tell "no duplicates" from "not deduped".
    """
    payload = job.to_dict(detail=detail)
    if collapsed_urls is not None:
        payload["duplicates"] = len(collapsed_urls)
        payload["duplicate_urls"] = collapsed_urls
    return payload


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
    # by_platform keeps counting the rows this run found — the same thing
    # `total` counts — so a board's numbers still add up to the envelope's
    # total. The collapse is reported by `unique`/`hidden_duplicates`, not by
    # quietly shrinking a per-board count.
    for job in enriched:
        bucket = by_platform.setdefault(job.platform, {"total": 0, "new": 0})
        bucket["total"] += 1
        bucket["new"] += int(job.is_new)

    # Content dedupe sits between the new flag (a group's representative has to
    # be able to inherit it) and the sort/limit, so `limit` and `shown` count
    # the vacancies a caller reads rather than the cards. It does not touch the
    # persistence above: every URL is still stored, so a card that was hidden
    # today is not reported as new tomorrow.
    collapsed = collapse_duplicates(enriched) if request.dedupe else None
    if collapsed is None:
        display = list(enriched)
        collapsed_urls: dict[str, list[str]] = {}
    else:
        display = [job for job, _ in collapsed]
        collapsed_urls = {job.url: urls for job, urls in collapsed}

    ordered = sorted(display, key=lambda j: (not j.is_new, j.platform, j.title.lower()))
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
            # Counted on what is returned, not on the raw rows: a caller that
            # counts `is_new` in `jobs` has to be able to reach this number.
            "new": sum(1 for job in display if job.is_new),
            "saved": saved,
            "shown": len(shown),
            "unique": len(display),
            "hidden_duplicates": len(enriched) - len(display),
            "by_platform": by_platform,
            "errors": errors,
            "filter": filter_stats,
        },
        "jobs": [
            _wire(job, detail=request.detail, collapsed_urls=collapsed_urls.get(job.url))
            for job in shown
        ],
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
    detail: bool = False,
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    """Read stored listings, newest first.

    Collapsing happens *after* the store's page fetch, and it has to: the SQL
    pages on ``first_seen_at`` and cannot group rows by content, so the page is
    all the information this function has. The consequence is explicit rather
    than hidden — ``duplicates`` says what was folded away *within the fetched
    page*, and a vacancy split across two pages comes back as two cards with
    ``duplicates`` 0 each. Paging further is how a caller sees the rest.
    """
    platform = resolve_platform(source) if source else None
    jobs = repository.query(
        text=text, source=platform, limit=limit, offset=offset, new_since=new_since
    )
    if not dedupe:
        return [job.to_dict(detail=detail) for job in jobs]
    return [
        _wire(job, detail=detail, collapsed_urls=urls)
        for job, urls in collapse_duplicates(jobs)
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
    detail: bool = False,
    dedupe: bool = True,
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
        detail=detail,
        dedupe=dedupe,
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
    "collapse_duplicates",
    "configured_sources",
    "ingest",
    "open_db",
    "query",
    "run_search",
    "search",
    "stats",
    "utcnow",
]

"""Standalone job-search CLI — the headless core of Job Hunter.

Runs the same scrapers the Telegram bot uses, but without any bot,
token, or scheduler.  It scrapes the selected Japanese job boards,
deduplicates against the SQLite database, marks which listings are new
since the last run, persists the new ones, and prints the results as
human-readable text or JSON.

This module deliberately does NOT import ``config.settings`` (which
requires ``BOT_TOKEN``/``ADMIN_CHAT_ID``), so it can run in any
environment with just the scraping dependencies installed.

Examples::

    # Default: design jobs in Tokyo, both boards, human-readable
    python search_cli.py

    # Custom query + location, JSON for programmatic use
    python search_cli.py --keyword "frontend engineer" --location tokyo --json

    # Only Wantedly, only listings new since the last run, top 10
    python search_cli.py --source wantedly --new-only --limit 10

    # One-off search that must NOT touch the database
    python search_cli.py --keyword "designer" --no-save
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from database.engine import create_engine_and_session
from database.models import Base
from database.repository import normalize_url
from database.sqlalchemy_repository import SQLAlchemyJobRepository
from models.enums import SourcePlatform
from models.job_posting import JobPosting
from scrapers.base import BaseScraper
from scrapers.cli.linkedin import LinkedInScraper
from scrapers.implementations.mynavi2027 import Mynavi2027Scraper
from scrapers.implementations.wantedly import WantedlyScraper
from job_hunter.agent_filter import filter_jobs

# CLI source name -> scraper class.  Add a board here after registering
# it in ``models/enums.py`` and creating its scraper implementation.
_SCRAPERS: dict[str, type[BaseScraper]] = {
    "wantedly": WantedlyScraper,
    "mynavi2027": Mynavi2027Scraper,
    "linkedin": LinkedInScraper,
}

_PROJECT_ROOT = Path(__file__).resolve().parent
_DEFAULT_DB_URL = f"sqlite+aiosqlite:///{_PROJECT_ROOT / 'jobs.db'}"


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


def _build_scrapers(
    sources: list[str],
    keyword: str | None,
    location: str | None,
    headless: bool,
    timeout_ms: int,
) -> list[BaseScraper]:
    """Instantiate one scraper per selected source."""
    return [
        _SCRAPERS[name](
            headless=headless,
            timeout_ms=timeout_ms,
            keyword=keyword,
            location=location,
        )
        for name in sources
    ]


async def _fetch_all(
    scrapers: list[BaseScraper],
) -> tuple[list[JobPosting], dict[str, str]]:
    """Run every scraper concurrently; isolate per-scraper failures.

    A single failing board never blocks the others — its error is
    collected and reported instead of propagating.
    """
    results = await asyncio.gather(
        *(s.fetch_jobs() for s in scrapers),
        return_exceptions=True,
    )
    jobs: list[JobPosting] = []
    errors: dict[str, str] = {}
    for scraper, result in zip(scrapers, results):
        if isinstance(result, Exception):
            errors[scraper.platform.value] = str(result)
            print(
                f"warning: scraper '{scraper.platform.value}' failed: {result}",
                file=sys.stderr,
            )
        else:
            jobs.extend(result)
    return jobs, errors


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    """Gather jobs (scrape or ingest), dedup, persist, and print.

    Returns a process exit code.
    """
    engine, session_factory = create_engine_and_session(args.db)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        repo = SQLAlchemyJobRepository(session_factory)

        if args.ingest:
            jobs, errors = _read_ingest_jobs(args)
            sources = sorted({job.source_platform.value for job in jobs}) or [
                args.ingest_source
            ]
        else:
            scrapers = _build_scrapers(
                args.sources, args.keyword, args.location, not args.headful, args.timeout_ms
            )
            jobs, errors = await _fetch_all(scrapers)
            sources = args.sources

        # ---- Agent validation step ----
        if args.validate and jobs:
            raw_count = len(jobs)
            jobs = await _run_validation(args, jobs)
            logger.info(
                "Agent validation completed",
                extra={"before": raw_count, "after": len(jobs)},
            )

        exit_code = await _process_and_report(repo, jobs, args, errors, sources)

        # Optional: write to Obsidian
        if args.obsidian and jobs:
            _write_obsidian(args, jobs)

        return exit_code
    finally:
        await engine.dispose()


async def _process_and_report(
    repo: SQLAlchemyJobRepository,
    jobs: list[JobPosting],
    args: argparse.Namespace,
    errors: dict[str, str],
    sources: list[str],
) -> int:
    """Dedup within the run, mark new-since-last-run, persist, and print.

    Shared by scrape mode and ``--ingest`` mode so both get identical
    deduplication, new-flagging, persistence, and output.
    """
    # Deduplicate within this run by normalized URL (keep first seen).
    seen: set[str] = set()
    unique: list[tuple[JobPosting, str]] = []
    for job in jobs:
        norm = normalize_url(str(job.url))
        if norm in seen:
            continue
        seen.add(norm)
        unique.append((job, norm))

    existing = await repo.get_existing_urls([norm for _, norm in unique])

    enriched: list[tuple[JobPosting, bool]] = [
        (job, norm not in existing) for job, norm in unique
    ]
    new_jobs = [job for job, is_new in enriched if is_new]

    saved = 0
    if new_jobs and not args.no_save:
        await repo.save_many(new_jobs)
        saved = len(new_jobs)

    _print_results(args, enriched, errors=errors, saved=saved, sources=sources)

    # Non-zero only when everything failed and nothing came back.
    return 1 if errors and not unique else 0


# ---------------------------------------------------------------------------
# Ingest mode (browser-fetched listings, e.g. Indeed)
# ---------------------------------------------------------------------------


def _resolve_platform(name: str) -> SourcePlatform:
    """Map a source string to a ``SourcePlatform`` (raises on unknown)."""
    key = str(name).strip().lower()
    key = {"mynavi2027": "mynavi_2027"}.get(key, key)
    return SourcePlatform(key)


def _read_ingest_jobs(
    args: argparse.Namespace,
) -> tuple[list[JobPosting], dict[str, str]]:
    """Read job records as JSON from stdin and validate them.

    Accepts a JSON array of objects, or an object with a ``"jobs"`` array.
    Each record needs ``title`` and ``url``; ``company``, ``location``,
    ``salary`` and ``source_platform`` are optional (source defaults to
    ``--ingest-source``). Invalid records are skipped with a warning.
    """
    if sys.stdin.isatty():
        print(
            "error: --ingest reads JSON from stdin; pipe records in, e.g. "
            "echo '[{...}]' | ... --ingest",
            file=sys.stderr,
        )
        return [], {"ingest": "no piped stdin"}
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: --ingest expects JSON on stdin: {exc}", file=sys.stderr)
        return [], {"ingest": f"invalid JSON: {exc}"}

    records = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(records, list):
        print('error: --ingest JSON must be a list or {"jobs": [...]}', file=sys.stderr)
        return [], {"ingest": "expected a list of job records"}

    jobs: list[JobPosting] = []
    errors: dict[str, str] = {}
    skipped = 0
    for i, rec in enumerate(records):
        try:
            platform = _resolve_platform(rec.get("source_platform") or args.ingest_source)
            jobs.append(
                JobPosting(
                    title=str(rec["title"]).strip(),
                    company=str(rec.get("company") or "Unknown").strip(),
                    url=rec["url"],
                    location=str(rec.get("location") or "Japan").strip(),
                    source_platform=platform,
                    salary=(str(rec["salary"]).strip() if rec.get("salary") else None),
                )
            )
        except Exception as exc:
            skipped += 1
            print(f"warning: skipped ingest record #{i}: {exc}", file=sys.stderr)

    if skipped:
        errors["ingest_skipped"] = f"{skipped} record(s) invalid"
    return jobs, errors


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _job_dict(job: JobPosting, is_new: bool) -> dict:
    return {
        "title": job.title,
        "company": job.company,
        "url": str(job.url),
        "location": job.location,
        "salary": job.salary,
        "source_platform": job.source_platform.value,
        "is_new": is_new,
    }


def _counts_by_platform(enriched: list[tuple[JobPosting, bool]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for job, is_new in enriched:
        platform = job.source_platform.value
        bucket = counts.setdefault(platform, {"total": 0, "new": 0})
        bucket["total"] += 1
        if is_new:
            bucket["new"] += 1
    return counts


def _select_for_display(
    args: argparse.Namespace, enriched: list[tuple[JobPosting, bool]]
) -> list[tuple[JobPosting, bool]]:
    """Order new-first, then apply --new-only and --limit."""
    ordered = sorted(
        enriched,
        key=lambda t: (not t[1], t[0].source_platform.value, t[0].title.lower()),
    )
    if args.new_only:
        ordered = [t for t in ordered if t[1]]
    if args.limit:
        ordered = ordered[: args.limit]
    return ordered


def _print_results(
    args: argparse.Namespace,
    enriched: list[tuple[JobPosting, bool]],
    errors: dict[str, str],
    saved: int,
    sources: list[str],
) -> None:
    by_platform = _counts_by_platform(enriched)
    total = len(enriched)
    new_total = sum(1 for _, is_new in enriched if is_new)
    display = _select_for_display(args, enriched)

    if args.json:
        payload = {
            "query": {
                "keyword": args.keyword,
                "location": (args.location or "tokyo"),
                "sources": sources,
            },
            "summary": {
                "total": total,
                "new": new_total,
                "saved": saved,
                "shown": len(display),
                "by_platform": by_platform,
                "errors": errors,
            },
            "jobs": [_job_dict(job, is_new) for job, is_new in display],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    # ---- Human-readable ----
    kw = args.keyword if args.keyword else "design roles (default)"
    print(f"Japan Job Search — keyword: {kw} · location: {args.location or 'tokyo'}")
    print(f"Sources: {', '.join(sources)}")
    print()

    if not display:
        if errors:
            print("No results — every selected scraper failed (see warnings above).")
        else:
            print("No matching jobs found.")
        return

    current_platform: str | None = None
    for index, (job, is_new) in enumerate(display, start=1):
        platform = job.source_platform.value
        if platform != current_platform:
            current_platform = platform
            stats = by_platform.get(platform, {"total": 0, "new": 0})
            print(f"\n{platform.upper()} ({stats['total']} jobs, {stats['new']} new)")
        tag = "[NEW] " if is_new else "      "
        print(f"  {index:>2}. {tag}{job.title}  —  {job.company}")
        print(f"          {job.location}")
        print(f"          {job.url}")

    print()
    shown_note = f" (showing {len(display)})" if len(display) != total else ""
    print(f"Summary: {total} total · {new_total} new · {saved} saved to DB{shown_note}")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_sources(raw: str) -> list[str]:
    """Parse a --source value into an ordered, de-duplicated list."""
    if raw.strip().lower() == "all":
        return list(_SCRAPERS)
    chosen: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in _SCRAPERS:
            valid = ", ".join([*_SCRAPERS, "all"])
            raise argparse.ArgumentTypeError(
                f"unknown source '{name}'. Valid: {valid}"
            )
        if name not in chosen:
            chosen.append(name)
    if not chosen:
        raise argparse.ArgumentTypeError("no valid source given")
    return chosen


async def _run_validation(
    args: argparse.Namespace,
    jobs: list[JobPosting],
) -> list[JobPosting]:
    """Run agent validation filter and return only kept jobs."""
    from models.job_posting import JobPosting as JP

    job_dicts = [
        {
            "title": j.title,
            "company": j.company,
            "location": j.location,
            "url": str(j.url),
        }
        for j in jobs
    ]

    result = await filter_jobs(
        jobs=job_dicts,
        profile=args.validate_profile,
        mode=args.validate_mode,
    )

    if args.verbose:
        print(
            f"Agent filter ({result.stats['mode']}): "
            f"{result.stats['kept']} kept / {result.stats['rejected']} rejected "
            f"of {result.stats['total']} total",
            file=sys.stderr,
        )

    # Reconstruct JobPosting objects for kept jobs
    kept_urls = {j["url"] for j in result.kept}
    return [j for j in jobs if str(j.url) in kept_urls]


def _write_obsidian(args: argparse.Namespace, jobs: list[JobPosting]) -> None:
    """Write job listings as a Markdown note in Obsidian vault."""
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    vault = args.obsidian_path or os.environ.get("OBSIDIAN_VAULT_PATH", "")
    if not vault:
        # Auto-detect
        for candidate in [
            Path.home() / "Obsidian" / "Adi",
            Path.home() / "Documents" / "Obsidian Vault",
        ]:
            if (candidate / ".obsidian").is_dir():
                vault = str(candidate)
                break
    if not vault:
        print("warning: Obsidian vault not found — skipping write", file=sys.stderr)
        return

    out_dir = Path(vault) / "job_hunter"
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d_%H%M")
    kw_slug = (args.keyword or "design").replace(" ", "-").lower()[:30]
    filename = f"search_{kw_slug}_{ts}.md"
    filepath = out_dir / filename

    lines = [
        f"# Job Search: {args.keyword or 'design roles (default)'}",
        "",
        f"**Query:** `{args.keyword or 'design roles (default)'}` · "
        f"**Location:** `{args.location or 'tokyo'}`",
        f"**Sources:** {', '.join(args.sources)}",
        f"**Date:** {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"| Total | {len(jobs)} |",
        "|---|---|",
        "",
        "## Jobs",
        "",
    ]

    cur = None
    for i, job in enumerate(jobs, 1):
        p = job.source_platform.value
        if p != cur:
            cur = p
            lines.append(f"### {p.upper()}")
            lines.append("")
        lines.append(f"{i}. **[{job.title}]({job.url})**")
        lines.append(f"   Company: {job.company} | Location: {job.location}")
        if job.salary:
            lines.append(f"   Salary: {job.salary}")
        lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    print(f"Obsidian note written: {filepath}", file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="search_cli.py",
        description="Search Japanese job boards (Wantedly, Mynavi 2027) and "
        "report new listings. Runs the Job Hunter scrapers without the bot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-k",
        "--keyword",
        default=None,
        help="Free-text search query (e.g. 'frontend engineer'). "
        "Omit for the default design-role search.",
    )
    parser.add_argument(
        "-l",
        "--location",
        default=None,
        help="Location slug for Wantedly (default 'tokyo'; 'any' disables "
        "the filter). Mynavi 2027 is new-grad/nationwide and ignores this.",
    )
    parser.add_argument(
        "-s",
        "--source",
        dest="sources",
        type=_parse_sources,
        default=list(_SCRAPERS),
        help="Comma-separated boards to search: "
        f"{', '.join(_SCRAPERS)}, or 'all' (default).",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Cap the number of listings printed (new ones first).",
    )
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Print only listings new since the last run.",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not persist results — everything is reported as new.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON object instead of human-readable text.",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Skip scraping; read job records as JSON from stdin and run "
        "them through the same dedup/save/new-flag pipeline. Used to feed "
        "browser-fetched listings (e.g. Indeed) into the DB.",
    )
    parser.add_argument(
        "--ingest-source",
        default="indeed",
        help="Default source_platform for --ingest records lacking one "
        "(default: indeed).",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show the browser window (default is headless).",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=30_000,
        help="Playwright navigation timeout in milliseconds (default 30000).",
    )
    parser.add_argument(
        "--db",
        default=_DEFAULT_DB_URL,
        help="SQLAlchemy async DB URL (default: the project's jobs.db).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log scraper progress to stderr.",
    )
    parser.add_argument(
        "--obsidian",
        action="store_true",
        help="Write results as a Markdown note to the Obsidian vault "
        "(auto-detected or set via OBSIDIAN_VAULT_PATH env var).",
    )
    parser.add_argument(
        "--obsidian-path",
        default=None,
        help="Path to Obsidian vault (overrides auto-detection).",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run agent-based validation to filter out irrelevant jobs. "
        "Uses regex heuristics by default; add --validate-mode llm for LLM.",
    )
    parser.add_argument(
        "--validate-mode",
        choices=["local", "llm"],
        default="local",
        help="Validation mode: 'local' (regex, free) or 'llm' (needs DEEPSEEK_API_KEY).",
    )
    parser.add_argument(
        "--validate-profile",
        choices=["designer", "frontend", "engineering", "any"],
        default="designer",
        help="Target role profile for agent validation. 'any' keeps all tech/design roles.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.verbose:
        logging.basicConfig(
            level=logging.INFO,
            stream=sys.stderr,
            format="%(levelname)s %(name)s: %(message)s",
        )

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

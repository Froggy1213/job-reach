"""Job Hunter - Hermes Agent integration API.

Provides a clean, importable interface for Hermes to search jobs,
write results to Obsidian, and query the job database.

Usage from Hermes (execute_code)::

    import asyncio
    import sys
    sys.path.insert(0, "/Users/hikki/My_projects/job_hunter")
    from hermes import search, search_and_save_obsidian, get_stats

    results = asyncio.run(search(keyword="designer", location="tokyo"))
    print(results)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent


async def search(
    keyword: str | None = None,
    location: str | None = None,
    sources: list[str] | None = None,
    limit: int | None = None,
    new_only: bool = False,
    headless: bool = True,
    timeout_ms: int = 30_000,
    db_path: str | None = None,
    save_to_db: bool = True,
    validate: bool = False,
    validate_profile: str = "designer",
    validate_mode: str = "local",
) -> dict[str, Any]:
    """Search jobs across configured boards.

    Args:
        validate: Run agent-based validation to filter irrelevant jobs.
        validate_profile: ``"designer"``, ``"frontend"``, ``"engineering"``, ``"any"``.
        validate_mode: ``"local"`` (regex, free) or ``"llm"`` (needs API key).

    Returns:
        ``{summary: {total, new, saved, shown, by_platform, errors}, jobs: [...]}``
    """
    import asyncio
    from database.engine import create_engine_and_session
    from database.models import Base
    from database.repository import normalize_url
    from database.sqlalchemy_repository import SQLAlchemyJobRepository
    from search_cli import _SCRAPERS as SCRAPER_REGISTRY

    db_url = db_path or f"sqlite+aiosqlite:///{_PROJECT_ROOT / 'jobs.db'}"

    engine, session_factory = create_engine_and_session(db_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        repo = SQLAlchemyJobRepository(session_factory)

        if sources is None:
            sources = list(SCRAPER_REGISTRY)
        else:
            sources = [s for s in sources if s in SCRAPER_REGISTRY]

        scrapers = [
            SCRAPER_REGISTRY[name](
                headless=headless, timeout_ms=timeout_ms,
                keyword=keyword, location=location,
            )
            for name in sources
        ]

        results = await asyncio.gather(
            *(s.fetch_jobs() for s in scrapers), return_exceptions=True,
        )

        jobs = []
        errors: dict[str, str] = {}
        for scraper, result in zip(scrapers, results):
            if isinstance(result, Exception):
                errors[scraper.platform.value] = str(result)
            else:
                jobs.extend(result)

        # ---- Agent validation ----
        if validate and jobs:
            from job_hunter.agent_filter import filter_jobs as af_filter
            job_dicts = [
                {"title": j.title, "company": j.company, "location": j.location, "url": str(j.url)}
                for j in jobs
            ]
            af_result = await af_filter(
                jobs=job_dicts, profile=validate_profile, mode=validate_mode,
            )
            kept_urls = {j["url"] for j in af_result.kept}
            jobs = [j for j in jobs if str(j.url) in kept_urls]
            logger.info(
                "Agent validation: %d kept / %d rejected of %d",
                af_result.stats["kept"], af_result.stats["rejected"], af_result.stats["total"],
            )

        # Dedup
        seen = set()
        unique = []
        for job in jobs:
            norm = normalize_url(str(job.url))
            if norm in seen:
                continue
            seen.add(norm)
            unique.append((job, norm))

        existing = await repo.get_existing_urls([n for _, n in unique])
        enriched = [(job, norm not in existing) for job, norm in unique]
        new_list = [j for j, is_new in enriched if is_new]

        saved = 0
        if new_list and save_to_db:
            await repo.save_many(new_list)
            saved = len(new_list)

        ordered = sorted(enriched, key=lambda t: (not t[1], t[0].title.lower()))
        if new_only:
            ordered = [t for t in ordered if t[1]]
        if limit:
            ordered = ordered[:limit]

        counts = {}
        for job, is_new in enriched:
            p = job.source_platform.value
            b = counts.setdefault(p, {"total": 0, "new": 0})
            b["total"] += 1
            if is_new:
                b["new"] += 1

        return {
            "summary": {
                "total": len(enriched),
                "new": sum(1 for _, n in enriched if n),
                "saved": saved,
                "shown": len(ordered),
                "by_platform": counts,
                "errors": errors,
            },
            "jobs": [
                {
                    "title": job.title, "company": job.company,
                    "url": str(job.url), "location": job.location,
                    "salary": job.salary,
                    "source_platform": job.source_platform.value,
                    "is_new": is_new,
                }
                for job, is_new in ordered
            ],
        }
    finally:
        await engine.dispose()


async def search_and_save_obsidian(
    keyword: str | None = None,
    location: str | None = None,
    sources: list[str] | None = None,
    vault_path: str | None = None,
    **kwargs,
) -> str:
    """Search jobs and write results as a Markdown note in Obsidian.

    Returns the absolute path to the written note.
    """
    from datetime import datetime, timezone

    result = await search(keyword=keyword, location=location, sources=sources, **kwargs)

    if vault_path is None:
        vault_path = _find_obsidian_vault()
    vault = Path(vault_path)
    if not vault.exists():
        raise FileNotFoundError(f"Obsidian vault not found: {vault}")

    out_dir = vault / "job_hunter"
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d_%H%M")
    kw_slug = (keyword or "design").replace(" ", "-").lower()[:30]
    filename = f"search_{kw_slug}_{ts}.md"
    filepath = out_dir / filename

    lines = _format_markdown(result, keyword, location)
    filepath.write_text("\n".join(lines), encoding="utf-8")
    return str(filepath)


def _find_obsidian_vault() -> str:
    for p in [
        Path.home() / "Obsidian" / "Adi",
        Path.home() / "Documents" / "Obsidian Vault",
    ]:
        if (p / ".obsidian").is_dir():
            return str(p)
    return str(Path.home() / "Obsidian" / "Adi")


def _format_markdown(result, keyword, location):
    s = result["summary"]
    kw = keyword or "design roles (default)"
    loc = location or "tokyo"
    lines = [
        f"# Job Search: {kw}",
        "",
        f"**Query:** `{kw}` · **Location:** `{loc}`",
        f"**Sources:** {', '.join(s.get('by_platform', {}).keys()) or 'all'}",
        "",
        "| | Count |",
        "|---|---|",
        f"| Total found | {s['total']} |",
        f"| New | {s['new']} |",
        f"| Saved to DB | {s['saved']} |",
        "",
    ]
    if s.get("errors"):
        lines.append("## Errors")
        for p, e in s["errors"].items():
            lines.append(f"- **{p}**: {e}")
        lines.append("")

    if result["jobs"]:
        lines.append("## Jobs")
        lines.append("")
        cur = None
        for i, j in enumerate(result["jobs"], 1):
            if j["source_platform"] != cur:
                cur = j["source_platform"]
                lines.append(f"### {cur.upper()}")
                lines.append("")
            tag = "🆕 " if j["is_new"] else ""
            lines.append(f"{i}. {tag}**[{j['title']}]({j['url']})**")
            lines.append(f"   Company: {j['company']} | Location: {j['location']}")
            if j.get("salary"):
                lines.append(f"   Salary: {j['salary']}")
            lines.append("")
    else:
        lines.append("_No jobs found._")
        lines.append("")

    from datetime import datetime, timezone
    lines.extend([
        "---",
        f"_Generated by Job Hunter · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
    ])
    return lines


async def get_stats(db_path: str | None = None) -> dict[str, Any]:
    """Return per-platform job counts from the database."""
    from database.engine import create_engine_and_session
    from database.sqlalchemy_repository import SQLAlchemyJobRepository
    from models.enums import SourcePlatform

    db_url = db_path or f"sqlite+aiosqlite:///{_PROJECT_ROOT / 'jobs.db'}"
    engine, session_factory = create_engine_and_session(db_url)
    try:
        repo = SQLAlchemyJobRepository(session_factory)
        total = await repo.count_jobs()
        by_platform = {}
        for p in SourcePlatform:
            c = await repo.count_jobs(p)
            if c > 0:
                by_platform[p.value] = c
        return {"total": total, "by_platform": by_platform}
    finally:
        await engine.dispose()


async def get_recent_jobs(
    limit: int = 10,
    source: str | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent job listings from the database."""
    from database.engine import create_engine_and_session
    from database.sqlalchemy_repository import SQLAlchemyJobRepository
    from models.enums import SourcePlatform

    db_url = db_path or f"sqlite+aiosqlite:///{_PROJECT_ROOT / 'jobs.db'}"
    engine, session_factory = create_engine_and_session(db_url)
    try:
        repo = SQLAlchemyJobRepository(session_factory)
        platform = SourcePlatform(source) if source else None
        jobs = await repo.get_jobs_page(limit=limit, offset=0, source=platform)
        return [
            {
                "title": j.title, "company": j.company,
                "url": str(j.url), "location": j.location,
                "salary": j.salary,
                "source_platform": j.source_platform.value,
            }
            for j in jobs
        ]
    finally:
        await engine.dispose()

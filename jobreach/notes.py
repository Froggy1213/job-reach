"""Obsidian note rendering.

Notes are the primary *human* deliverable: the agent searches, the note
persists. The template intentionally matches what the previous
``japan-job-search`` skill produced — YAML frontmatter, one table per board,
a ``🏷️ NEW`` badge on fresh listings — so existing notes stay consistent.

Rendering is pure string work with no Obsidian dependency: a vault is just a
directory containing ``.obsidian``, and a note is just Markdown.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import NOTE_SUBFOLDER, find_vault
from .domain import PLATFORM_LABELS
from .errors import ConfigError

#: Characters that are unsafe or awkward in a filename.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def slugify(value: str, *, limit: int = 60) -> str:
    """Turn a query into a filesystem-safe, lowercase, hyphenated slug."""
    text = _UNSAFE.sub("", str(value)).strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return (text or "search")[:limit]


def note_path(
    *,
    keyword: str | None,
    location: str | None,
    vault: str | Path | None = None,
    subfolder: str = NOTE_SUBFOLDER,
    when: datetime | None = None,
) -> Path:
    """Compute the note's path: ``<vault>/<subfolder>/YYYY-MM-DD - kw - loc.md``."""
    root = Path(vault).expanduser() if vault else find_vault()
    if root is None:
        raise ConfigError(
            "no Obsidian vault found; set OBSIDIAN_VAULT_PATH (or pass a vault "
            "path) so the note has somewhere to live"
        )
    stamp = (when or datetime.now(UTC)).strftime("%Y-%m-%d")
    name = f"{stamp} - {slugify(keyword or 'design roles')} - {slugify(location or 'any', limit=30)}.md"
    return root / subfolder / name


def render_note(result: Mapping[str, Any], *, generated_at: datetime | None = None) -> str:
    """Render a search result envelope into the Obsidian Markdown template."""
    moment = generated_at or datetime.now(UTC)
    query = result.get("query") or {}
    summary = result.get("summary") or {}
    jobs: list[dict[str, Any]] = list(result.get("jobs") or [])

    keyword = query.get("keyword") or "design roles (default)"
    location = query.get("location") or "any"
    boards = _boards_present(jobs, summary)

    lines: list[str] = [
        "---",
        f"date: {moment.strftime('%Y-%m-%dT%H:%M')}",
        f'query: "{keyword}"',
        f'location: "{location}"',
        f"sources: [{', '.join(boards)}]",
        f"total: {summary.get('total', len(jobs))}",
        f"new: {summary.get('new', 0)}",
        "---",
        "",
        f"# Job Search: {keyword} in {location}",
        "",
        f"**Searched:** {moment.strftime('%Y-%m-%d %H:%M UTC')} · "
        f"**Sources:** {', '.join(_label(b) for b in boards) or '—'}",
        f"**Results:** {summary.get('total', len(jobs))} total · "
        f"{summary.get('new', 0)} new since last run",
        "",
        "---",
        "",
    ]

    if not jobs:
        lines.extend(["_No listings matched this run._", ""])
    for board in boards:
        rows = [job for job in jobs if job.get("source_platform") == board]
        if not rows:
            continue
        lines.extend([f"## {_label(board)} ({len(rows)} jobs)", ""])
        lines.extend(["| # | Title | Company | Location |", "|---|-------|---------|----------|"])
        for index, job in enumerate(rows, start=1):
            badge = "🏷️ NEW " if job.get("is_new") else ""
            title = _escape_pipes(str(job.get("title") or "Untitled"))
            url = str(job.get("url") or "")
            company = _escape_pipes(str(job.get("company") or "Unknown"))
            where = _escape_pipes(str(job.get("location") or ""))
            lines.append(f"| {index} | {badge}[{title}]({url}) | {company} | {where} |")
        lines.append("")

    errors = summary.get("errors") or {}
    if errors:
        lines.extend(["## Warnings", ""])
        for board, message in errors.items():
            lines.append(f"- **{board}**: {message}")
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            f"_Scraped at {moment.strftime('%Y-%m-%dT%H:%M:%SZ')} · "
            f"Query: \"{keyword}\" · Location: {location}_",
            "_New listings flagged with 🏷️ · Run again to track changes_",
            "",
        ]
    )
    return "\n".join(lines)


def write_note(
    result: Mapping[str, Any],
    *,
    keyword: str | None = None,
    location: str | None = None,
    vault: str | Path | None = None,
    subfolder: str = NOTE_SUBFOLDER,
) -> Path:
    """Render *result* and write it into the vault. Returns the note's path."""
    query = result.get("query") or {}
    target = note_path(
        keyword=keyword or query.get("keyword"),
        location=location or query.get("location"),
        vault=vault,
        subfolder=subfolder,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_note(result), encoding="utf-8")
    return target


def _boards_present(jobs: Iterable[dict[str, Any]], summary: Mapping[str, Any]) -> list[str]:
    """Boards to render, in the project's canonical board order (see ``domain``)."""
    order = [str(platform) for platform in PLATFORM_LABELS]
    seen = {str(job.get("source_platform")) for job in jobs}
    seen |= set((summary.get("by_platform") or {}).keys())
    ordered = [board for board in order if board in seen]
    ordered.extend(sorted(seen - set(order) - {""}))
    return ordered


#: Board id → display name, derived from the domain so the two cannot drift.
_LABELS: dict[str, str] = {str(platform): label for platform, label in PLATFORM_LABELS.items()}


def _label(board: str) -> str:
    """Display name for a board id; an unknown id falls back to the raw string."""
    return _LABELS.get(board, board)


def _escape_pipes(text: str) -> str:
    """Keep table rendering intact when a title contains ``|``."""
    return text.replace("|", "\\|")

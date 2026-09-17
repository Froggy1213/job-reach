"""Job Reach — Hermes Agent plugin entry point.

This module is the entire plugin surface. Hermes imports it as a package whose
search path is this directory, then calls :func:`register` with a context
object. Everything the plugin contributes is wired up here, in one place:

* **seven LLM-callable tools** (``schemas.py`` describes them, ``tools.py``
  implements them) — this is how the agent searches boards, ingests
  browser-fetched Indeed listings, writes Obsidian notes, and schedules
  monitoring;
* **one bundled skill**, ``job-reach:job-search``, holding the long-form
  procedure and board-specific knowledge that would bloat a tool description;
* **a ``/jobs`` slash command** for a quick in-session search;
* **``hermes job-reach ...``**, a thin CLI façade over the engine, so a human
  can do everything the agent can.

The heavy lifting lives in the ``jobreach`` subpackage, which uses only the
standard library. Nothing here imports Playwright, SQLAlchemy, or any other
third-party package into Hermes' own runtime — see ``jobreach/runtime.py``.
"""

from __future__ import annotations

import sys
from contextlib import suppress
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
SKILL_PATH = PLUGIN_DIR / "skills" / "job-search" / "SKILL.md"

#: Toolset name shown in ``hermes tools`` and used for per-platform gating.
TOOLSET = "job_reach"

#: Short trigger text shown in the plugin list (the full description lives in
#: ``plugin.yaml``).
TOOL_DESCRIPTIONS = {
    "job_search": "Search Japanese job boards and flag new listings",
    "job_ingest": "Feed browser-fetched listings (Indeed) into the pipeline",
    "job_list": "Read job listings already stored locally",
    "job_note": "Write a job-search result into the Obsidian vault",
    "job_status": "Report stored-listing and runtime readiness",
    "job_setup": "Install the scraping runtime (venv, Playwright, Chromium)",
    "job_cron": "Schedule recurring job monitoring through hermes cron",
}

TOOL_EMOJI = {
    "job_search": "🔎",
    "job_ingest": "📥",
    "job_list": "🗂️",
    "job_note": "📝",
    "job_status": "🩺",
    "job_setup": "⚙️",
    "job_cron": "⏰",
}

SKILL_DESCRIPTION = "Search and monitor Japanese job boards (Wantedly, Mynavi, LinkedIn, Indeed)."


def register(ctx) -> None:
    """Wire the plugin into Hermes. Called once per session by the plugin loader."""
    from .schemas import TOOL_SCHEMAS
    from .tools import HANDLERS

    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        handler = HANDLERS.get(name)
        if handler is None:  # pragma: no cover - guarded by the contract test
            continue
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            is_async=True,
            description=TOOL_DESCRIPTIONS.get(name, schema.get("description", "")),
            emoji=TOOL_EMOJI.get(name, "💼"),
        )

    _register_skill(ctx)
    _register_commands(ctx)


def _register_skill(ctx) -> None:
    """Publish the bundled skill as ``job-reach:job-search``.

    Plugin skills are explicit-load only — they never appear in the agent's
    ``<available_skills>`` index, so they cannot auto-trigger on their own.
    ``job_setup`` additionally copies the same file into
    ``$HERMES_HOME/skills/productivity/job-reach/``, where it *is* indexed and
    *does* auto-trigger. Both routes point at one source of truth.
    """
    if not SKILL_PATH.exists():  # pragma: no cover - packaging error
        return
    # A skill must never break tool registration, so every optional
    # registration below is best-effort.
    with suppress(Exception):
        ctx.register_skill(
            "job-search",
            SKILL_PATH,
            description="Japanese job-board search and monitoring",
        )


def _register_commands(ctx) -> None:
    """Add the ``/jobs`` slash command and the ``hermes job-reach`` CLI."""
    with suppress(Exception):
        ctx.register_command(
            "jobs",
            _slash_jobs,
            description="Search Japanese job boards (e.g. /jobs frontend engineer)",
            args_hint="[keyword]",
        )

    with suppress(Exception):
        ctx.register_cli_command(
            "job-reach",
            help="Search Japanese job boards, manage the store, install the runtime",
            setup_fn=_cli_setup,
            handler_fn=_cli_handler,
            description=(
                "Thin wrapper around the jobreach engine. Run "
                "`hermes job-reach --help` for the full subcommand list "
                "(search, ingest, list, stats, note, monitor, doctor, setup)."
            ),
        )


# --------------------------------------------------------------------------- #
# /jobs
# --------------------------------------------------------------------------- #


async def _slash_jobs(raw_args: str) -> str:
    """Run a quick search for ``/jobs <keyword>`` and return a short digest.

    Kept terse on purpose: a slash command's output goes straight into the
    conversation, so it prints the top hits and points at the note-writing tool
    rather than dumping a full envelope.
    """
    import asyncio
    import json

    from .tools import _invoke

    keyword = (raw_args or "").strip()
    sources = ["wantedly", "linkedin"] if keyword else ["wantedly", "mynavi2027"]
    args = ["search", "--json", "-n", "10", "--source", ",".join(sources)]
    if keyword:
        args += ["-k", keyword]

    payload, error = await asyncio.to_thread(_invoke, args, timeout=600.0)
    if error:
        return f"Job search failed: {error.get('error')}\n{error.get('hint', '')}".strip()

    result = json.loads(json.dumps(payload))
    summary = result.get("summary", {})
    jobs = result.get("jobs", [])
    if not jobs:
        return (
            f"No listings found for {keyword or 'the default design feed'}. "
            f"Boards checked: {', '.join(result.get('query', {}).get('sources', []))}."
        )

    lines = [
        f"**{keyword or 'design roles (default)'}** — "
        f"{summary.get('total', 0)} found, **{summary.get('new', 0)} new**"
    ]
    for job in jobs[:10]:
        badge = "NEW " if job.get("is_new") else ""
        lines.append(
            f"- {badge}[{job['title']}]({job['url']}) — {job['company']} "
            f"({job['source_label']})"
        )
    errors = summary.get("errors") or {}
    for board, message in errors.items():
        lines.append(f"- _{board} failed: {message}_")
    lines.append("\nAsk me to save this to Obsidian and I will write the note.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# hermes job-reach ...
# --------------------------------------------------------------------------- #


def _cli_setup(parser) -> None:
    """Declare ``hermes job-reach [args...]``, forwarding everything verbatim."""
    import argparse

    parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="subcommand and flags forwarded to the jobreach engine "
             "(search, ingest, list, stats, note, monitor, doctor, setup, "
             "install-skill, install-cron)",
    )


def _cli_handler(args, **_kwargs) -> int:
    """Run the engine with the forwarded arguments and stream its output."""
    from .jobreach.runtime import run_engine

    forwarded = list(getattr(args, "args", []) or [])
    if not forwarded:
        forwarded = ["--help"]

    try:
        result = run_engine(forwarded, timeout=1800.0)
    except Exception as exc:  # noqa: BLE001 — surface, never traceback at the user
        print(f"job-reach: could not run the engine: {exc}", file=sys.stderr)
        return 1

    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return int(result.returncode)

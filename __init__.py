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
  can do everything the agent can;
* **a ``post_tool_call`` hook** that journals this plugin's own invocations, so
  ``job_status`` can report what just happened without re-reading the chat.

The heavy lifting lives in the ``jobreach`` subpackage, which uses only the
standard library. Nothing here imports Playwright, SQLAlchemy, or any other
third-party package into Hermes' own runtime — see ``jobreach/runtime.py``.

Why the context is bound by closure
-----------------------------------
Hermes never hands a tool handler the ``PluginContext``: ``tools/registry.py``
calls ``entry.handler(args, **kwargs)`` with ``task_id``/``session_id``/
``user_task``, and ``PluginContext.register_tool`` has no context parameter
either. The context therefore reaches the handler from :func:`register`, which
is the only place that has it — see :func:`_with_context`.
"""

from __future__ import annotations

import functools
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Every key ``plugin.yaml``'s ``config_schema`` advertises. Each one is read
#: through ``ctx.get_config`` on every call and forwarded to the engine, so the
#: GUI can never show a setting that does nothing (see ``tools._settings`` and
#: ``jobreach.settings``, which turns it back into an environment variable).
#:
#: Defined in ``tools.py`` — the only consumer — because that module has to stay
#: importable without this one, and re-exported here (the redundant alias is the
#: explicit re-export idiom) because the manifest tests read the list from the
#: plugin root.
from .tools import SETTINGS_KEYS as SETTINGS_KEYS

logger = logging.getLogger(__name__)

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

#: The tools this plugin owns. ``post_tool_call`` fires for *every* tool in the
#: session, so the journal needs a cheap way to tell ours from everyone else's;
#: deriving it from the description map keeps that list single-sourced.
PLUGIN_TOOLS = frozenset(TOOL_DESCRIPTIONS)

SKILL_DESCRIPTION = "Search and monitor Japanese job boards (Wantedly, Mynavi, LinkedIn, Indeed)."

#: Hook name, named once so the registration and the manifest cannot drift.
POST_TOOL_CALL_HOOK = "post_tool_call"


# --------------------------------------------------------------------------- #
# Context binding
# --------------------------------------------------------------------------- #


def _with_context(handler: Callable[..., Any], ctx: Any) -> Callable[..., Any]:
    """Bind *ctx* to a tool *handler*, whatever else Hermes forwards.

    ``functools.partial`` would do for today's dispatch payload, but Hermes
    documents hook and dispatch payloads as *additive*: if a future keyword is
    ever named ``ctx``, a partial call would raise "got multiple values for
    keyword argument". Dropping the forwarded name keeps the plugin's own
    context authoritative and cannot break.
    """

    @functools.wraps(handler)
    async def bound(params: Any, **kwargs: Any) -> Any:
        kwargs.pop("ctx", None)
        return await handler(params, ctx=ctx, **kwargs)

    return bound


def _hook_with_context(callback: Callable[..., Any], ctx: Any) -> Callable[..., None]:
    """The same binding for a *synchronous* hook callback (hooks get no context)."""

    @functools.wraps(callback)
    def bound(**payload: Any) -> None:
        payload.pop("ctx", None)
        callback(**payload, ctx=ctx)

    return bound


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def register(ctx) -> None:
    """Wire the plugin into Hermes. Called once per session by the plugin loader.

    Never raises: the loader reads an exception here as "disable this plugin",
    which would take the tools down with it. A failure is logged with its
    traceback instead, and whatever did register keeps working.
    """
    try:
        _register_tools(ctx)
        _register_skill(ctx)
        _register_commands(ctx)
        _register_hooks(ctx)
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning("job-reach: plugin registration failed", exc_info=True)


def _register_tools(ctx) -> None:
    """Register the seven tools, each one bound to *ctx* (see ``_with_context``)."""
    from .schemas import TOOL_SCHEMAS
    from .tools import HANDLERS

    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        handler = HANDLERS.get(name)
        if handler is None:  # pragma: no cover - guarded by the contract test
            continue
        try:
            ctx.register_tool(
                name=name,
                toolset=TOOLSET,
                schema=schema,
                handler=_with_context(handler, ctx),
                is_async=True,
                description=TOOL_DESCRIPTIONS.get(name, schema.get("description", "")),
                emoji=TOOL_EMOJI.get(name, "💼"),
            )
        except Exception:  # noqa: BLE001 — one bad tool must not take the rest down
            logger.warning("job-reach: could not register tool %s", name, exc_info=True)


def _register_skill(ctx) -> None:
    """Publish the bundled skill as ``job-reach:job-search``.

    Plugin skills are explicit-load only — they never appear in the agent's
    ``<available_skills>`` index, so they cannot auto-trigger on their own.
    ``job_setup`` additionally copies the same file into
    ``$HERMES_HOME/skills/productivity/job-reach/``, where it *is* indexed and
    *does* auto-trigger. Both routes point at one source of truth.
    """
    if not SKILL_PATH.exists():  # pragma: no cover - packaging error
        logger.warning("job-reach: bundled skill is missing at %s", SKILL_PATH)
        return
    # A skill must never break tool registration, so this stays best-effort —
    # but it is no longer silent: a skill that fails to register simply never
    # triggers, and only the log can tell the user why.
    try:
        ctx.register_skill(
            "job-search",
            SKILL_PATH,
            description="Japanese job-board search and monitoring",
        )
    except Exception:  # noqa: BLE001 — see the comment above
        logger.warning("job-reach: could not register the bundled skill", exc_info=True)


def _register_commands(ctx) -> None:
    """Add the ``/jobs`` slash command and the ``hermes job-reach`` CLI."""
    try:
        ctx.register_command(
            "jobs",
            _slash_jobs,
            description="Search Japanese job boards (e.g. /jobs frontend engineer)",
            args_hint="[keyword]",
        )
    except Exception:  # noqa: BLE001 — a command is a convenience, not a tool
        logger.warning("job-reach: could not register the /jobs command", exc_info=True)

    try:
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
    except Exception:  # noqa: BLE001 — see the comment above
        logger.warning("job-reach: could not register the hermes job-reach command", exc_info=True)


def _register_hooks(ctx) -> None:
    """Subscribe the tool-call journal to ``post_tool_call``.

    The hook has to be declared in ``plugin.yaml``'s ``provides_hooks`` as well:
    a hook the manifest does not advertise is an observability hole the other
    way round (see the contract test).
    """
    ctx.register_hook(POST_TOOL_CALL_HOOK, _hook_with_context(record_tool_call, ctx))


# --------------------------------------------------------------------------- #
# post_tool_call → the tool-call journal
# --------------------------------------------------------------------------- #


def record_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int = 0,
    *,
    ctx: Any = None,
    **kwargs: Any,
) -> None:
    """Hook: remember this plugin's own tool calls (dump path, result counts, success).

    ``post_tool_call`` fires for every tool in the session, so the first thing
    this does is ignore anything that is not ours. Every documented payload
    field is declared with a default and the signature ends in ``**kwargs``
    because Hermes inspects it: a narrower callback only receives the fields it
    names, while ``**kwargs`` opts into fields added by a later release.

    Never raises. The guide would log a crashing hook and skip it; ours must not
    even be logged as broken, because a diagnostics journal is never worth
    interrupting the agent's tool loop.

    ``ctx`` is bound at registration time by :func:`_hook_with_context` —
    Hermes does not pass the plugin context to hooks either.
    """
    try:
        if tool_name not in PLUGIN_TOOLS:
            return  # not one of ours; journaling foreign tools is not this hook's job
        from .tools import journal_append, journal_entry

        journal_append(ctx, journal_entry(str(tool_name), result))
    except Exception:  # noqa: BLE001 — see the docstring
        logger.debug("job-reach: could not record the tool call", exc_info=True)


# --------------------------------------------------------------------------- #
# /jobs
# --------------------------------------------------------------------------- #


async def _slash_jobs(raw_args: str) -> str:
    """Run a quick search for ``/jobs <keyword>`` and return a short digest.

    Kept terse on purpose: a slash command's output goes straight into the
    conversation, so it prints the top hits and points at the note-writing tool
    rather than dumping a full envelope.

    It deliberately does *not* consult the configured defaults: the whole point
    of ``/jobs`` is the built-in design feed, and a slash command has no
    ``ctx`` to read settings from.
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

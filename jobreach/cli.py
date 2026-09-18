"""``jobreach`` — the command line that everything else drives.

The Hermes plugin never imports the engine; it spawns this CLI. That makes the
CLI the real public interface, so it has to be good on its own terms:

* ``--json`` writes one JSON object to **stdout** and nothing else (logs go to
  stderr), so the agent can parse it without filtering noise.
* every subcommand maps 1:1 onto a Hermes tool, which keeps the two surfaces
  from drifting.
* failures exit non-zero with the message on stderr, so ``subprocess`` callers
  can distinguish "no results" from "it broke".
* flags a Hermes setting can answer (``--source``, ``--subfolder``) take their
  argparse default from :mod:`jobreach.settings`, so a configured
  ``default_sources``/``note_subfolder`` reaches a bare ``jobreach search`` or
  ``jobreach note``. An explicit flag still wins, and ``--help`` prints the
  effective default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__, settings
from .config import DEFAULT_LOCATION, Sources
from .domain import PLATFORM_LABELS, SourcePlatform, resolve_platform
from .errors import JobReachError
from .logging_setup import setup_logging
from .pipeline import SearchRequest, ingest, open_db, query, search, stats
from .scrapers import available_sources

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def _default_source_flag() -> str:
    """The ``--source`` default: the configured boards, else the built-in feed.

    Read through :mod:`jobreach.settings` rather than using
    ``Sources.parse(None)`` directly, because ``search`` and ``monitor`` must
    answer "no ``--source``" identically — that is the one place a Hermes
    ``default_sources`` setting can reach the engine. It is still a plain
    string here so a bogus value fails as a *usage* error in the handler (see
    :func:`main`) instead of exploding while the parser is being built.
    """
    return ",".join(settings.default_sources())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobreach",
        description="Search Japanese job boards and track what is new.",
    )
    # Settings are read once per parser build — i.e. once per engine
    # subprocess — so an edit to ``config.yaml`` is picked up by the next tool
    # call rather than frozen at import time.
    default_source = _default_source_flag()
    default_subfolder = settings.note_subfolder()
    parser.add_argument("--version", action="version", version=f"jobreach {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="log progress to stderr (never to stdout)",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="suppress warnings on stderr",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    search_cmd = sub.add_parser("search", help="scrape the selected boards")
    search_cmd.add_argument("-k", "--keyword", default=None,
                            help="free-text query (omit for the default design feed)")
    search_cmd.add_argument("-l", "--location", default=DEFAULT_LOCATION,
                            help="Wantedly location slug, or 'any' (default: tokyo)")
    search_cmd.add_argument("-s", "--source", default=default_source,
                            help=f"comma list of {', '.join(available_sources())}, or 'all' "
                                 f"(default: {default_source})")
    search_cmd.add_argument("-n", "--limit", type=int, default=None,
                            help="cap how many listings are returned (new ones first)")
    search_cmd.add_argument("--new-only", action="store_true",
                            help="return only listings not seen in a previous run")
    search_cmd.add_argument("--no-save", action="store_true",
                            help="do not touch the database")
    search_cmd.add_argument("--headful", action="store_true",
                            help="show the browser window (debugging)")
    search_cmd.add_argument("--timeout-ms", type=int, default=30_000)
    search_cmd.add_argument("--validate", choices=["off", "local", "llm"], default="off",
                            help="relevance filter: 'local' is free, 'llm' needs an API key")
    search_cmd.add_argument("--profile", default="designer",
                            help="filter profile: designer, frontend, engineering, product, any")
    search_cmd.add_argument("--llm-model", default=None,
                            help="model for --validate llm (default: implied by the API key found)")
    search_cmd.add_argument("--llm-base-url", default=None,
                            help="OpenAI-compatible endpoint for --validate llm")
    search_cmd.add_argument("--json", action="store_true", help="emit JSON on stdout")
    search_cmd.add_argument("--db", default=None, help="path to the SQLite database")

    ingest_cmd = sub.add_parser(
        "ingest", help="push browser-fetched listings (Indeed) through the same pipeline"
    )
    ingest_cmd.add_argument("--file", default=None,
                            help="read records from a file instead of stdin")
    ingest_cmd.add_argument("--source", default="indeed",
                            help="default board for records without one (default: indeed)")
    ingest_cmd.add_argument("-n", "--limit", type=int, default=None)
    ingest_cmd.add_argument("--new-only", action="store_true")
    ingest_cmd.add_argument("--no-save", action="store_true")
    ingest_cmd.add_argument("--json", action="store_true")
    ingest_cmd.add_argument("--db", default=None)

    list_cmd = sub.add_parser("list", help="read stored listings")
    list_cmd.add_argument("-n", "--limit", type=int, default=25)
    list_cmd.add_argument("--offset", type=int, default=0)
    list_cmd.add_argument("-s", "--source", default=None)
    list_cmd.add_argument("-k", "--keyword", default=None,
                          help="substring match on title/company/location")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.add_argument("--db", default=None)

    stats_cmd = sub.add_parser("stats", help="counts by board plus recent runs")
    stats_cmd.add_argument("--json", action="store_true")
    stats_cmd.add_argument("--db", default=None)

    note_cmd = sub.add_parser("note", help="render a search result into an Obsidian note")
    note_cmd.add_argument("--input", default=None,
                          help="read a saved JSON result from this file (default: stdin)")
    note_cmd.add_argument("--vault", default=None, help="Obsidian vault path")
    note_cmd.add_argument("--subfolder", default=default_subfolder,
                          help=f"subfolder inside the vault (default: {default_subfolder})")
    note_cmd.add_argument("--json", action="store_true")

    monitor_cmd = sub.add_parser(
        "monitor",
        help="print a stable digest of newly found listings (for `hermes cron --monitor-script`)",
    )
    monitor_cmd.add_argument("-k", "--keyword", default=None)
    monitor_cmd.add_argument("-l", "--location", default=DEFAULT_LOCATION)
    monitor_cmd.add_argument("-s", "--source", default=default_source,
                             help=f"boards to watch (default: {default_source})")
    monitor_cmd.add_argument("--profile", default="designer")
    monitor_cmd.add_argument("--llm-model", default=None)
    monitor_cmd.add_argument("--llm-base-url", default=None)
    monitor_cmd.add_argument("--validate", choices=["off", "local", "llm"], default="local")
    monitor_cmd.add_argument("--db", default=None)

    doctor_cmd = sub.add_parser("doctor", help="report runtime readiness")
    doctor_cmd.add_argument("--json", action="store_true")

    setup_cmd = sub.add_parser("setup", help="create the scraping venv (one-time)")
    setup_cmd.add_argument("--force", action="store_true", help="recreate the venv")
    setup_cmd.add_argument("--no-browser", action="store_true",
                           help="skip the Chromium download")
    setup_cmd.add_argument("--json", action="store_true")

    skill_cmd = sub.add_parser(
        "install-skill", help="copy the bundled skill into ~/.hermes/skills so it auto-triggers"
    )
    skill_cmd.add_argument("--json", action="store_true")

    cron_cmd = sub.add_parser("install-cron", help="schedule a monitoring job via hermes cron")
    cron_cmd.add_argument("--schedule", default="0 9 * * *",
                          help="cron expression or interval like '6h' (default: 09:00 daily)")
    cron_cmd.add_argument("--keyword", default=None)
    cron_cmd.add_argument("--location", default=DEFAULT_LOCATION)
    cron_cmd.add_argument("--source", default=None,
                          help="boards to watch (default: the configured default_sources, "
                               "else the built-in feed)")
    cron_cmd.add_argument("--deliver", default=None,
                          help="delivery target: telegram, discord, origin, ...")
    cron_cmd.add_argument("--name", default="job-reach-monitor")
    cron_cmd.add_argument("--json", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging("INFO" if args.verbose else ("ERROR" if args.quiet else "WARNING"))

    handler = {
        "search": _cmd_search,
        "ingest": _cmd_ingest,
        "list": _cmd_list,
        "stats": _cmd_stats,
        "note": _cmd_note,
        "monitor": _cmd_monitor,
        "doctor": _cmd_doctor,
        "setup": _cmd_setup,
        "install-skill": _cmd_install_skill,
        "install-cron": _cmd_install_cron,
    }[args.command]

    try:
        return handler(args)
    except JobReachError as exc:
        hint = getattr(exc, "hint", "")
        print(f"error: {exc}", file=sys.stderr)
        if hint:
            print(hint, file=sys.stderr)
        return EXIT_FAILURE
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return 130


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _preflight(sources: Sources) -> None:
    """Fail fast when *no* selected board can possibly run.

    Without this, a missing browser stack means every Playwright board retries
    its navigation three times before reporting the same error — minutes of
    waiting for an answer we already knew. Crucially this only aborts when
    *every* scrapable board is blocked: a mixed selection still runs, so a
    working board is never sacrificed to a broken one.
    """
    from .errors import ConfigError
    from .scrapers import check_source_requirements

    requested = list(sources.scraped)
    if not requested:
        return
    blocked = check_source_requirements(requested)
    if blocked and len(blocked) == len(requested):
        board = next(iter(blocked))
        raise ConfigError(f"no selected board can run:\n{blocked[board]}")


def _cmd_search(args: argparse.Namespace) -> int:
    request = SearchRequest(
        keyword=args.keyword,
        location=args.location,
        sources=Sources.parse(args.source),
        limit=args.limit,
        new_only=args.new_only,
        save=not args.no_save,
        headless=not args.headful,
        timeout_ms=args.timeout_ms,
        validation=args.validate,
        validation_profile=args.profile,
        llm_model=args.llm_model,
        llm_base_url=args.llm_base_url,
    )
    _preflight(request.sources)
    repository = open_db(args.db)
    try:
        result = asyncio.run(search(request, repository))
    finally:
        repository.close()

    if args.json:
        _print_json(result)
    else:
        _print_text(result)
    return EXIT_FAILURE if _total_failure(result) else EXIT_OK


def _cmd_ingest(args: argparse.Namespace) -> int:
    raw = Path(args.file).read_text(encoding="utf-8") if args.file else _read_stdin()
    if not raw.strip():
        print("error: no JSON on stdin (pipe records in or use --file)", file=sys.stderr)
        return EXIT_USAGE
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON: {exc}", file=sys.stderr)
        return EXIT_USAGE

    records = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        print('error: expected a JSON list or {"jobs": [...]}', file=sys.stderr)
        return EXIT_USAGE

    request = SearchRequest(
        keyword=None,
        location=None,
        limit=args.limit,
        new_only=args.new_only,
        save=not args.no_save,
    )
    repository = open_db(args.db)
    try:
        result = ingest(records, request, repository, default_source=args.source)
    finally:
        repository.close()

    if args.json:
        _print_json(result)
    else:
        _print_text(result)
    return EXIT_FAILURE if _total_failure(result) else EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    repository = open_db(args.db)
    try:
        jobs = query(
            repository,
            text=args.keyword,
            source=args.source,
            limit=args.limit,
            offset=args.offset,
        )
        total = repository.count(
            source=resolve_platform(args.source) if args.source else None
        )
    finally:
        repository.close()

    if args.json:
        _print_json({"total": total, "shown": len(jobs), "jobs": jobs})
    else:
        if not jobs:
            print("No stored listings match.")
            return EXIT_OK
        for job in jobs:
            print(f"  {job['title']}  —  {job['company']}")
            print(f"      {job['location']} · {job['source_label']}")
            print(f"      {job['url']}")
        print(f"\n{total} stored listing(s); showing {len(jobs)}.")
    return EXIT_OK


def _cmd_stats(args: argparse.Namespace) -> int:
    repository = open_db(args.db)
    try:
        payload = stats(repository)
    finally:
        repository.close()

    if args.json:
        _print_json(payload)
        return EXIT_OK

    print(f"Database: {payload['database']}")
    print(f"Total listings: {payload['total']}")
    for board, count in (payload.get("by_platform") or {}).items():
        print(f"  {PLATFORM_LABELS.get(SourcePlatform(board), board):<14} {count}")
    if payload.get("oldest_first_seen"):
        print(f"Window: {payload['oldest_first_seen']} → {payload['newest_first_seen']}")
    last = payload.get("last_run")
    if last:
        print(
            f"Last run: {last['mode']} at {last['started_at']} — "
            f"{last['total']} found, {last['new']} new, {last['saved']} saved"
        )
    return EXIT_OK


def _cmd_note(args: argparse.Namespace) -> int:
    raw = Path(args.input).read_text(encoding="utf-8") if args.input else _read_stdin()
    if not raw.strip():
        print("error: no search result on stdin (pipe one in or use --input)", file=sys.stderr)
        return EXIT_USAGE
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON: {exc}", file=sys.stderr)
        return EXIT_USAGE

    from .notes import write_note

    path = write_note(result, vault=args.vault, subfolder=args.subfolder)
    if args.json:
        _print_json({"note": str(path)})
    else:
        print(f"Note written: {path}")
    return EXIT_OK


def _cmd_monitor(args: argparse.Namespace) -> int:
    """Emit a byte-stable digest of newly discovered listings.

    Stability matters: Hermes' ``cron --monitor-script`` hashes this output and
    skips the agent run when it is unchanged. So there are deliberately no
    timestamps, no counts of *all* listings, and no ordering that depends on
    anything but the URLs.
    """
    request = SearchRequest(
        keyword=args.keyword,
        location=args.location,
        sources=Sources.parse(args.source),
        new_only=True,
        save=True,
        validation=args.validate,
        validation_profile=args.profile,
        llm_model=args.llm_model,
        llm_base_url=args.llm_base_url,
    )
    _preflight(request.sources)
    repository = open_db(args.db)
    try:
        result = asyncio.run(search(request, repository))
    finally:
        repository.close()

    new_jobs = [job for job in result["jobs"] if job.get("is_new")]
    if not new_jobs:
        return EXIT_OK  # empty stdout → cron stays silent

    for job in sorted(new_jobs, key=lambda item: item["url"]):
        print(f"{job['url']}\t{job['title']}\t{job['company']}\t{job['source_platform']}")
    errors = result["summary"].get("errors") or {}
    for board in sorted(errors):
        print(f"ERROR\t{board}\t{errors[board]}")
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .runtime import diagnostics

    payload = diagnostics()
    if args.json:
        _print_json(payload)
        return EXIT_OK

    print(f"jobreach {payload['plugin_version']}")
    print(f"  plugin dir     {payload['plugin_dir']}")
    print(f"  data dir       {payload['data_dir']}")
    print(f"  database       {payload['database']}"
          f"{'' if payload['database_exists'] else '  (not created yet)'}")
    print(f"  interpreter    {payload['engine_interpreter']}  [{payload['engine_interpreter_source']}]")
    backend = payload.get("backend") or {}
    selected = backend.get("selected") or {}
    if selected:
        print(f"  browser        {selected.get('name')} — {selected.get('detail')}")
    else:
        print(f"  browser        NONE selected — {backend.get('problem') or 'not installed'}")
        for line in str(backend.get("hint") or "").splitlines():
            print(f"                 {line}")
    print(f"  scrapling      {backend.get('scrapling', {}).get('version') or 'not found'}")
    print(f"  venv           {payload['venv'] or '— (run `jobreach setup`)'}")
    print(f"  uv             {payload['uv'] or '— not found'}")
    print(f"  obsidian vault {payload['obsidian_vault'] or '— not found'}")
    print("  boards:")
    for board, info in payload["boards"].items():
        mark = "ok  " if info["ready"] else "FAIL"
        note = info.get("note") or info.get("problem") or ""
        head, *rest = str(note).splitlines() or [""]
        print(f"    [{mark}] {board:<11} {info.get('backend', ''):<12} {head}")
        # Continuation lines carry the actionable hint, indented so they read
        # as belonging to the board above them.
        for line in rest:
            print(f"         {' ' * 23} {line}")
    return EXIT_OK


def _cmd_setup(args: argparse.Namespace) -> int:
    from .runtime import setup_runtime

    report = setup_runtime(with_browser=not args.no_browser, force=args.force)
    if args.json:
        _print_json(report.to_dict())
    else:
        for step in report.steps:
            mark = "ok  " if step["ok"] else "FAIL"
            print(f"[{mark}] {step['step']}: {step['detail']}")
        if report.hint:
            print(report.hint, file=sys.stderr)
    return EXIT_OK if report.ok else EXIT_FAILURE


def _cmd_install_skill(args: argparse.Namespace) -> int:
    from .install import install_skill

    path = install_skill()
    if args.json:
        _print_json({"skill": str(path)})
    else:
        print(f"Skill installed: {path}")
    return EXIT_OK


def _cmd_install_cron(args: argparse.Namespace) -> int:
    from .install import install_cron

    payload = install_cron(
        schedule=args.schedule,
        keyword=args.keyword,
        location=args.location,
        sources=args.source,
        deliver=args.deliver,
        name=args.name,
    )
    if args.json:
        _print_json(payload)
    else:
        print(payload["message"])
        if not payload["created"]:
            print("Run this manually:\n  " + payload["command"], file=sys.stderr)
    return EXIT_OK if payload["created"] else EXIT_FAILURE


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_text(result: dict[str, Any]) -> None:
    query = result["query"]
    summary = result["summary"]
    jobs = result["jobs"]

    keyword = query.get("keyword") or "design roles (default)"
    print(f"Job search — {keyword} · {query.get('location') or 'any'}")
    print(f"Sources: {', '.join(query.get('sources') or [])}")
    print()

    if not jobs:
        if summary.get("errors"):
            print("No results — every selected board failed (see warnings below).")
        else:
            print("No matching listings found.")
    current = None
    for index, job in enumerate(jobs, start=1):
        if job["source_platform"] != current:
            current = job["source_platform"]
            bucket = summary["by_platform"].get(current, {"total": 0, "new": 0})
            print(f"\n{job['source_label'].upper()} ({bucket['total']} jobs, {bucket['new']} new)")
        tag = "[NEW] " if job["is_new"] else "      "
        print(f"  {index:>2}. {tag}{job['title']}  —  {job['company']}")
        print(f"          {job['location']}")
        print(f"          {job['url']}")

    print()
    print(
        f"Summary: {summary['total']} total · {summary['new']} new · "
        f"{summary['saved']} saved to DB"
        + (f" · showing {summary['shown']}" if summary["shown"] != summary["total"] else "")
    )
    for board, message in (summary.get("errors") or {}).items():
        print(f"  ! {board}: {message}", file=sys.stderr)


def _total_failure(result: dict[str, Any]) -> bool:
    """True only when every board failed and nothing came back at all."""
    summary = result["summary"]
    return bool(summary.get("errors")) and summary.get("total", 0) == 0


def _read_stdin() -> str:
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

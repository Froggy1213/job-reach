"""Tool handlers — the bridge between Hermes and the ``jobreach`` engine.

Every handler is deliberately thin. It has exactly three jobs:

1. translate tool arguments into ``jobreach`` CLI arguments,
2. run that CLI **in a subprocess** with the interpreter chosen by
   :mod:`jobreach.runtime`,
3. return a compact JSON string, turning a non-zero exit into a structured
   error the model can act on instead of a stack trace.

Why a subprocess rather than importing the engine
-------------------------------------------------
The engine is standard-library-only, so in principle these handlers could
``import jobreach``. They do not, for reasons that matter in production:

* scraping needs Playwright, which must not be installed into Hermes' own
  runtime venv (Python 3.14) where a bad wheel could break the agent itself;
* a scrape takes 30–90 seconds and several hundred MB of Chromium — a
  killable child process is far easier to reason about than a blocked agent;
* a crashing browser cannot take the agent down with it.

The cost is one ``fork`` per call, which is irrelevant next to a 60-second
scrape.

All handlers are ``async`` (registered with ``is_async=True``) and hand the
blocking subprocess to a worker thread, so a long search never stalls the
agent's event loop.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from typing import Any

# Relative import on purpose: Hermes loads this plugin as a package whose
# search path is the plugin directory, so ``jobreach`` is a subpackage of it.
# A top-level ``import jobreach`` would only work if the plugin directory
# happened to be on sys.path, which is not guaranteed.
from .jobreach.runtime import run_engine

#: Generous per-call ceiling: a two-board headless scrape plus retries.
DEFAULT_TIMEOUT = 600.0


# --------------------------------------------------------------------------- #
# Result helpers
# --------------------------------------------------------------------------- #


def _ok(payload: dict[str, Any]) -> str:
    """Serialise a successful handler result."""
    return json.dumps({"success": True, **payload}, ensure_ascii=False, default=str)


def _fail(error: str, *, hint: str = "", **extra: Any) -> str:
    """Serialise a failure the model can recover from."""
    return json.dumps(
        {"success": False, "error": error, "hint": hint, **extra},
        ensure_ascii=False,
        default=str,
    )


def _timeout_hint(seconds: float) -> str:
    return (
        f"The engine did not finish within {int(seconds)}s. A headless scrape "
        "usually takes 30-90s per board. Narrow the search "
        "(sources=['wantedly']) before retrying."
    )


def _invoke(
    args: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    stdin: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Run the engine and decode its JSON.

    Returns:
        ``(payload, error)`` — exactly one of them is non-``None``.
    """
    try:
        proc = run_engine(args, timeout=timeout, stdin=stdin)
    except subprocess.TimeoutExpired:
        return None, {"error": f"jobreach {' '.join(args)} timed out", "hint": _timeout_hint(timeout)}
    except FileNotFoundError as exc:
        return None, {
            "error": f"could not start the engine: {exc}",
            "hint": "Run job_setup, or set JOBREACH_PYTHON to a Python 3.11+ interpreter.",
        }
    except OSError as exc:
        return None, {"error": f"could not start the engine: {exc}"}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0 and not stdout:
        return None, {
            "error": stderr[-1500:] or f"engine exited with code {proc.returncode}",
            "hint": (
                "If this mentions playwright or chromium, run job_setup. "
                "Otherwise report this error to the user verbatim."
            ),
        }

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, {
            "error": f"engine produced unparseable output: {exc}",
            "stdout_tail": stdout[-800:],
            "stderr_tail": stderr[-800:],
        }

    if proc.returncode != 0 and isinstance(payload, dict) and payload.get("errors"):
        # A partial failure still returns usable data — surface the warnings
        # alongside it rather than discarding the successful boards.
        payload.setdefault("engine_exit_code", proc.returncode)
    if stderr and isinstance(payload, dict):
        payload.setdefault("engine_warnings", stderr[-800:])
    return payload, None


def _drop_none(mapping: dict[str, Any]) -> dict[str, Any]:
    """Remove ``None`` values so they never become CLI flags."""
    return {key: value for key, value in mapping.items() if value is not None}


def _bool_flag(args: list[str], condition: bool, flag: str) -> None:
    if condition:
        args.append(flag)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


async def handle_job_search(params: dict[str, Any], **_kwargs: Any) -> str:
    """Scrape the selected boards and return the standard result envelope."""
    args: list[str] = ["search", "--json"]
    options = _drop_none(
        {
            "-k": params.get("keyword"),
            "-l": params.get("location"),
            "-n": params.get("limit"),
            "--validate": params.get("validation"),
            "--profile": params.get("profile"),
        }
    )
    for flag, value in options.items():
        args.extend([flag, str(value)])

    sources = params.get("sources")
    if sources:
        if isinstance(sources, str):
            args.extend(["--source", sources])
        else:
            args.extend(["--source", ",".join(str(item) for item in sources)])

    _bool_flag(args, bool(params.get("new_only")), "--new-only")
    if params.get("save") is False:
        args.append("--no-save")
    if params.get("headless") is False:
        args.append("--headful")

    payload, error = await asyncio.to_thread(_invoke, args, timeout=DEFAULT_TIMEOUT)
    return _fail(**error) if error else _ok({"result": payload})


async def handle_job_ingest(params: dict[str, Any], **_kwargs: Any) -> str:
    """Push agent-fetched listings (Indeed) through the standard pipeline."""
    jobs = params.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return _fail(
            "`jobs` must be a non-empty array of {title, url, ...} objects",
            hint=(
                "Fetch the cards with a real browser first, then pass "
                '[{"title": "...", "url": "...", "company": "..."}]'
            ),
        )

    args = ["ingest", "--json", "--source", str(params.get("source") or "indeed")]
    options = _drop_none({"-n": params.get("limit")})
    for flag, value in options.items():
        args.extend([flag, str(value)])
    _bool_flag(args, bool(params.get("new_only")), "--new-only")
    if params.get("save") is False:
        args.append("--no-save")

    payload, error = await asyncio.to_thread(
        _invoke,
        args,
        timeout=120.0,
        stdin=json.dumps(jobs, ensure_ascii=False),
    )
    return _fail(**error) if error else _ok({"result": payload})


async def handle_job_list(params: dict[str, Any], **_kwargs: Any) -> str:
    """Read stored listings; no network involved."""
    args = ["list", "--json"]
    options = _drop_none(
        {
            "-n": params.get("limit"),
            "--offset": params.get("offset"),
            "-k": params.get("text"),
            "-s": params.get("source"),
        }
    )
    for flag, value in options.items():
        args.extend([flag, str(value)])

    payload, error = await asyncio.to_thread(_invoke, args, timeout=60.0)
    if error:
        return _fail(**error)
    if params.get("new_since"):
        # Filtering in the handler keeps the CLI surface small; the store
        # already returns newest-first.
        threshold = str(params["new_since"])
        payload["jobs"] = [
            job for job in payload.get("jobs", []) if str(job.get("scraped_at") or "") >= threshold
        ]
        payload["shown"] = len(payload["jobs"])
    return _ok({"result": payload})


async def handle_job_note(params: dict[str, Any], **_kwargs: Any) -> str:
    """Render a result envelope into an Obsidian note."""
    result = params.get("result")
    if not isinstance(result, dict) or "jobs" not in result:
        return _fail(
            "`result` must be the object returned by job_search or job_ingest",
            hint="Call job_search first and pass its `result` field through unchanged.",
        )

    args = ["note", "--json"]
    if params.get("vault"):
        args.extend(["--vault", str(params["vault"])])
    if params.get("subfolder"):
        # The CLI reads the subfolder from the vault layout; keep parity by
        # rendering through the same code path rather than re-implementing it.
        args.extend(["--subfolder", str(params["subfolder"])])

    payload, error = await asyncio.to_thread(
        _invoke, args, timeout=60.0, stdin=json.dumps(result, ensure_ascii=False)
    )
    return _fail(**error) if error else _ok({"note": payload.get("note")})


async def handle_job_status(params: dict[str, Any], **_kwargs: Any) -> str:
    """Store statistics plus runtime diagnostics in one call."""
    runs = int(params.get("recent_runs") or 5)

    store, store_error = await asyncio.to_thread(
        _invoke, ["stats", "--json"], timeout=60.0
    )
    runtime, runtime_error = await asyncio.to_thread(
        _invoke, ["doctor", "--json"], timeout=60.0
    )
    if store_error and runtime_error:
        return _fail(store_error["error"], hint=runtime_error.get("hint", ""))
    if store:
        store["recent_runs"] = store.get("recent_runs", [])[:runs]
    return _ok({"store": store, "runtime": runtime})


async def handle_job_setup(params: dict[str, Any], **_kwargs: Any) -> str:
    """Prepare the scraping runtime (venv, Playwright, Chromium, skill)."""
    args = ["setup", "--json"]
    if params.get("with_browser") is False:
        args.append("--no-browser")
    if params.get("force"):
        args.append("--force")

    payload, error = await asyncio.to_thread(_invoke, args, timeout=900.0)

    skill_path = None
    if params.get("install_skill", True):
        skill_payload, skill_error = await asyncio.to_thread(
            _invoke, ["install-skill", "--json"], timeout=60.0
        )
        if skill_error:
            payload = payload or {}
            payload["skill_error"] = skill_error["error"]
        elif skill_payload:
            skill_path = skill_payload.get("skill")

    if error and not skill_path:
        return _fail(**error)
    return _ok({"setup": payload, "skill": skill_path})


async def handle_job_cron(params: dict[str, Any], **_kwargs: Any) -> str:
    """Schedule a recurring monitoring run through Hermes' cron system."""
    args = ["install-cron", "--json"]
    options = _drop_none(
        {
            "--schedule": params.get("schedule"),
            "--keyword": params.get("keyword"),
            "--location": params.get("location"),
            "--source": params.get("sources"),
            "--deliver": params.get("deliver"),
            "--name": params.get("name"),
        }
    )
    for flag, value in options.items():
        args.extend([flag, str(value)])

    payload, error = await asyncio.to_thread(_invoke, args, timeout=120.0)
    if error:
        return _fail(**error)
    if not payload.get("created"):
        return _fail(
            payload.get("message") or "the cron job was not created",
            hint="Run the printed command manually: " + str(payload.get("command", "")),
        )
    return _ok({"cron": payload})


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

#: Tool name → async handler. ``__init__.py`` pairs this with ``schemas.py``
#: so a tool can never be registered without a schema, or vice versa.
HANDLERS: dict[str, Callable[..., Any]] = {
    "job_search": handle_job_search,
    "job_ingest": handle_job_ingest,
    "job_list": handle_job_list,
    "job_note": handle_job_note,
    "job_status": handle_job_status,
    "job_setup": handle_job_setup,
    "job_cron": handle_job_cron,
}

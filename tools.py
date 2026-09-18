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

Beyond argv translation this module owns the two things that only exist on the
plugin side of that bridge:

* **settings** — the engine can never call ``ctx.get_config`` from a subprocess,
  so :func:`_settings` reads the four ``config_schema`` keys once per call and
  hands them to :func:`jobreach.runtime.run_engine`, which mirrors them into the
  child's environment;
* **the tool-call journal** — :func:`journal_append` / :func:`journal_read` keep
  the last few invocations in ``ctx.state``, which is what lets ``job_status``
  answer "what did Job Reach just do, and did it work?" without replaying the
  conversation.

All handlers are ``async`` (registered with ``is_async=True``) and hand the
blocking subprocess to a worker thread, so a long search never stalls the
agent's event loop. Each one is wrapped in :func:`_guard`, which is what makes
"returns JSON, never raises" structural rather than aspirational.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import subprocess
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

# Relative imports on purpose: Hermes loads this plugin as a package whose
# search path is the plugin directory, so ``jobreach`` is a subpackage of it.
# A top-level ``import jobreach`` would only work if the plugin directory
# happened to be on sys.path, which is not guaranteed.
from .jobreach.runtime import run_engine

logger = logging.getLogger(__name__)

#: Every key ``plugin.yaml``'s ``config_schema`` advertises, and the only keys
#: this module reads. The tuple lives here rather than in ``__init__.py``
#: because this module must stay importable on its own — tooling imports it as a
#: plain submodule, without the plugin package's ``__init__`` having run —
#: while ``__init__.py`` re-exports it for the manifest tests.
SETTINGS_KEYS = (
    "default_keyword",
    "default_sources",
    "note_subfolder",
    "max_results",
    "default_validation",
    "default_profile",
)

#: Generous per-call ceiling: a two-board headless scrape plus retries.
DEFAULT_TIMEOUT = 600.0

#: What to tell the model when the bridge itself failed (not the engine).
BRIDGE_FAILURE_HINT = (
    "Unexpected engine-bridge failure. Run job_status for diagnostics; "
    "report the error verbatim if it repeats."
)

#: The engine validates its own arguments too, but the plugin builds them, so a
#: rejection there is our bug rather than something the model can fix.
ARGUMENT_HINT = (
    "The engine rejected arguments the plugin builds for you. Report this "
    "verbatim if it repeats."
)

#: A missing browser is the one engine failure with a documented cure, so it is
#: the one failure that gets a specific hint.
SETUP_HINT = "Run job_setup to install the scraping runtime, then retry."

REPORT_HINT = "Report this error to the user verbatim."


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


class _BadArgument(Exception):
    """A tool argument the handler cannot use.

    ``hint`` is the model-facing fix, so raising this is how a handler reports
    "you called me wrong" without knowing anything about envelopes. The
    registration-time wrapper (:func:`_guard`) turns it into one.
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------------- #
# Argument coercion
# --------------------------------------------------------------------------- #


def _as_dict(params: Any) -> dict[str, Any]:
    """The tool arguments as a mapping.

    Hermes always sends a ``dict``, but the contract is "any input, always a
    JSON answer": ``None`` is a call with no arguments, and anything else is
    malformed and has to be reported rather than crashing on ``.get``.
    """
    if params is None:
        return {}
    if isinstance(params, Mapping):
        return dict(params)
    raise _BadArgument(
        f"tool arguments must be a JSON object, got {type(params).__name__}",
        hint='Pass the arguments as an object, e.g. {"keyword": "designer"}.',
    )


def _str_arg(params: Mapping[str, Any], key: str, *, label: str = "") -> str | None:
    """One optional text argument; ``None`` when it was omitted or blank.

    Blank counts as omitted because ``config.yaml`` and the model both express
    "not set" as an empty string. Scalars are stringified — a model sending
    ``{"location": 23}`` means the place "23" — but a list or object is a
    malformed call: folding it into a CLI flag would hand the engine something
    neither side can interpret. *label* names the argument in the error when the
    caller is reading something other than a top-level key (``_csv``).
    """
    display = label or f"`{key}`"
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        raise _BadArgument(
            f"{display} must be text, got {type(value).__name__}",
            hint=f"Pass {display} as a single string.",
        )
    text = str(value).strip()
    return text or None


def _int_arg(params: Mapping[str, Any], key: str) -> int | None:
    """One optional integer argument; ``None`` when it was omitted or blank.

    A model occasionally sends ``"10"`` for a numeric field, so a numeric string
    is accepted. ``"many"`` is not: passed through as ``-n many`` it comes back
    as an argparse usage dump from the engine, which tells the model nothing
    about the call it just made. Booleans are rejected explicitly because
    ``True`` is an ``int`` in Python and ``-n 1`` would be a silently wrong
    answer.
    """
    value = params.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise _BadArgument(
            f"`{key}` must be an integer, got {value!r}",
            hint=f"Pass `{key}` as a number, or omit it to use the default.",
        )
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise _BadArgument(
            f"`{key}` must be an integer, got {value!r}",
            hint=f"Pass `{key}` as a number, or omit it to use the default.",
        ) from None


def _csv(value: Any) -> str:
    """A comma-separated CLI value from a string or a list of boards.

    ``default_sources`` is declared as a list in ``plugin.yaml``, but a user may
    equally write ``wantedly,green`` in ``config.yaml``; both have to reach
    ``--source`` in the single form the CLI parses.

    Unlike :func:`_str_arg` this never objects to blank entries — the settings
    side drops them on purpose, so a list of nothing but blanks is a legitimate
    "not configured" and collapses to ``""``. The caller then omits the flag
    rather than passing ``--source ''``, which the engine rejects outright: a
    half-cleared config form would otherwise break every search.
    """
    if isinstance(value, (list, tuple)):
        parts = [
            _str_arg({"entry": item}, "entry")
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        return ",".join(parts)
    return _str_arg({"value": value}, "value") or ""


def _settings(ctx: Any) -> dict[str, Any]:
    """This plugin's settings, read through ``ctx.get_config`` once per call.

    Deliberately not cached: the user can edit ``config.yaml`` while Hermes is
    running, and a stale copy would silently override the fresh value.

    Keys that are unset, blank, or (for ``max_results``) not a positive integer
    are omitted, so "absent" and "configured but blank" both mean "use the
    plugin default" — the same rule :mod:`jobreach.settings` applies on the
    engine side. A context without ``get_config`` (older Hermes) degrades to
    ``{}``: settings are a convenience, never a reason to fail a tool call.
    """
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return {}

    settings: dict[str, Any] = {}
    for key in SETTINGS_KEYS:
        try:
            value = getter(key)
        except Exception:  # noqa: BLE001 — a broken config must not break the tool
            logger.warning("job-reach: could not read setting %r", key, exc_info=True)
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        settings[key] = value

    if "max_results" in settings:
        try:
            cap = int(str(settings["max_results"]).strip())
        except (TypeError, ValueError):
            cap = 0
        if cap > 0:
            settings["max_results"] = cap
        else:
            rejected = settings.pop("max_results")
            logger.warning(
                "job-reach: ignoring max_results=%r (want a positive integer)", rejected
            )
    return settings


# --------------------------------------------------------------------------- #
# Engine bridge
# --------------------------------------------------------------------------- #


def _run_engine(
    args: list[str],
    *,
    timeout: float,
    stdin: str | None,
    settings: Mapping[str, Any] | None,
) -> subprocess.CompletedProcess[str]:
    """Call :func:`jobreach.runtime.run_engine`, adding *settings* only when there are any.

    With nothing configured this is the exact pre-settings call, which keeps two
    things true: a user who never touched ``config.yaml`` keeps the behaviour
    they already have, and a runtime predating the settings bridge still works.
    """
    if settings:
        return run_engine(args, timeout=timeout, stdin=stdin, settings=settings)
    return run_engine(args, timeout=timeout, stdin=stdin)


def _argument_error_line(text: str) -> str:
    """The ``error:`` line of an argparse failure, or ``""`` for anything else.

    argparse writes a ``usage:`` block first and ends with
    ``<prog>: error: argument ...``. The usage block is matched first on purpose:
    a traceback can contain the substring "error:" in a frame, and stripping the
    frames must not depend on which of the two this is.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not any(line.startswith("usage:") for line in lines):
        return ""
    for line in reversed(lines):
        _, marker, message = line.partition("error: ")
        if marker and message.strip():
            return message.strip()
    return ""


def _last_traceback_line(text: str) -> str:
    """The exception line of a Python traceback — the only part worth showing.

    Scanning from the end skips the frame list, the ``raise`` echo and the
    ``During handling…`` markers, leaving the line that names the exception.
    """
    skip_prefixes = (
        "Traceback (most recent call last)",
        'File "',
        "During handling",
        "The above exception",
        "raise ",
        "^",
    )
    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        if line.startswith(skip_prefixes):
            continue
        return line
    return ""


def _stderr_error(stderr: str, returncode: int) -> dict[str, Any]:
    """Turn a failed engine's stderr into an error the model can act on.

    Three shapes reach here and they need different treatment: an argparse
    failure (verbose usage block, one useful line), a Python traceback (our file
    paths, one useful line), and an ordinary error message (already fine).
    """
    text = stderr.strip()
    if not text:
        return {"error": f"engine exited with code {returncode}", "hint": REPORT_HINT}

    argument_error = _argument_error_line(text)
    if argument_error:
        return {"error": f"engine rejected an argument: {argument_error}", "hint": ARGUMENT_HINT}

    if "Traceback (most recent call last)" in text:
        message = _last_traceback_line(text) or "unknown error"
        return {"error": f"the engine crashed: {message}", "hint": REPORT_HINT}

    lowered = text.lower()
    hint = SETUP_HINT if "playwright" in lowered or "chromium" in lowered else REPORT_HINT
    return {"error": text[-1500:], "hint": hint}


def _invoke(
    args: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    stdin: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Run the engine and decode its JSON.

    Returns:
        ``(payload, error)`` — exactly one of them is non-``None``.

    Never raises: the engine is another process, so every way it can fail
    (missing interpreter, timeout, junk on stdout, a crash, a bug in this
    function) has to become an error the model can read. The specific branches
    keep their hints; the last one exists so an unforeseen failure is still an
    envelope rather than a traceback in the conversation.
    """
    try:
        proc = _run_engine(args, timeout=timeout, stdin=stdin, settings=settings)
    except subprocess.TimeoutExpired:
        return None, {"error": f"jobreach {' '.join(args)} timed out", "hint": _timeout_hint(timeout)}
    except FileNotFoundError as exc:
        return None, {
            "error": f"could not start the engine: {exc}",
            "hint": "Run job_setup, or set JOBREACH_PYTHON to a Python 3.11+ interpreter.",
        }
    except OSError as exc:
        return None, {"error": f"could not start the engine: {exc}"}
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.warning("job-reach: engine bridge failed", exc_info=True)
        return None, {"error": f"{type(exc).__name__}: {exc}", "hint": BRIDGE_FAILURE_HINT}
    except BaseException as exc:  # noqa: BLE001 — KeyboardInterrupt lands here
        logger.error("job-reach: engine bridge aborted", exc_info=True)
        return None, {
            "error": f"{type(exc).__name__}: the engine call was interrupted",
            "hint": "Nothing was saved. Re-run the call if you still need the result.",
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0 and not stdout:
        return None, _stderr_error(stderr, proc.returncode)

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, {
            "error": f"engine produced unparseable output: {exc}",
            "stdout_tail": stdout[-800:],
            "stderr_tail": stderr[-800:],
        }

    if not isinstance(payload, dict):
        # A JSON list/string/number is a protocol violation, not a result: every
        # caller indexes into an envelope, so returning it as one hides the bug.
        return None, {
            "error": f"engine returned a JSON {type(payload).__name__}, not an object",
            "hint": BRIDGE_FAILURE_HINT,
        }

    if proc.returncode != 0 and payload.get("errors"):
        # A partial failure still returns usable data — surface the warnings
        # alongside it rather than discarding the successful boards.
        payload.setdefault("engine_exit_code", proc.returncode)
    if stderr:
        payload.setdefault("engine_warnings", stderr[-800:])
    return payload, None


def _drop_none(mapping: dict[str, Any]) -> dict[str, Any]:
    """Remove ``None`` values so they never become CLI flags."""
    return {key: value for key, value in mapping.items() if value is not None}


def _bool_flag(args: list[str], condition: bool, flag: str) -> None:
    if condition:
        args.append(flag)


# --------------------------------------------------------------------------- #
# The tool-call journal
# --------------------------------------------------------------------------- #

#: ``ctx.state`` key holding the journal ``job_status`` reports.
JOURNAL_KEY = "recent_tool_calls"

#: How many entries the journal keeps. Small on purpose: it answers "what just
#: happened", it is not an audit log — the engine's SQLite store is the durable
#: record of a search.
JOURNAL_LIMIT = 20

#: Guards the read-modify-write of the journal. Hermes runs ``post_tool_call``
#: on a bounded worker thread and fires it concurrently for parallel tool calls;
#: the state facade's own lock covers one atomic read or write, not the pair.
_JOURNAL_LOCK = threading.Lock()


def _utc_now() -> str:
    """Second-resolution UTC timestamp — sortable, and unambiguous in a log."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _short(value: Any, limit: int = 160) -> str:
    """One line of detail: state is JSON, but a model reads it as prose."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _success_detail(tool_name: str, payload: Mapping[str, Any]) -> str:
    """A one-line "what came back" for a successful call, per tool."""
    if tool_name == "job_note":
        return _str_arg(payload, "note") or "note written"
    if tool_name == "job_status":
        store = payload.get("store")
        total = store.get("total") if isinstance(store, Mapping) else None
        return f"{total} listings stored" if isinstance(total, int) else "diagnostics collected"
    if tool_name == "job_setup":
        return _str_arg(payload, "skill") or "runtime ready"
    if tool_name == "job_cron":
        return "cron created"
    # job_search / job_ingest / job_list all wrap the engine envelope in "result".
    result = payload.get("result")
    if isinstance(result, Mapping):
        summary = result.get("summary")
        if isinstance(summary, Mapping) and isinstance(summary.get("total"), int):
            new = summary.get("new")
            suffix = f" ({new} new)" if isinstance(new, int) and new else ""
            return f"{summary['total']} listings{suffix}"
        jobs = result.get("jobs")
        if isinstance(jobs, list):
            return f"{len(jobs)} listings"
    return ""


def _summarise_result(tool_name: str, result: Any) -> tuple[bool, str]:
    """``(ok, detail)`` from a handler's JSON result string.

    The hook payload carries the *string* the handler returned, so the journal
    has to read it back. Anything that is not our success/error envelope —
    Hermes' own error object, a multimodal envelope, junk — is recorded as a
    failure with its text: the journal's job is to show what happened, not to
    hide it.
    """
    payload: Any = result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return False, _short(payload) or "no result"
    if not isinstance(payload, Mapping):
        return False, _short(payload) or "no result"
    if payload.get("success") is False or payload.get("error"):
        return False, _short(payload.get("error") or "failed")
    return True, _short(_success_detail(tool_name, payload))


def journal_entry(tool_name: str, result: Any) -> dict[str, Any]:
    """One compact journal record: ``{"tool", "at", "ok", "detail"}``.

    JSON-serialisable by construction — strings and a bool — because
    ``ctx.state`` is a JSON document, and a value that cannot be encoded there
    would be lost silently at the worst moment.
    """
    ok, detail = _summarise_result(tool_name, result)
    return {"tool": str(tool_name), "at": _utc_now(), "ok": bool(ok), "detail": detail}


def _state_get(state: Any, key: str, default: Any) -> Any:
    """Read one key from ``ctx.state``.

    Hermes' real facade (``hermes_cli/plugins_state.py::PluginState``) exposes
    ``get``/``set`` only — it is **not** subscriptable, so the ``state[key]``
    shape would raise ``TypeError`` against a real Hermes. Mapping-style state is
    accepted too, because that is what a dict-backed test double looks like.
    """
    getter = getattr(state, "get", None)
    if callable(getter):
        return getter(key, default)
    try:
        return state[key]
    except Exception:  # noqa: BLE001 — a foreign state object is not our problem
        return default


def _state_set(state: Any, key: str, value: Any) -> None:
    """Write one key to ``ctx.state``, preferring the documented ``set()``."""
    setter = getattr(state, "set", None)
    if callable(setter):
        setter(key, value)
        return
    state[key] = value


def journal_append(ctx: Any, entry: Mapping[str, Any]) -> None:
    """Prepend *entry* to the journal in ``ctx.state``, newest first, capped.

    A "last 20, tolerate a lost entry" policy would be simpler, but the lock is
    cheap and the journal is small: a dropped entry would make ``job_status``
    lie about a call the agent just made. Every failure is swallowed — this runs
    from a hook Hermes may execute on a worker thread, and a diagnostics journal
    is never worth an exception in the tool loop. A context without state
    (older Hermes) simply has nothing to record.
    """
    try:
        state = getattr(ctx, "state", None)
        if state is None:
            return
        with _JOURNAL_LOCK:
            stored = _state_get(state, JOURNAL_KEY, [])
            recent = (
                [item for item in stored if isinstance(item, Mapping)]
                if isinstance(stored, list)
                else []
            )
            recent.insert(0, dict(entry))
            _state_set(state, JOURNAL_KEY, recent[:JOURNAL_LIMIT])
    except Exception:  # noqa: BLE001 — see the docstring
        logger.debug("job-reach: could not write the tool-call journal", exc_info=True)


def journal_read(ctx: Any) -> list[dict[str, Any]]:
    """Newest-first copies of the journal; ``[]`` when there is none.

    ``job_status`` calls this with whatever context it was given, including
    ``None`` when a caller omits one, so every failure (no state at all,
    malformed JSON on disk, a foreign shape) degrades to "no recent tool calls"
    rather than failing the call that was supposed to report the problem.
    """
    try:
        state = getattr(ctx, "state", None)
        if state is None:
            return []
        stored = _state_get(state, JOURNAL_KEY, [])
    except Exception:  # noqa: BLE001 — diagnostics must not fail the tool call
        logger.debug("job-reach: could not read the tool-call journal", exc_info=True)
        return []
    if not isinstance(stored, list):
        return []
    return [dict(item) for item in stored if isinstance(item, Mapping)][:JOURNAL_LIMIT]


# --------------------------------------------------------------------------- #
# The never-raise wrapper
# --------------------------------------------------------------------------- #


def _guard(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Make "handlers return JSON and never raise" structural (AGENTS.md #4).

    Hermes' registry also wraps a handler in ``try/except``, but by then the
    exception has cost the user a readable answer: the guide's rule exists
    because a raw exception reaches the model as an opaque failure. A
    :class:`_BadArgument` carries the model-facing hint; anything else is a bug
    and says so, with the traceback going to the log where a human can see it.

    ``BaseException`` is caught too, not just ``Exception``. A Ctrl-C landing
    inside the 60–600 s engine call surfaces as ``KeyboardInterrupt`` — a
    ``BaseException`` — and ``SystemExit``/``GeneratorExit`` can arrive from a
    host that is tearing the session down. Letting those through would put the
    agent back in exactly the state this wrapper exists to prevent: a tool call
    that answers with an opaque failure instead of a sentence. The interrupt is
    still recorded as a failure and logged loudly, so nothing is swallowed
    silently — but the *run* is already over by then (the engine child is killed
    by :mod:`jobreach.proc`), so what the model needs is the explanation.
    """

    @functools.wraps(handler)
    async def guarded(params: Any, **kwargs: Any) -> str:
        try:
            return await handler(params, **kwargs)
        except _BadArgument as exc:
            return _fail(str(exc), hint=exc.hint)
        except Exception as exc:  # noqa: BLE001 — the entire point of this wrapper
            logger.warning("job-reach: %s failed", handler.__name__, exc_info=True)
            return _fail(f"{type(exc).__name__}: {exc}", hint=BRIDGE_FAILURE_HINT)
        except BaseException as exc:  # noqa: BLE001 — KeyboardInterrupt lives here
            logger.error("job-reach: %s was aborted", handler.__name__, exc_info=True)
            return _fail(
                f"{type(exc).__name__}: the call was interrupted",
                hint="The call was stopped before it finished. Nothing was saved; "
                     "re-run it if you still need the result.",
            )

    return guarded


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


@_guard
async def handle_job_search(params: dict[str, Any], *, ctx: Any = None, **kwargs: Any) -> str:
    """Scrape the selected boards and return the standard result envelope.

    ``default_keyword``, ``default_sources`` and ``max_results`` fill in only
    what the caller left out: an argument the model passed always wins.
    """
    params = _as_dict(params)
    settings = _settings(ctx)

    keyword = _str_arg(params, "keyword") or _str_arg(settings, "default_keyword")
    sources = params.get("sources") or settings.get("default_sources")
    limit = _int_arg(params, "limit")
    if limit is None:
        limit = settings.get("max_results")  # already validated by _settings

    args: list[str] = ["search", "--json"]
    options = _drop_none(
        {
            "-k": keyword,
            "-l": _str_arg(params, "location"),
            "-n": limit,
            "--validate": _str_arg(params, "validation"),
            "--profile": _str_arg(params, "profile"),
        }
    )
    for flag, value in options.items():
        args.extend([flag, str(value)])

    # Check the *rendered* value, not the raw one: `[""]` is truthy but renders
    # to "", and `--source ""` makes the engine reject the whole search.
    source_flag = _csv(sources) if sources else ""
    if source_flag:
        args.extend(["--source", source_flag])

    _bool_flag(args, bool(params.get("new_only")), "--new-only")
    _bool_flag(args, bool(params.get("detail")), "--detail")
    # Only an explicit `false` travels: saying nothing leaves the engine's own
    # default (collapse) in charge, so this flag cannot drift from the CLI's.
    _bool_flag(args, params.get("dedupe") is False, "--no-dedupe")
    if params.get("save") is False:
        args.append("--no-save")
    if params.get("headless") is False:
        args.append("--headful")

    payload, error = await asyncio.to_thread(
        _invoke, args, timeout=DEFAULT_TIMEOUT, settings=settings
    )
    return _fail(**error) if error else _ok({"result": payload})


@_guard
async def handle_job_ingest(params: dict[str, Any], *, ctx: Any = None, **kwargs: Any) -> str:
    """Push agent-fetched listings (Indeed) through the standard pipeline."""
    params = _as_dict(params)
    settings = _settings(ctx)

    jobs = params.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return _fail(
            "`jobs` must be a non-empty array of {title, url, ...} objects",
            hint=(
                "Fetch the cards with a real browser first, then pass "
                '[{"title": "...", "url": "...", "company": "..."}]'
            ),
        )

    source = _str_arg(params, "source") or "indeed"
    args = ["ingest", "--json", "--source", source]
    options = _drop_none({"-n": _int_arg(params, "limit")})
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
        settings=settings,
    )
    return _fail(**error) if error else _ok({"result": payload})


@_guard
async def handle_job_list(params: dict[str, Any], *, ctx: Any = None, **kwargs: Any) -> str:
    """Read stored listings; no network involved.

    ``max_results`` is the page size when the caller passed no ``limit``;
    without it the CLI's own default (25) applies.
    """
    params = _as_dict(params)
    settings = _settings(ctx)

    limit = _int_arg(params, "limit")
    if limit is None:
        limit = settings.get("max_results")

    args = ["list", "--json"]
    options = _drop_none(
        {
            "-n": limit,
            "--offset": _int_arg(params, "offset"),
            "-k": _str_arg(params, "text"),
            "-s": _str_arg(params, "source"),
        }
    )
    for flag, value in options.items():
        args.extend([flag, str(value)])

    _bool_flag(args, bool(params.get("detail")), "--detail")
    _bool_flag(args, params.get("dedupe") is False, "--no-dedupe")

    payload, error = await asyncio.to_thread(
        _invoke, args, timeout=60.0, settings=settings
    )
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
        # The collapse happened inside the engine and cannot see this filter,
        # so `unique` has to come down with `shown` or the envelope counts rows
        # it is no longer returning.
        payload["unique"] = payload["shown"]
    return _ok({"result": payload})


@_guard
async def handle_job_note(params: dict[str, Any], *, ctx: Any = None, **kwargs: Any) -> str:
    """Render a result envelope into an Obsidian note.

    ``note_subfolder`` is only a default: an explicit ``subfolder`` argument
    always wins.
    """
    params = _as_dict(params)
    settings = _settings(ctx)

    result = params.get("result")
    if not isinstance(result, dict) or "jobs" not in result:
        return _fail(
            "`result` must be the object returned by job_search or job_ingest",
            hint="Call job_search first and pass its `result` field through unchanged.",
        )

    args = ["note", "--json"]
    vault = _str_arg(params, "vault")
    if vault:
        args.extend(["--vault", vault])
    subfolder = _str_arg(params, "subfolder") or _str_arg(settings, "note_subfolder")
    if subfolder:
        # The CLI reads the subfolder from the vault layout; keep parity by
        # rendering through the same code path rather than re-implementing it.
        args.extend(["--subfolder", subfolder])

    payload, error = await asyncio.to_thread(
        _invoke,
        args,
        timeout=60.0,
        stdin=json.dumps(result, ensure_ascii=False),
        settings=settings,
    )
    return _fail(**error) if error else _ok({"note": payload.get("note")})


@_guard
async def handle_job_status(params: dict[str, Any], *, ctx: Any = None, **kwargs: Any) -> str:
    """Store statistics, runtime diagnostics, and the recent tool-call journal.

    The journal comes from ``ctx.state``, so this is also the one call that can
    answer "is the plugin itself working?" when a search just failed.
    """
    params = _as_dict(params)
    settings = _settings(ctx)

    runs = _int_arg(params, "recent_runs")
    if runs is None:
        runs = 5

    store, store_error = await asyncio.to_thread(
        _invoke, ["stats", "--json"], timeout=60.0, settings=settings
    )
    runtime, runtime_error = await asyncio.to_thread(
        _invoke, ["doctor", "--json"], timeout=60.0, settings=settings
    )
    if store_error and runtime_error:
        return _fail(store_error["error"], hint=runtime_error.get("hint", ""))
    if store:
        store["recent_runs"] = store.get("recent_runs", [])[:runs]
    return _ok(
        {
            "store": store,
            "runtime": runtime,
            "recent_tool_calls": journal_read(ctx),
        }
    )


@_guard
async def handle_job_setup(params: dict[str, Any], *, ctx: Any = None, **kwargs: Any) -> str:
    """Prepare the scraping runtime (venv, Playwright, Chromium, skill)."""
    params = _as_dict(params)
    settings = _settings(ctx)

    args = ["setup", "--json"]
    if params.get("with_browser") is False:
        args.append("--no-browser")
    if params.get("force"):
        args.append("--force")

    payload, error = await asyncio.to_thread(
        _invoke, args, timeout=900.0, settings=settings
    )

    skill_path = None
    if params.get("install_skill", True):
        skill_payload, skill_error = await asyncio.to_thread(
            _invoke, ["install-skill", "--json"], timeout=60.0, settings=settings
        )
        if skill_error:
            payload = payload or {}
            payload["skill_error"] = skill_error["error"]
        elif skill_payload:
            skill_path = skill_payload.get("skill")

    if error and not skill_path:
        return _fail(**error)
    return _ok({"setup": payload, "skill": skill_path})


@_guard
async def handle_job_cron(params: dict[str, Any], *, ctx: Any = None, **kwargs: Any) -> str:
    """Schedule a recurring monitoring run through Hermes' cron system."""
    params = _as_dict(params)
    settings = _settings(ctx)

    args = ["install-cron", "--json"]
    options = _drop_none(
        {
            "--schedule": _str_arg(params, "schedule"),
            "--keyword": _str_arg(params, "keyword"),
            "--location": _str_arg(params, "location"),
            "--source": _str_arg(params, "sources"),
            "--deliver": _str_arg(params, "deliver"),
            "--name": _str_arg(params, "name"),
        }
    )
    for flag, value in options.items():
        args.extend([flag, str(value)])

    payload, error = await asyncio.to_thread(
        _invoke, args, timeout=120.0, settings=settings
    )
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

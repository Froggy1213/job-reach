"""Tool-handler contract: envelopes, argument coercion, settings, and the journal.

Why this module exists
----------------------
``tests/test_plugin_contract.py`` drives the handlers through the *real*
subprocess bridge, which is the right end-to-end check but a poor place to pin
failure behaviour: every error path there depends on what the engine happens to
print. This module replaces ``tools.run_engine`` with a recording stub, so the
argv a handler builds, the settings it forwards, and the envelope it returns for
a hostile argument can all be asserted directly — without spawning a browser,
opening a socket, or importing a single line of Hermes.

Three promises are pinned here, all of them from the plugin guide:

1. **a handler always returns a JSON object and never raises**, for any input,
   including the arguments a model gets wrong;
2. **an engine failure arrives as an error the model can act on** — a typed
   argument error rather than an argparse usage dump, an exception line rather
   than a Python traceback full of our file paths, and a hint that matches the
   failure instead of one that talks about Playwright every time;
3. **the settings bridge is populated here and nowhere else** — the engine runs
   in a subprocess and can never call ``ctx.get_config``, so the "explicit
   argument beats configured default" rules have to hold in this layer.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.machinery
import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Distinct from the other suites' package names: each test module imports the
#: plugin as its own package, exactly as Hermes does for an installed plugin.
PACKAGE = "jobreach_plugin_ws_a_tools"


# --------------------------------------------------------------------------- #
# Loading the plugin without Hermes
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def plugin():
    """Import the plugin the way Hermes does: package rooted at the plugin dir."""
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        str(PROJECT_ROOT / "__init__.py"),
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tools(plugin):
    """The handler module, imported the way ``register()`` imports it."""
    return importlib.import_module(f"{PACKAGE}.tools")


def test_tools_imports_before_the_package_init_has_run(plugin):
    """Hermes execs every sibling ``*.py`` *before* the plugin's ``__init__.py``.

    ``plugins/plugin_loader.py::load_plugin_module`` reserves the package name,
    execs each sibling as ``<plugin>.<stem>``, and only afterwards execs
    ``__init__.py``. So ``tools.py`` must not reach for a name the package
    defines: a ``from . import SETTINGS_KEYS`` would work in every other test in
    this module — they execute ``__init__`` first — and fail in production.
    """
    name = f"{PACKAGE}_loader_order"
    shell = importlib.machinery.ModuleSpec(name, None, is_package=True)
    shell.submodule_search_locations = [str(PROJECT_ROOT)]
    sys.modules[name] = importlib.util.module_from_spec(shell)
    try:
        module = importlib.import_module(f"{name}.tools")
        assert module.SETTINGS_KEYS == plugin.SETTINGS_KEYS
        assert callable(module.handle_job_list)
    finally:
        for key in [key for key in sys.modules if key == name or key.startswith(f"{name}.")]:
            del sys.modules[key]


# --------------------------------------------------------------------------- #
# The engine stub and the fake contexts
# --------------------------------------------------------------------------- #


class EngineStub:
    """Stand in for ``jobreach.runtime.run_engine``; records argv, spawns nothing.

    A handler under test may never start the engine — the point of this layer is
    that it only builds argv — so the recorded call is the assertion target.
    """

    #: A superset of what the engine's ``--json`` envelopes contain, so one stub
    #: serves every handler's happy path. The shape matters: the handlers wrap
    #: the engine payload in their own ``{"success": true, "result": …}``
    #: envelope, so the engine's own keys (``summary``, ``jobs``) must stay at
    #: the top level here.
    DEFAULT: dict[str, Any] = {
        "query": {"keyword": "designer", "sources": ["wantedly"]},
        "summary": {"total": 0, "new": 0, "by_platform": {}},
        "jobs": [],
        "recent_runs": [],
        "note": "/tmp/vault/job-searches/note.md",
        "created": True,
        "cron": {"name": "job-reach-monitor"},
        "skill": "/tmp/skills/SKILL.md",
    }

    def __init__(
        self,
        payload: Any = None,
        *,
        returncode: int = 0,
        stderr: str = "",
        raises: BaseException | None = None,
    ) -> None:
        self.payload = self.DEFAULT if payload is None else payload
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        args: Any,
        *,
        timeout: float | None = None,
        stdin: str | None = None,
        settings: Any = None,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(
            {"args": list(args), "timeout": timeout, "stdin": stdin, "settings": settings}
        )
        if self.raises is not None:
            raise self.raises
        stdout = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return subprocess.CompletedProcess(list(args), self.returncode, stdout, self.stderr)

    @property
    def last(self) -> dict[str, Any]:
        """The most recent recorded call; fails loudly when there was none."""
        assert self.calls, "the handler never reached the engine bridge"
        return self.calls[-1]


@pytest.fixture()
def engine(tools, monkeypatch: pytest.MonkeyPatch) -> EngineStub:
    """Replace the bridge for one test; nothing here starts a subprocess."""
    stub = EngineStub()
    monkeypatch.setattr(tools, "run_engine", stub)
    return stub


class FakeContext:
    """The ``ctx`` surface the handlers use: ``get_config`` and ``state``."""

    def __init__(self, settings: dict[str, Any] | None = None, state: Any = None) -> None:
        self.settings = dict(settings or {})
        self.config_reads: list[str] = []
        self.state = {} if state is None else state

    def get_config(self, key: str, default: Any = None) -> Any:
        self.config_reads.append(key)
        return self.settings.get(key, default)


class RecordingContext(FakeContext):
    """FakeContext plus the registration surface ``register()`` calls."""

    def __init__(self, settings: dict[str, Any] | None = None, state: Any = None) -> None:
        super().__init__(settings, state)
        self.tools: list[dict[str, Any]] = []
        self.hooks: dict[str, Any] = {}
        self.skill: tuple[str, Path] | None = None
        self.command: str | None = None
        self.cli_command: str | None = None

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_skill(self, name: str, path: Path, **kwargs: Any) -> None:
        self.skill = (name, Path(path))

    def register_command(self, name: str, handler: Any, **kwargs: Any) -> None:
        self.command = name

    def register_cli_command(self, name: str, **kwargs: Any) -> None:
        self.cli_command = name

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback


@pytest.fixture()
def registered(plugin) -> RecordingContext:
    """A fresh registration per test, so the journal never leaks between tests."""
    ctx = RecordingContext()
    plugin.register(ctx)
    return ctx


class ExplodingState:
    """A state facade whose every read and write fails (corrupt file, no access)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, key: str, default: Any = None) -> Any:
        self.calls.append(f"get:{key}")
        raise RuntimeError("plugin state is unreadable")

    def set(self, key: str, value: Any) -> None:
        self.calls.append(f"set:{key}")
        raise RuntimeError("plugin state is unreadable")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def call(handler: Any, params: Any, *, ctx: Any = None) -> dict[str, Any]:
    """Invoke a handler and decode its envelope, asserting the JSON-string rule."""
    raw = asyncio.run(handler(params, ctx=ctx))
    assert isinstance(raw, str), f"a handler returned {type(raw).__name__}, not a JSON string"
    payload = json.loads(raw)
    assert isinstance(payload, dict), f"a handler returned {type(payload).__name__}, not an object"
    return payload


def _flag(args: list[str], name: str) -> str | None:
    """The value following *name* in an argv, or ``None`` when the flag is absent."""
    return args[args.index(name) + 1] if name in args else None


def registered_handler(ctx: RecordingContext, name: str) -> Any:
    """The handler ``register(ctx)`` actually wired up for *name*."""
    return next(tool["handler"] for tool in ctx.tools if tool["name"] == name)


# --------------------------------------------------------------------------- #
# "Always a JSON envelope, never an exception"
# --------------------------------------------------------------------------- #

#: Arguments a model plausibly gets wrong. Every handler has to answer all of them.
HOSTILE_PARAMS: list[Any] = [
    None,
    {},
    "nonsense",
    12,
    {"limit": "many"},
    {"jobs": "nope"},
    {"recent_runs": []},
    {"result": 3},
]


def test_every_handler_answers_hostile_input_with_json(tools, engine):
    for name, handler in tools.HANDLERS.items():
        for params in HOSTILE_PARAMS:
            payload = call(handler, params)
            assert "success" in payload, f"{name} did not report success for {params!r}"


def test_a_bridge_crash_is_an_envelope_not_an_exception(tools, engine):
    """Item 1: even a bug in the bridge becomes JSON the model can read."""
    engine.raises = RuntimeError("the bridge fell over")
    payload = call(tools.handle_job_list, {})
    assert payload["success"] is False
    assert payload["error"] == "RuntimeError: the bridge fell over"
    assert "job_status" in payload["hint"]


def test_an_interrupt_during_a_scrape_is_also_an_envelope(tools, engine):
    """A Ctrl-C during the 60-600 s engine call must not escape as an exception.

    ``KeyboardInterrupt`` and friends are ``BaseException``, not ``Exception``,
    so an ``except Exception`` branch lets them straight through — and an
    interrupted scrape is the *most* likely way this plugin sees one. The
    ``SystemExit``/``GeneratorExit`` cases come from a host tearing the session
    down, which is equally not the model's problem to diagnose.

    Two layers have to hold, and they are tested separately because either one
    alone leaves a hole: :func:`_invoke` covers an interrupt raised by the
    subprocess call, and :func:`_guard` covers one raised by a handler *before*
    it reaches the bridge (reading `ctx`, coercing an argument, …).
    """
    for exc in (KeyboardInterrupt(), SystemExit(3), GeneratorExit(), BaseException("odd")):
        engine.raises = exc
        payload = call(tools.handle_job_list, {})
        assert payload["success"] is False, f"{type(exc).__name__} produced a success envelope"
        assert "interrupted" in payload["error"], payload["error"]
        assert payload["hint"], "an interrupted call still owes the model a next step"


def test_the_guard_itself_converts_an_interrupt_that_never_reaches_the_bridge(
    tools, engine, monkeypatch: pytest.MonkeyPatch
):
    """The backstop, exercised directly — the bridge must not be in the way.

    ``_invoke`` swallows an interrupt that happens *inside* the subprocess call,
    so a test that only monkeypatches the engine can never reach the guard's own
    handler. Breaking `ctx.get_config` first does reach it, and pins the branch
    that would otherwise be dead code the moment someone simplified the wrapper.
    """
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(tools, "_settings", boom)
    payload = call(tools.handle_job_search, {})
    assert payload["success"] is False
    assert "KeyboardInterrupt" in payload["error"]
    assert payload["hint"]


def test_a_foreign_json_payload_is_an_error_envelope(tools, engine):
    """Item 1: a JSON list is a protocol violation, not a result envelope."""
    engine.payload = [{"title": "a list where an object belongs"}]
    payload = call(tools.handle_job_list, {})
    assert payload["success"] is False
    assert "list" in payload["error"]


def test_a_json_scalar_payload_is_an_error_envelope(tools, engine):
    engine.payload = "just a string"
    payload = call(tools.handle_job_list, {})
    assert payload["success"] is False


# --------------------------------------------------------------------------- #
# Item 10: a typed argument error instead of an argparse usage dump
# --------------------------------------------------------------------------- #


def test_a_non_integer_limit_never_reaches_the_engine(tools, engine):
    payload = call(tools.handle_job_list, {"limit": "many"})
    assert payload["success"] is False
    assert "`limit` must be an integer" in payload["error"]
    assert "usage:" not in payload["error"]
    assert engine.calls == [], "the handler must reject the argument before building argv"


def test_a_non_integer_limit_is_rejected_by_job_search_too(tools, engine):
    payload = call(tools.handle_job_search, {"limit": "many"})
    assert payload["success"] is False
    assert "`limit` must be an integer" in payload["error"]
    assert engine.calls == []


def test_a_non_integer_recent_runs_never_reaches_the_engine(tools, engine):
    """Item 10 defect 3: this used to raise straight out of the handler."""
    payload = call(tools.handle_job_status, {"recent_runs": "five"})
    assert payload["success"] is False
    assert "`recent_runs` must be an integer" in payload["error"]
    assert engine.calls == []


def test_a_non_integer_offset_never_reaches_the_engine(tools, engine):
    payload = call(tools.handle_job_list, {"offset": "later"})
    assert payload["success"] is False
    assert "`offset` must be an integer" in payload["error"]
    assert engine.calls == []


def test_a_boolean_limit_is_not_silently_one(tools, engine):
    payload = call(tools.handle_job_list, {"limit": True})
    assert payload["success"] is False, "True is an int in Python; -n 1 would be a wrong answer"


def test_a_numeric_string_is_still_a_number(tools, engine):
    """The model sends "10" as often as 10 — that is a value, not an error."""
    payload = call(tools.handle_job_list, {"limit": "10"})
    assert payload["success"] is True
    assert _flag(engine.last["args"], "-n") == "10"


def test_a_container_where_text_belongs_is_a_typed_error(tools, engine):
    payload = call(tools.handle_job_list, {"text": ["designer"]})
    assert payload["success"] is False
    assert "`text` must be text" in payload["error"]
    assert engine.calls == []


# --------------------------------------------------------------------------- #
# Item 10: the engine's stderr is triaged, not dumped
# --------------------------------------------------------------------------- #

ARGPARSE_STDERR = (
    "usage: jobreach list [-h] [-n LIMIT] [--offset OFFSET] [--json]\n"
    "jobreach list: error: argument -n/--limit: invalid int value: 'many'\n"
)

TRACEBACK_STDERR = '''Traceback (most recent call last):
  File "/opt/jobreach/jobreach/__main__.py", line 8, in <module>
    raise SystemExit(main())
  File "/opt/jobreach/jobreach/cli.py", line 203, in main
    return handler(args)
ValueError: schedule must look like a cron expression
'''


def test_an_argparse_failure_keeps_only_the_error_line(tools, engine):
    engine.payload = ""
    engine.returncode = 2
    engine.stderr = ARGPARSE_STDERR
    payload = call(tools.handle_job_list, {})
    assert payload["success"] is False
    assert "usage:" not in payload["error"], "the model does not need the option block"
    assert "invalid int value" in payload["error"]


def test_a_traceback_never_leaks_frames(tools, engine):
    engine.payload = ""
    engine.returncode = 1
    engine.stderr = TRACEBACK_STDERR
    payload = call(tools.handle_job_cron, {"schedule": "twice a blue moon"})
    assert payload["success"] is False
    assert 'File "' not in payload["error"], "internal file paths are noise to the model"
    assert "Traceback" not in payload["error"]
    assert payload["error"] == (
        "the engine crashed: ValueError: schedule must look like a cron expression"
    )


def test_the_hint_matches_the_failure(tools, engine):
    """The old hint talked about Playwright whatever had actually failed."""
    engine.payload = ""
    engine.returncode = 1
    engine.stderr = "MissingDependencyError: playwright is not installed"
    payload = call(tools.handle_job_search, {})
    assert "job_setup" in payload["hint"]

    engine.stderr = "some unrelated engine failure"
    payload = call(tools.handle_job_search, {})
    assert "job_setup" not in payload["hint"]
    assert "verbatim" in payload["hint"]


def test_an_ordinary_engine_error_is_passed_through(tools, engine):
    engine.payload = ""
    engine.returncode = 1
    engine.stderr = "ConfigError: OBSIDIAN_VAULT_PATH does not point at a vault"
    payload = call(tools.handle_job_note, {"result": {"jobs": []}})
    assert payload["success"] is False
    assert "OBSIDIAN_VAULT_PATH" in payload["error"]


# --------------------------------------------------------------------------- #
# The settings bridge
# --------------------------------------------------------------------------- #


def test_default_keyword_only_fills_in_an_omitted_keyword(tools, engine):
    ctx = FakeContext({"default_keyword": "designer"})
    assert call(tools.handle_job_search, {}, ctx=ctx)["success"] is True
    assert _flag(engine.last["args"], "-k") == "designer"

    call(tools.handle_job_search, {"keyword": "backend engineer"}, ctx=ctx)
    assert _flag(engine.last["args"], "-k") == "backend engineer", "an explicit keyword wins"

    # A blank keyword means "omitted", not "search for nothing".
    call(tools.handle_job_search, {"keyword": "   "}, ctx=ctx)
    assert _flag(engine.last["args"], "-k") == "designer"


def test_default_sources_only_fill_in_omitted_sources(tools, engine):
    ctx = FakeContext({"default_sources": ["wantedly", "green"]})
    call(tools.handle_job_search, {}, ctx=ctx)
    assert _flag(engine.last["args"], "--source") == "wantedly,green"

    call(tools.handle_job_search, {"sources": ["daijob"]}, ctx=ctx)
    assert _flag(engine.last["args"], "--source") == "daijob", "an explicit list wins"

    call(tools.handle_job_search, {}, ctx=FakeContext())
    assert "--source" not in engine.last["args"], "no configured default means no flag"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param([], id="empty-list"),
        pytest.param("", id="empty-string"),
        pytest.param([None], id="list-of-null"),
        pytest.param(["", "   "], id="list-of-blanks"),
        pytest.param([None, ""], id="nulls-and-blanks"),
    ],
)
def test_a_blank_default_sources_never_becomes_an_empty_flag(tools, engine, value):
    """A half-cleared config form must not break every search.

    ``--source ''`` is not "no preference": the engine rejects it outright with
    "no valid source selected" and exits 2, so the tool would fail on *every*
    call while the setting looked harmlessly blank. Blank entries are
    legitimate — YAML writes an empty bullet as ``null`` — so the flag has to be
    omitted rather than passed empty.
    """
    call(tools.handle_job_search, {"save": False}, ctx=FakeContext({"default_sources": value}))
    assert "--source" not in engine.last["args"], f"{value!r} produced an empty --source"
    # The engine, not the plugin, still owes the user the built-in default feed:
    # omitting the flag is exactly how that happens.
    assert engine.last["args"][:2] == ["search", "--json"]


def test_partly_blank_source_lists_keep_the_real_boards(tools, engine):
    """Dropping the blanks must not drop the boards around them."""
    ctx = FakeContext({"default_sources": ["green", None, " daijob ", ""]})
    call(tools.handle_job_search, {}, ctx=ctx)
    assert _flag(engine.last["args"], "--source") == "green,daijob"


def test_max_results_is_the_default_limit_when_none_is_passed(tools, engine):
    """`max_results` fills a gap; it is deliberately not a ceiling.

    A caller that asks for 50 listings knows what it is doing — the tool must
    not quietly return 25 and let the model believe the board had no more.
    """
    ctx = FakeContext({"max_results": 5})
    call(tools.handle_job_list, {}, ctx=ctx)
    assert _flag(engine.last["args"], "-n") == "5"

    call(tools.handle_job_list, {"limit": 50}, ctx=ctx)
    assert _flag(engine.last["args"], "-n") == "50", "an explicit limit is not capped"

    call(tools.handle_job_search, {}, ctx=ctx)
    assert _flag(engine.last["args"], "-n") == "5"

    call(tools.handle_job_list, {}, ctx=FakeContext())
    assert "-n" not in engine.last["args"], "without max_results the CLI default applies"


def test_junk_max_results_degrades_to_the_default(tools, engine):
    """A typo in config.yaml must not turn every listing read into an error."""
    payload = call(tools.handle_job_list, {}, ctx=FakeContext({"max_results": "many"}))
    assert payload["success"] is True
    assert "-n" not in engine.last["args"]


def test_note_subfolder_is_a_default_and_an_explicit_one_wins(tools, engine):
    envelope = {"jobs": []}
    ctx = FakeContext({"note_subfolder": "from-config"})

    call(tools.handle_job_note, {"result": envelope}, ctx=ctx)
    assert _flag(engine.last["args"], "--subfolder") == "from-config"

    call(tools.handle_job_note, {"result": envelope, "subfolder": "explicit"}, ctx=ctx)
    assert _flag(engine.last["args"], "--subfolder") == "explicit"

    call(tools.handle_job_note, {"result": envelope}, ctx=FakeContext())
    assert "--subfolder" not in engine.last["args"]


def test_settings_are_read_once_per_call_and_forwarded(plugin, tools, engine):
    ctx = FakeContext({"default_keyword": "designer"})
    call(tools.handle_job_search, {}, ctx=ctx)
    assert ctx.config_reads == list(plugin.SETTINGS_KEYS), "every advertised key is read"
    assert engine.last["settings"] == {"default_keyword": "designer"}

    call(tools.handle_job_search, {}, ctx=ctx)
    assert len(ctx.config_reads) == 2 * len(plugin.SETTINGS_KEYS), "read per call, not cached"


def test_untouched_configuration_keeps_the_bridge_call_unchanged(tools, engine):
    """A user who never edited config.yaml sees exactly the pre-settings call."""
    call(tools.handle_job_list, {}, ctx=FakeContext())
    assert not engine.last["settings"]


def test_a_context_without_get_config_degrades_to_defaults(tools, engine):
    """Older Hermes has no ``get_config``; that must not fail the call."""

    class BareContext:
        state: dict[str, Any] = {}

    payload = call(tools.handle_job_list, {}, ctx=BareContext())
    assert payload["success"] is True


def test_a_context_that_is_missing_entirely_degrades_to_defaults(tools, engine):
    payload = call(tools.handle_job_list, {}, ctx=None)
    assert payload["success"] is True
    assert not engine.last["settings"]


# --------------------------------------------------------------------------- #
# The context reaches the registered handler by closure
# --------------------------------------------------------------------------- #


def test_registered_handlers_use_the_context_they_were_registered_with(
    registered: RecordingContext, engine
):
    registered.settings["max_results"] = 7
    handler = registered_handler(registered, "job_list")

    # Called exactly the way tools/registry.py does: args, no context.
    payload = json.loads(asyncio.run(handler({})))
    assert payload["success"] is True
    assert _flag(engine.last["args"], "-n") == "7"

    # Hermes forwards its own kwargs; a stray `ctx` must not displace ours.
    asyncio.run(handler({}, ctx=FakeContext({"max_results": 99})))
    assert _flag(engine.last["args"], "-n") == "7"


def test_registration_binds_the_tool_context_and_the_hook_context(plugin, registered):
    assert len(registered.tools) == 7
    assert registered.skill is not None
    assert registered.command == "jobs"
    assert registered.cli_command == "job-reach"
    assert set(registered.hooks) == {"post_tool_call"}


def test_the_registered_hook_declares_the_documented_payload(registered):
    """Hermes passes only the fields a callback declares, plus ``**kwargs``."""
    parameters = inspect.signature(registered.hooks["post_tool_call"]).parameters
    for field in ("tool_name", "args", "result", "task_id", "duration_ms"):
        assert field in parameters, f"the post_tool_call callback is missing {field!r}"
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


# --------------------------------------------------------------------------- #
# The tool-call journal
# --------------------------------------------------------------------------- #


def test_the_journal_ignores_foreign_tools_and_records_ours(registered, engine):
    callback = registered.hooks["post_tool_call"]

    callback(tool_name="terminal", args={"command": "ls"}, result="ok", task_id="t1")
    assert not registered.state.get("recent_tool_calls"), "only this plugin's calls belong here"

    result = asyncio.run(registered_handler(registered, "job_search")({"keyword": "designer"}))
    callback(tool_name="job_search", args={"keyword": "designer"}, result=result, task_id="t1")

    journal = registered.state["recent_tool_calls"]
    assert [entry["tool"] for entry in journal] == ["job_search"]
    entry = journal[0]
    assert set(entry) == {"tool", "at", "ok", "detail"}
    assert entry["ok"] is True
    assert entry["detail"] == "0 listings"
    assert json.dumps(registered.state), "the journal must be JSON-serialisable state"


def test_the_journal_is_newest_first_and_capped_at_twenty(registered):
    callback = registered.hooks["post_tool_call"]
    for index in range(25):
        callback(
            tool_name="job_note",
            args={},
            result=json.dumps({"success": True, "note": f"/vault/{index}.md"}),
            task_id="t1",
        )

    journal = registered.state["recent_tool_calls"]
    assert len(journal) == 20
    assert journal[0]["detail"] == "/vault/24.md", "newest first"
    assert journal[-1]["detail"] == "/vault/5.md", "the oldest entries are dropped"


def test_the_journal_records_failures_and_unparseable_results(registered):
    callback = registered.hooks["post_tool_call"]
    for result in (
        None,
        "not json at all",
        json.dumps([1, 2, 3]),
        json.dumps({"success": False, "error": "the store is locked"}),
    ):
        callback(tool_name="job_list", args={}, result=result, task_id="t1")

    journal = registered.state["recent_tool_calls"]
    assert len(journal) == 4
    assert all(entry["ok"] is False for entry in journal)
    assert journal[0]["detail"] == "the store is locked", "newest first"
    assert all(entry["detail"] for entry in journal), "every entry says something"


def test_recording_swallows_a_state_error(plugin):
    """A corrupt state file must not break the hook — or even be logged as broken."""
    state = ExplodingState()
    ctx = RecordingContext(state=state)
    plugin.register(ctx)
    callback = ctx.hooks["post_tool_call"]

    callback(tool_name="job_list", args={}, result=json.dumps({"success": True}))
    plugin.record_tool_call(tool_name="job_list", result="{}", ctx=ctx)

    # "Did not raise" alone is too weak — a hook that did nothing would pass it,
    # which is how this test survived a mutation that made the recorder a no-op.
    # The spy proves the recorder really tried: it read the journal and therefore
    # had to swallow the failure rather than never reaching the facade.
    assert state.calls, "the recorder never touched the state facade — is it still wired up?"
    assert state.calls[0].startswith("get:"), f"expected a journal read first, got {state.calls}"


def test_recording_without_state_is_simply_not_recorded(plugin):
    class BareContext:
        pass

    plugin.record_tool_call(tool_name="job_list", result="{}", ctx=BareContext())
    plugin.record_tool_call(tool_name="job_list", result="{}", ctx=None)

    # A context with no `state` has nowhere to record and must not invent one.
    assert plugin.tools.journal_read(BareContext()) == []
    assert plugin.tools.journal_read(None) == []


def test_a_foreign_tool_result_is_never_parsed_for_detail(registered):
    callback = registered.hooks["post_tool_call"]
    callback(tool_name="job_setup", args={}, result=json.dumps({"success": True, "skill": "/s.md"}))
    callback(tool_name="read_file", args={}, result="not ours")

    journal = registered.state["recent_tool_calls"]
    assert [entry["tool"] for entry in journal] == ["job_setup"]
    assert journal[0]["detail"] == "/s.md"


# --------------------------------------------------------------------------- #
# job_status reads the journal back
# --------------------------------------------------------------------------- #


def test_job_status_reports_the_journal(registered, engine):
    callback = registered.hooks["post_tool_call"]
    callback(
        tool_name="job_list",
        args={},
        result=json.dumps({"success": True, "result": {"jobs": []}}),
        task_id="t1",
    )

    payload = json.loads(asyncio.run(registered_handler(registered, "job_status")({})))
    assert payload["success"] is True
    assert payload["recent_tool_calls"][0]["tool"] == "job_list"


def test_job_status_tolerates_a_context_without_state(tools, engine):
    payload = call(tools.handle_job_status, {}, ctx=None)
    assert payload["success"] is True
    assert payload["recent_tool_calls"] == []


def test_job_status_honours_recent_runs_but_keeps_the_journal(tools, engine):
    engine.payload = {"success": True, "recent_runs": [1, 2, 3, 4, 5, 6, 7]}
    payload = call(tools.handle_job_status, {"recent_runs": 2})
    assert payload["store"]["recent_runs"] == [1, 2]
    assert payload["recent_tool_calls"] == []

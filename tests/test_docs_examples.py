"""The frozen external-plugin fixture: what a freshly installed Hermes sees.

Why this module exists
----------------------
Every other suite imports the plugin straight out of the working tree, where
``tests/conftest.py`` has already put the repo root on ``sys.path``. That makes
three classes of bug invisible: a relative import that only resolves because
the tree happens to be importable one way, a third-party import that only
resolves because the developer's venv is loaded, and a write into the plugin
directory that only shows up on a machine where Hermes installed the plugin
somewhere else.

The official plugin guide is explicit that *"Hermes enforces this contract with
frozen external-plugin fixtures discovered from an isolated HERMES_HOME"* and
that those fixtures assert *real registration and callback outcomes*, not
internal symbol lists. This module is that fixture, written without importing
Hermes: it copies the plugin tree into ``<temp HERMES_HOME>/plugins/job-reach/``,
imports **that copy** as a package from that location, and drives it through a
fake ``PluginContext`` that implements exactly the surface the guide documents.

The fixture is deliberately hostile:

* an undocumented ``ctx.*`` attribute raises instead of silently no-op'ing — a
  typo in a ``register_*`` name otherwise costs a feature, not an error;
* the engine bridge is a recording stub, and sockets are refused, so no handler
  may reach the network;
* the copied tree is snapshotted, so nothing may write into the directory
  Hermes replaces on every update.

A test here that fails because a promised symbol is missing says so in the
failure message, rather than dying with an ``AttributeError`` traceback.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: What must never travel into the frozen copy: build artefacts and caches (the
#: plugin is loaded from source), VCS metadata, and the developer's ``.env``.
COPY_IGNORES = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", ".git", "*cache*", ".env"
)

#: Files whose appearance is *not* the plugin writing into its own directory:
#: ``__pycache__`` is the interpreter caching bytecode, and Hermes replaces the
#: whole plugin directory on update, so the rule being pinned is about data.
SNAPSHOT_IGNORES = ("__pycache__",)


# --------------------------------------------------------------------------- #
# A PluginContext limited to the documented surface
# --------------------------------------------------------------------------- #


class FakeState:
    """``ctx.state`` exactly as the guide documents it: ``get`` / ``set``.

    The real facade (``hermes_cli/plugins_state.py::PluginState``) exposes no
    item access, so a plugin that subscripts the journal (``ctx.state["k"]``)
    is broken against Hermes even though a dict-backed fake would hide it.
    """

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class StrictPluginContext:
    """Stand-in for Hermes' ``PluginContext``, strict about its surface.

    Every method here mirrors the signature in the official guide (and in
    ``hermes_cli/plugins.py``); anything else the plugin calls raises
    ``AttributeError`` naming the undocumented attribute, so a registration
    typo fails the suite instead of silently disabling a feature.
    """

    #: The ``ctx`` surface the plugin guide documents for a native plugin.
    DOCUMENTED = frozenset(
        {
            "plugin_id",
            "profile_name",
            "register_tool",
            "register_hook",
            "register_skill",
            "register_command",
            "register_cli_command",
            "get_config",
            "set_config",
            "state",
            "has_capability",
            "dispatch_tool",
        }
    )

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.plugin_id = "job-reach"
        self.profile_name = "default"
        self.settings: dict[str, Any] = dict(settings or {})
        self.state = FakeState()
        self.tools: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.cli_commands: list[dict[str, Any]] = []
        self.hooks: list[tuple[str, Callable[..., Any]]] = []
        self.dispatched: list[tuple[str, dict[str, Any]]] = []

    # -- registration ------------------------------------------------------- #

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        check_fn: Callable[..., Any] | None = None,
        requires_env: list[str] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ) -> None:
        self.tools.append(
            {
                "name": name,
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                "check_fn": check_fn,
                "requires_env": requires_env,
                "is_async": is_async,
                "description": description,
                "emoji": emoji,
                "override": override,
            }
        )

    def register_hook(self, hook_name: str, callback: Callable[..., Any]) -> None:
        self.hooks.append((hook_name, callback))

    def register_skill(
        self,
        name: str,
        path: Path,
        description: str = "",
        frontmatter: dict[str, Any] | None = None,
    ) -> None:
        self.skills.append(
            {
                "name": name,
                "path": Path(path),
                "description": description,
                "frontmatter": dict(frontmatter or {}),
            }
        )

    def register_command(
        self,
        name: str,
        handler: Callable[..., Any],
        description: str = "",
        args_hint: str = "",
        argument_mode: str | None = None,
    ) -> None:
        self.commands.append(
            {
                "name": name,
                "handler": handler,
                "description": description,
                "args_hint": args_hint,
                "argument_mode": argument_mode,
            }
        )

    def register_cli_command(
        self,
        name: str,
        help: str,
        setup_fn: Callable[..., Any],
        handler_fn: Callable[..., Any] | None = None,
        description: str = "",
    ) -> None:
        self.cli_commands.append(
            {
                "name": name,
                "help": help,
                "setup_fn": setup_fn,
                "handler_fn": handler_fn,
                "description": description,
            }
        )

    # -- settings, state and dispatch --------------------------------------- #

    def get_config(self, key: str, default: Any = None) -> Any:
        """Plugin-relative settings read; the fake holds a flat mapping."""
        return self.settings.get(key, default)

    def set_config(self, key: str, value: Any) -> None:
        self.settings[key] = value

    def has_capability(self, capability: str) -> bool:
        """This plugin declares no capability, so every probe is False."""
        return False

    def dispatch_tool(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        self.dispatched.append((tool_name, dict(args or {})))
        return json.dumps({"success": True, "tool": tool_name})

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal attribute lookup fails — i.e. the plugin
        # called something the guide does not document.
        raise AttributeError(
            f"ctx.{name!r} is not part of the PluginContext surface the official "
            f"plugin guide documents; inside Hermes this call would fail or "
            f"silently do nothing. Documented surface: "
            f"{', '.join(sorted(self.DOCUMENTED))}"
        )


# --------------------------------------------------------------------------- #
# The frozen copy
# --------------------------------------------------------------------------- #


class FrozenPlugin:
    """A copy of the plugin installed under ``<HERMES_HOME>/plugins/job-reach``."""

    def __init__(self, plugin_dir: Path) -> None:
        self.plugin_dir = plugin_dir
        # Unique per test: two frozen copies must never share sys.modules
        # entries, or the second test would talk to the first one's objects.
        self.package_name = f"frozen_job_reach_{uuid.uuid4().hex[:10]}"
        self.module: Any = None
        self.ctx: StrictPluginContext | None = None

    def load(self) -> FrozenPlugin:
        """Import the copy the way Hermes' loader does: as a package, from there."""
        spec = importlib.util.spec_from_file_location(
            self.package_name,
            self.plugin_dir / "__init__.py",
            submodule_search_locations=[str(self.plugin_dir)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[self.package_name] = module
        spec.loader.exec_module(module)
        self.module = module

        # Self-containment guard: a relative import that silently resolves to
        # the working tree would make this whole module a lie.
        tools = importlib.import_module(f"{self.package_name}.tools")
        engine_module = tools.run_engine.__module__
        assert engine_module.startswith(f"{self.package_name}."), (
            f"the frozen copy imported its engine from {engine_module!r} instead of "
            f"from the copy — the fixture is testing the working tree, not the "
            f"installed plugin"
        )
        return self

    def register(self) -> StrictPluginContext:
        if self.module is None:
            self.load()
        ctx = StrictPluginContext()
        self.module.register(ctx)
        self.ctx = ctx
        return ctx

    def handler(self, name: str) -> Callable[..., Any]:
        ctx = self.ctx or self.register()
        for tool in ctx.tools:
            if tool["name"] == name:
                return tool["handler"]
        pytest.fail(
            f"{name!r} is not registered by the frozen copy; registered: "
            f"{sorted(tool['name'] for tool in ctx.tools)}"
        )


@pytest.fixture()
def frozen_copy(data_home: Path) -> FrozenPlugin:
    """Copy the tree into ``$HERMES_HOME/plugins/job-reach`` — imported, not yet loaded."""
    hermes_home = Path(os.environ["HERMES_HOME"])
    target = hermes_home / "plugins" / "job-reach"
    shutil.copytree(PROJECT_ROOT, target, ignore=COPY_IGNORES)
    return FrozenPlugin(target)


@pytest.fixture()
def frozen(frozen_copy: FrozenPlugin) -> FrozenPlugin:
    """The copy, imported from its installed location."""
    return frozen_copy.load()


@pytest.fixture()
def manifest() -> dict[str, Any]:
    return yaml.safe_load((PROJECT_ROOT / "plugin.yaml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def accepts_var_kwargs(callback: Any) -> bool:
    """Mirror of the plugin doctor's ``_accepts_var_kwargs`` check."""
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters)


def declares_parameters(callback: Any, names: tuple[str, ...]) -> bool:
    """True when *callback* can receive every name in *names* as a keyword."""
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return all(name in parameters for name in names)


def call_handler(handler: Callable[..., Any], params: Any) -> Any:
    """Invoke a handler the way ``tools/registry.py::dispatch`` does."""
    result = handler(params)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return result


def hook_for(plugin: FrozenPlugin, name: str) -> Callable[..., Any]:
    """Return the registered hook callback, or fail loudly about the promise."""
    ctx = plugin.ctx or plugin.register()
    callbacks = [callback for hook, callback in ctx.hooks if hook == name]
    if not callbacks:
        pytest.fail(
            f"the frozen copy registered no {name!r} hook (registered: "
            f"{[hook for hook, _ in ctx.hooks] or 'none'}). SPEC WS-A item 6: "
            f"register(ctx) must call ctx.register_hook(\"post_tool_call\", "
            f"record_tool_call); SPEC WS-E item 1: plugin.yaml must declare "
            f"provides_hooks: [post_tool_call]."
        )
    assert len(callbacks) == 1, f"{name!r} was registered {len(callbacks)} times"
    return callbacks[0]


class RecordingEngine:
    """Stand-in for ``run_engine``: records the argv, spawns nothing.

    Accepts ``**kwargs`` on purpose — the bridge is allowed to grow keyword
    parameters (the settings bridge added ``settings=``), and a stub that
    pinned the exact signature would blame the plugin for the stub's rigidity.
    """

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload if payload is not None else {
            "ok": True,
            "total": 0,
            "jobs": [],
            "recent_runs": [],
        }
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
            {
                "args": list(args),
                "timeout": timeout,
                "stdin": stdin,
                "settings": settings,
                "kwargs": kwargs,
            }
        )
        return subprocess.CompletedProcess(
            list(args), 0, json.dumps(self.payload), ""
        )


@pytest.fixture()
def stub_engine(frozen: FrozenPlugin, monkeypatch: pytest.MonkeyPatch) -> RecordingEngine:
    """Replace the engine bridge in the copy, in both places it is bound."""
    stub = RecordingEngine()
    monkeypatch.setattr(frozen.module.tools, "run_engine", stub)
    runtime = sys.modules[f"{frozen.package_name}.jobreach.runtime"]
    monkeypatch.setattr(runtime, "run_engine", stub)
    return stub


@pytest.fixture()
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor's guarantee, locally: a handler under test may not open a socket."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a tool handler tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# --------------------------------------------------------------------------- #
# The registered surface
# --------------------------------------------------------------------------- #


def test_the_frozen_copy_registers_the_surface_plugin_yaml_declares(
    frozen: FrozenPlugin, manifest: dict[str, Any]
):
    """Seven tools, one skill, one slash command, one CLI command, one hook."""
    ctx = frozen.register()

    declared_tools = manifest.get("provides_tools") or []
    assert declared_tools, "plugin.yaml declares no provides_tools"
    assert sorted(tool["name"] for tool in ctx.tools) == sorted(declared_tools)
    assert len(ctx.tools) == 7, "the tool surface is seven tools; see schemas.TOOL_NAMES"
    assert {tool["toolset"] for tool in ctx.tools} == {"job_reach"}
    assert all(tool["is_async"] for tool in ctx.tools), "handlers are async; register is_async=True"
    assert all(tool["schema"] for tool in ctx.tools), "a tool without a schema is invisible to the model"
    assert all(tool["handler"] for tool in ctx.tools)

    if not ctx.skills:
        pytest.fail(
            "the frozen copy registered no skill. register(ctx) must publish the "
            "bundled skills/job-search/SKILL.md (SPEC WS-A item 5 keeps skill "
            "registration best-effort but logged, never silent)."
        )
    (skill,) = ctx.skills
    assert skill["name"] == "job-search"
    assert skill["path"] == frozen.plugin_dir / "skills" / "job-search" / "SKILL.md"
    assert skill["path"].exists(), "the copy must ship the skill file it registers"

    assert [command["name"] for command in ctx.commands] == ["jobs"]
    assert callable(ctx.commands[0]["handler"])
    assert [command["name"] for command in ctx.cli_commands] == ["job-reach"]
    assert callable(ctx.cli_commands[0]["setup_fn"])

    declared_hooks = manifest.get("provides_hooks")
    if declared_hooks is None:
        pytest.fail(
            "plugin.yaml has no provides_hooks key. SPEC WS-E item 1: declare "
            "provides_hooks: [post_tool_call]."
        )
    assert sorted(hook for hook, _ in ctx.hooks) == sorted(declared_hooks), (
        "the manifest and register(ctx) disagree about which hooks exist; a "
        "declared hook that is never registered is a silent observability hole"
    )
    hook_for(frozen, "post_tool_call")  # fails loudly, naming the promise


# --------------------------------------------------------------------------- #
# Handlers: a JSON string for hostile input, no network
# --------------------------------------------------------------------------- #

#: Per-tool hostile arguments. ``None`` and ``{}`` are not hypothetical — they
#: are what a model sends for an all-optional schema and what a dispatcher can
#: hand over for a null argument object — and the wrong-typed variants are the
#: coercion sites the guide's "Never raise" rule is really about.
HOSTILE_INPUTS: dict[str, list[Any]] = {
    "job_search": [None, {}, {"sources": 42, "limit": "many", "keyword": ["a"]}],
    "job_ingest": [None, {}, {"jobs": "nope"}],
    "job_list": [None, {}, {"limit": "many", "text": {"nested": True}}],
    "job_note": [None, {}, {"result": "not-an-envelope"}],
    "job_status": [None, {}, {"recent_runs": "five"}],
    "job_setup": [None, {}, {"with_browser": "yes", "install_skill": "maybe"}],
    "job_cron": [None, {}, {"schedule": 5, "sources": ["wantedly"]}],
}


@pytest.mark.parametrize("tool_name", sorted(HOSTILE_INPUTS))
def test_registered_handlers_return_json_for_hostile_input(
    tool_name: str,
    frozen: FrozenPlugin,
    stub_engine: RecordingEngine,
    no_network: None,
):
    """A handler is the plugin's only mouth: it always answers, always in JSON."""
    frozen.register()
    handler = frozen.handler(tool_name)

    for params in HOSTILE_INPUTS[tool_name]:
        try:
            raw = call_handler(handler, params)
        except Exception as exc:  # noqa: BLE001 — that is exactly the failure being pinned
            pytest.fail(
                f"{tool_name}({params!r}) raised {type(exc).__name__}: {exc}. "
                f"SPEC WS-A item 2 / AGENTS.md invariant 4: every handler returns "
                f"a JSON error envelope for any input and never raises."
            )
        assert isinstance(raw, str), f"{tool_name}({params!r}) returned {type(raw).__name__}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            pytest.fail(f"{tool_name}({params!r}) returned non-JSON: {raw[:200]!r} ({exc})")
        assert isinstance(payload, dict), f"{tool_name}({params!r}) returned a JSON {type(payload).__name__}"
        assert "success" in payload or "error" in payload, (
            f"{tool_name}({params!r}) returned a payload that reports nothing: {payload!r}"
        )


def test_the_engine_bridge_stub_is_actually_used(
    frozen: FrozenPlugin, stub_engine: RecordingEngine
):
    """Guards the guard: if the stub were bypassed, the tests above could hit the network."""
    frozen.register()
    raw = call_handler(frozen.handler("job_list"), {})
    assert json.loads(raw)["success"] is True
    assert stub_engine.calls, "the handler did not go through run_engine at all"
    assert stub_engine.calls[0]["args"][:2] == ["list", "--json"]


# --------------------------------------------------------------------------- #
# The stdlib-only promise, measured in a clean interpreter
# --------------------------------------------------------------------------- #

#: Import every module of the copied engine in an interpreter started with
#: ``-S`` (no ``site``, therefore no site-packages). Only a ``jobreach`` module
#: may be missing: anything else is a third-party import that would break
#: Hermes' own runtime venv. Mirrors ``tests/test_platforms.py``'s probe.
CLEAN_IMPORT_PROBE = """
import importlib, json, sys

failures = []
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".")[0]
        if missing == "jobreach":
            continue  # an engine module, not a third-party leak
        failures.append({"module": name, "missing": missing, "error": str(exc)})
    except Exception as exc:  # a syntax error or import-time crash is a failure too
        failures.append({"module": name, "missing": None, "error": f"{type(exc).__name__}: {exc}"})
print(json.dumps(failures))
"""


def engine_module_names(plugin_dir: Path) -> list[str]:
    """Every importable ``jobreach.*`` module in the copy, parents first."""
    names: set[str] = set()
    for path in (plugin_dir / "jobreach").rglob("*.py"):
        if any(part in SNAPSHOT_IGNORES for part in path.parts):
            continue
        parts = list(path.relative_to(plugin_dir).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.add(".".join(parts))
    return sorted(names, key=lambda name: (name.count("."), name))


def run_in_clean_interpreter(plugin_dir: Path, code: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(plugin_dir)}
    env.pop("VIRTUAL_ENV", None)
    return subprocess.run(
        [sys.executable, "-S", "-c", code, *arguments],
        capture_output=True,
        text=True,
        cwd=plugin_dir,
        env=env,
        timeout=180,
    )


def test_the_frozen_copy_imports_without_third_party_packages(frozen_copy: FrozenPlugin):
    names = engine_module_names(frozen_copy.plugin_dir)
    assert "jobreach.cli" in names and "jobreach.settings" in names, names

    # Baseline first: prove the probe interpreter really has no site-packages,
    # so a green result cannot be a vacuous one.
    baseline = run_in_clean_interpreter(frozen_copy.plugin_dir, "import pytest")
    assert baseline.returncode != 0, "the probe interpreter loaded site-packages; -S did not apply"
    assert "ModuleNotFoundError" in baseline.stderr, baseline.stderr[-400:]

    result = run_in_clean_interpreter(
        frozen_copy.plugin_dir, CLEAN_IMPORT_PROBE, json.dumps(names)
    )
    assert result.returncode == 0, result.stderr[-2000:]
    failures = json.loads(result.stdout.strip().splitlines()[-1])
    assert failures == [], (
        f"AGENTS.md invariant 1: jobreach/ is standard-library only. "
        f"The frozen copy pulled in third-party modules: {failures}"
    )


# --------------------------------------------------------------------------- #
# Nothing writes into the plugin's own directory
# --------------------------------------------------------------------------- #


def tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """``{relative path: (mtime_ns, size)}`` for every real file under *root*."""
    snapshot: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SNAPSHOT_IGNORES for part in path.parts):
            continue
        stat = path.stat()
        snapshot[str(path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def test_importing_registering_and_reporting_never_writes_into_the_plugin(
    frozen_copy: FrozenPlugin
):
    """Hermes replaces the plugin directory on every update; data lives elsewhere.

    ``job_status`` runs the *real* engine (no stub): this is also the one test
    that proves the copy is a complete, self-contained plugin, started from
    ``$JOBREACH_HOME`` rather than from the working tree.
    """
    before = tree_snapshot(frozen_copy.plugin_dir)

    frozen_copy.load()
    ctx = frozen_copy.register()
    raw = call_handler(frozen_copy.handler("job_status"), {})
    payload = json.loads(raw)
    assert payload.get("success") is True, (
        f"the frozen copy could not report its own status: {raw[:400]}"
    )

    after = tree_snapshot(frozen_copy.plugin_dir)
    changed = sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )
    assert changed == [], (
        f"the plugin wrote into its own install directory: {changed}. "
        f"AGENTS.md invariant 5: user state lives under "
        f"$HERMES_HOME/plugin-data/job-reach/, never in the plugin directory."
    )
    assert ctx.tools, "registration should have happened before the snapshot was compared"


# --------------------------------------------------------------------------- #
# The observability hook
# --------------------------------------------------------------------------- #


def test_the_post_tool_call_hook_accepts_the_documented_payload(frozen: FrozenPlugin):
    """The guide: a callback with ``**kwargs`` receives the complete payload."""
    frozen.register()
    callback = hook_for(frozen, "post_tool_call")
    assert accepts_var_kwargs(callback), (
        "the post_tool_call callback must accept **kwargs; a legacy callback only "
        "receives the fields it declares, so an additive Hermes release would "
        "silently starve it"
    )
    assert declares_parameters(callback, ("tool_name", "args", "result", "task_id")), (
        "the guide's post_tool_call signature is "
        "(tool_name, args, result, task_id, duration_ms, **kwargs)"
    )


def test_the_post_tool_call_hook_journals_our_tools_and_ignores_foreign_ones(
    frozen: FrozenPlugin,
):
    """``ctx.state`` gets a compact, newest-first, capped journal of our calls."""
    frozen.register()
    callback = hook_for(frozen, "post_tool_call")

    callback(tool_name="terminal", args={"command": "ls"}, result="ok",
             task_id="t1", duration_ms=3)
    assert not frozen.ctx.state.get("recent_tool_calls"), (
        "the journal must only hold this plugin's own tool calls"
    )

    callback(
        tool_name="job_search",
        args={"keyword": "designer"},
        result=json.dumps(
            {"success": True, "result": {"jobs": [{"title": "A"}, {"title": "B"}]}}
        ),
        task_id="t1",
        duration_ms=1200,
    )
    callback(
        tool_name="job_list",
        args={},
        result=json.dumps({"success": True, "result": {"jobs": []}}),
        task_id="t1",
        duration_ms=10,
    )

    journal = frozen.ctx.state.get("recent_tool_calls")
    assert isinstance(journal, list) and journal, (
        "SPEC WS-A item 6c: the hook appends {'tool', 'at', 'ok', 'detail'} to "
        "ctx.state['recent_tool_calls'] (read/written through the documented "
        "get()/set() facade — the real PluginState has no item access)"
    )
    assert [entry["tool"] for entry in journal[:2]] == ["job_list", "job_search"], (
        f"the journal is newest-first: {journal}"
    )
    for entry in journal:
        assert {"tool", "at", "ok", "detail"} <= set(entry), entry
        assert isinstance(entry["ok"], bool), entry
    assert journal[-1]["ok"] is True
    assert json.dumps(frozen.ctx.state.data), "the journal must be JSON-serialisable state"

    # Garbage results and a state-less context must not raise: the guide logs a
    # crashing hook as broken, and this one must never even get there.
    for garbage in (None, "not json at all", json.dumps([1, 2, 3]), json.dumps({"success": False})):
        callback(tool_name="job_note", args={}, result=garbage, task_id="t1", duration_ms=1)
    assert len(frozen.ctx.state.get("recent_tool_calls")) <= 20, "the journal is capped at 20"


def test_a_job_status_call_can_see_the_journal(
    frozen: FrozenPlugin, stub_engine: RecordingEngine
):
    """The journal is only useful if the agent can read it back."""
    frozen.register()
    callback = hook_for(frozen, "post_tool_call")
    callback(
        tool_name="job_list",
        args={},
        result=json.dumps({"success": True, "result": {"jobs": []}}),
        task_id="t1",
        duration_ms=5,
    )

    payload = json.loads(call_handler(frozen.handler("job_status"), {}))
    assert payload.get("success") is True, payload
    assert "recent_tool_calls" in payload, (
        "SPEC WS-A item 7 / goal G3: job_status must surface recent_tool_calls "
        f"from ctx.state; got keys {sorted(payload)}"
    )
    assert payload["recent_tool_calls"][0]["tool"] == "job_list"

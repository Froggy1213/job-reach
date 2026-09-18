#!/usr/bin/env python3
"""End-to-end harness: drive the Job Reach tools the way Hermes does.

This is the acceptance test for the refactor, and it deliberately does **not**
import pytest fixtures or the repo's own test helpers. It builds a package out of
the plugin directory (Hermes' loader makes the plugin directory a package, so
``tools.py``'s relative imports only work that way), registers everything through
a fake ``PluginContext`` that mirrors the documented surface, and then calls the
handlers exactly as ``tools/registry.py::dispatch`` does:

    entry.handler(args, **kwargs)

What it proves, in order:

1. ``register(ctx)`` succeeds and registers the surface ``plugin.yaml`` declares.
2. Every handler returns a JSON **string** for hostile input — the guide's
   "never raise, always return JSON" rule. Any escaping exception fails the run.
3. A real engine call works through the subprocess bridge against a throwaway
   ``JOBREACH_HOME`` (SQLite + JSON envelope, no network).
4. A setting handed to the handler changes the engine's observable behaviour,
   which is the whole point of the ``JOBREACH_SETTING_*`` bridge.
5. A ``post_tool_call`` hook payload recorded via the fake context survives into
   ``job_status``'s journal.

Usage::

    python3 tools_acceptance.py [plugin_dir]        # default: cwd

Exit code 0 = all checks passed; 1 = at least one failed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# Test scaffolding
# --------------------------------------------------------------------------- #

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. Never raises — the run reports everything at the end."""
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {detail}")


class FakeState:
    """The documented ``ctx.state`` facade, backed by a plain dict."""

    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value


class FakeContext:
    """A strict stand-in for ``PluginContext``.

    Only the methods the guide documents exist here, so a handler that reaches
    for an undocumented ``ctx.register_*`` (or for ``ctx._cli_ref``) fails loudly
    during registration instead of silently no-op'ing in production.
    """

    def __init__(self, settings: dict | None = None) -> None:
        self.tools: dict[str, dict] = {}
        self.hooks: dict[str, list] = {}
        self.skills: list[tuple[str, str, str]] = []
        self.commands: list[tuple[str, str]] = []
        self.cli_commands: list[tuple[str, str]] = []
        self.state = FakeState()
        self._settings = dict(settings or {})
        self.plugin_id = "job-reach"

    # -- registration ------------------------------------------------------ #
    def register_tool(self, name, toolset, schema, handler, check_fn=None,
                      requires_env=None, is_async=False, description="", emoji="",
                      override=False):
        self.tools[name] = {
            "toolset": toolset, "schema": schema, "handler": handler,
            "is_async": is_async, "description": description, "emoji": emoji,
            "override": override, "check_fn": check_fn,
        }

    def register_hook(self, hook_name, callback):
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append((name, str(path), description))

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands.append((name, description))

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands.append((name, help))

    # -- documented read surfaces ------------------------------------------ #
    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def set_config(self, key, value):
        self._settings[key] = value

    def has_capability(self, capability: str) -> bool:
        return False

    def has_plugin(self, plugin_id: str) -> bool:
        return False

    def dispatch_tool(self, name, args, **kwargs):
        raise AssertionError("the plugin must not re-dispatch tools during these tests")


def build_package(plugin_dir: Path, root: Path) -> str:
    """Mirror Hermes' loader: load the plugin's ``__init__.py`` by path as a package.

    Hermes never copies or renames the plugin. ``plugins/plugin_loader.py``
    reserves a module name for the plugin directory, **executes its sibling
    ``*.py`` files first** (binding them onto the package), and only then runs
    ``__init__.py`` — which is what makes ``from .tools import …`` work inside
    it, and what makes ``tools.py``'s own relative imports resolve. Reproducing
    that order matters: a harness that imports submodules differently can pass
    while production fails, or the reverse.

    Returns the package name to import.
    """
    import importlib.util

    name = "jobreach_plugin_under_test"
    if name in sys.modules and getattr(sys.modules[name], "__file__", None):
        return name

    def new_module(module_name: str, file: Path, search: list[str] | None = None):
        spec = importlib.util.spec_from_file_location(
            module_name, str(file), submodule_search_locations=search)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        return module

    package = new_module(name, plugin_dir / "__init__.py", [str(plugin_dir)])
    siblings = []
    for source in sorted(plugin_dir.glob("*.py")):
        if source.name == "__init__.py":
            continue
        sub = new_module(f"{name}.{source.stem}", source)
        sub.__spec__.loader.exec_module(sub)
        siblings.append((source.stem, sub))
    package.__spec__.loader.exec_module(package)
    for stem, sub in siblings:
        setattr(package, stem, sub)
    return name


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    plugin_dir = Path(argv[0] if argv else os.getcwd()).resolve()
    print(f"Plugin directory: {plugin_dir}\n")

    work = Path(tempfile.mkdtemp(prefix="jobreach-acceptance-"))
    home = work / "jobreach-home"
    home.mkdir()
    os.environ["JOBREACH_HOME"] = str(home)
    os.environ.pop("JOBREACH_DB", None)
    os.environ.pop("JOBREACH_PYTHON", None)

    build_package(plugin_dir, work)
    sys.path.insert(0, str(work))
    # Import the package first: Hermes' loader executes the plugin's __init__.py,
    # and `tools.py` reads `SETTINGS_KEYS` off that package, so importing a
    # submodule in isolation would fail exactly as it would in production.
    import jobreach_plugin_under_test as plugin  # noqa: E402
    import jobreach_plugin_under_test.tools as tools  # noqa: E402

    print("1. registration")
    ctx = FakeContext(settings={"default_sources": ["wantedly"], "max_results": 1})
    try:
        plugin.register(ctx)
        check("register(ctx) does not raise", True)
    except Exception as exc:  # noqa: BLE001
        check("register(ctx) does not raise", False, f"{type(exc).__name__}: {exc}")
        return _report()

    declared = _declared_tools(plugin_dir)
    check("registered tools == plugin.yaml provides_tools",
          set(ctx.tools) == declared, f"registered {sorted(ctx.tools)}, declared {sorted(declared)}")
    check("every tool has a schema and a description",
          all(t["schema"] and t["description"] for t in ctx.tools.values()), "a tool lacks schema/description")
    check("every handler is async", all(t["is_async"] for t in ctx.tools.values()), "a tool is sync")
    check("skill registered", len(ctx.skills) == 1, f"{len(ctx.skills)} skills")
    check("slash command /jobs registered", [c[0] for c in ctx.commands] == ["jobs"], str(ctx.commands))
    check("CLI command job-reach registered",
          [c[0] for c in ctx.cli_commands] == ["job-reach"], str(ctx.cli_commands))
    check("post_tool_call hook registered", ctx.hooks.get("post_tool_call"), str(list(ctx.hooks)))

    print("\n2. handlers never raise (hostile input, stubbed engine)")
    real_run_engine = tools.run_engine

    class Crashing:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("synthetic bridge failure")

    hostile = [
        ("job_list", {"limit": "many"}),
        ("job_list", {"offset": object()}),
        ("job_status", {"recent_runs": "five"}),
        ("job_search", {"keyword": 12, "limit": "lots"}),
        ("job_ingest", {"jobs": "nope"}),
        ("job_note", {"result": "not-a-dict"}),
        ("job_cron", {"schedule": 42}),
    ]
    for name, params in hostile:
        handler = ctx.tools[name]["handler"]
        try:
            out = asyncio.run(asyncio.wait_for(handler(params), timeout=60))
        except Exception as exc:  # noqa: BLE001
            check(f"{name}{_short(params)} does not raise", False, f"{type(exc).__name__}: {exc}")
            continue
        if not isinstance(out, str):
            check(f"{name}{_short(params)} returns a JSON string", False, f"got {type(out).__name__}")
            continue
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            check(f"{name}{_short(params)} returns parseable JSON", False, str(exc))
            continue
        check(f"{name}{_short(params)} -> JSON envelope", "success" in payload, f"keys: {sorted(payload)}")

    # A crashed bridge must become an error envelope, not an exception.
    tools.run_engine = Crashing()
    try:
        out = asyncio.run(asyncio.wait_for(ctx.tools["job_status"]["handler"]({}), timeout=60))
        payload = json.loads(out)
        check("crashed engine bridge -> success:false envelope",
              payload.get("success") is False and payload.get("error"), str(payload)[:200])
    except Exception as exc:  # noqa: BLE001
        check("crashed engine bridge -> success:false envelope", False, f"{type(exc).__name__}: {exc}")
    finally:
        tools.run_engine = real_run_engine

    print("\n3. real subprocess bridge (no network)")
    envelope = _handler(ctx, "job_ingest", {
        "jobs": [{"title": "Acceptance UI Designer", "url": "https://example.invalid/a", "company": "ACME"}],
        "source": "indeed", "save": True,
    })
    ingested = (envelope or {}).get("result") or {}
    check("job_ingest reaches the engine and persists",
          bool(envelope) and envelope.get("success") is True
          and (ingested.get("summary") or {}).get("saved") == 1,
          str(envelope)[:220])
    listed_envelope = _handler(ctx, "job_list", {"text": "Acceptance"})
    listed = (listed_envelope or {}).get("result") or {}
    check("job_list reads the stored listing back",
          bool(listed_envelope) and listed_envelope.get("success") is True
          and any("Acceptance" in (job.get("title") or "") for job in listed.get("jobs", [])),
          str(listed_envelope)[:220])

    print("\n4. settings bridge reaches the engine")
    describe = _engine_json(["doctor", "--json"], settings={"default_sources": ["green"]})
    check("engine doctor reports the injected setting",
          (describe or {}).get("settings", {}).get("default_sources") == "green",
          f"doctor settings = {(describe or {}).get('settings')}")

    # The bridge is only worth having if a setting changes what the user sees.
    # `note_subfolder` is observable without a network call, so it is the one to
    # prove end to end: config.yaml value -> ctx.get_config -> handler ->
    # JOBREACH_SETTING_* -> jobreach.settings -> the note's directory.
    vault = work / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    ctx._settings["note_subfolder"] = "hermes-notes"
    note_envelope = _handler(ctx, "job_note", {
        "result": {"jobs": [{"title": "Acceptance UI Designer", "url": "https://example.invalid/a",
                             "company": "ACME", "source_platform": "indeed"}],
                   "query": {"keyword": "acceptance", "location": "tokyo", "sources": ["indeed"]},
                   "summary": {"total": 1, "new": 1}},
        "vault": str(vault),
    })
    note_path = ((note_envelope or {}).get("note") or "")
    check("configured note_subfolder decides where the note lands",
          isinstance(note_path, str) and f"{os.sep}hermes-notes{os.sep}" in note_path
          and Path(note_path).exists(),
          f"note = {note_path!r}")

    print("\n5. post_tool_call journal reaches job_status")
    record = (ctx.hooks.get("post_tool_call") or [None])[0]
    if record is None:
        check("journal records only our tools", False, "no post_tool_call hook was registered")
    else:
        try:
            record(tool_name="job_list", args={},
                   result=json.dumps({"success": True, "result": {"jobs": [1, 2]}}),
                   task_id="t", session_id="s", tool_call_id="c", duration_ms=5)
            record(tool_name="terminal", args={}, result="ignored", task_id="t", session_id="s",
                   tool_call_id="c2", duration_ms=1)
            journal = ctx.state.get("recent_tool_calls", default=[])
            check("journal records only our tools", len(journal) == 1,
                  f"{len(journal)} entries: {journal}")
            status = _run_handler(ctx, "job_status", {})
            check("job_status surfaces the journal",
                  bool(status) and status.get("recent_tool_calls") == journal,
                  f"job_status keys: {sorted(status) if status else None}")
        except Exception as exc:  # noqa: BLE001
            check("journal records only our tools", False, f"{type(exc).__name__}: {exc}")

    shutil.rmtree(work, ignore_errors=True)
    return _report()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _short(params: dict) -> str:
    text = json.dumps(params, default=str)
    return f"({text[:28]})" if len(text) <= 30 else f"({text[:27]}…)"


def _declared_tools(plugin_dir: Path) -> set[str]:
    """Read ``provides_tools`` out of plugin.yaml without importing Hermes."""
    text = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    names: set[str] = set()
    in_block = False
    for line in text.splitlines():
        if line.startswith("provides_tools:"):
            in_block = True
            continue
        if in_block:
            if line.startswith("  - "):
                names.add(line[4:].strip())
            elif line.strip() and not line.startswith(" "):
                break
    return names


def _handler(ctx: FakeContext, name: str, params: dict) -> dict | None:
    """Call a handler, decode its envelope, and swallow nothing silently."""
    try:
        raw = asyncio.run(asyncio.wait_for(ctx.tools[name]["handler"](params), timeout=180))
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"      (handler {name} raised {type(exc).__name__}: {exc})")
        return None


def _run_handler(ctx: FakeContext, name: str, params: dict) -> dict | None:
    """Call a handler and return its *payload*, whichever envelope shape it uses.

    ``job_search``/``job_ingest``/``job_list``/``job_note`` wrap the engine
    envelope under ``result``; ``job_status`` deliberately returns ``store`` and
    ``runtime`` at the top level. The harness accepts both rather than forcing a
    shape the schemas never promised.
    """
    envelope = _handler(ctx, name, params)
    if not isinstance(envelope, dict):
        return None
    if "result" in envelope:
        return envelope["result"]
    return {key: value for key, value in envelope.items() if key != "success"}


def _engine_json(args: list[str], *, settings: dict | None = None) -> dict | None:
    """Run the engine CLI directly with the settings bridge, for comparison."""
    # Import through the package under test, not a bare ``jobreach``: the latter
    # would only resolve if the plugin directory happened to be on sys.path.
    from jobreach_plugin_under_test.jobreach.runtime import run_engine

    proc = run_engine(args, timeout=120.0, settings=settings)
    try:
        return json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        print(f"      (engine stdout was not JSON: {proc.stdout[:120]!r}; stderr: {proc.stderr[:120]!r})")
        return None


def _report() -> int:
    print(f"\n{'=' * 70}\nPASSED: {len(PASSED)}   FAILED: {len(FAILED)}")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

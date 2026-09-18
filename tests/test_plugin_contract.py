"""Plugin contract: the parts Hermes itself depends on.

Three things must never drift, and this module is where they are pinned:

1. **manifest ↔ registration** — ``plugin.yaml``'s ``provides_tools`` must match
   what ``register(ctx)`` actually registers (``hermes plugins validate`` checks
   this too; the test keeps it honest without a CLI round-trip);
2. **schema ↔ handler** — every advertised tool has an implementation, and every
   implementation is advertised;
3. **the agent-facing result contract** — handlers always return a JSON string
   and never raise, because Hermes' tool dispatcher shows raw exceptions to the
   model as opaque failures.

The skill is validated here as well, since a malformed ``SKILL.md`` fails
silently: the skill simply never triggers.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = PROJECT_ROOT / "skills" / "job-search"

#: Directories Hermes recognises inside a skill, and files it forbids there.
ALLOWED_SKILL_SUBDIRS = {"references", "templates", "scripts", "assets"}
FORBIDDEN_SKILL_FILES = {"README.md", "CHANGELOG.md", "install.sh", ".env", ".env.example"}


# --------------------------------------------------------------------------- #
# Load the plugin the way Hermes does: __init__.py as a package whose search
# path is the plugin directory, so the relative imports resolve.
# --------------------------------------------------------------------------- #


class RecordingContext:
    """Stand-in for Hermes' PluginContext that records what gets registered.

    ``get_config``/``state`` mirror the documented facade (the real
    ``PluginState`` is a ``get``/``set`` store with no item access), so a
    handler or hook that reads a setting or journals a tool call behaves here
    the way it does inside Hermes instead of erroring the whole module.
    """

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.cli_commands: list[dict[str, Any]] = []
        self.hooks: list[tuple[str, Any]] = []
        self.settings: dict[str, Any] = {}
        #: Every key a handler asked ``get_config`` for, in order. Recording the
        #: reads is what turns "the key is in a tuple" into "the key is used".
        self.config_reads: list[str] = []
        self.state = _RecordingState()

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_skill(self, name: str, path: Path, **kwargs: Any) -> None:
        self.skills.append({"name": name, "path": Path(path), **kwargs})

    def register_command(self, name: str, handler: Any, **kwargs: Any) -> None:
        self.commands.append({"name": name, "handler": handler, **kwargs})

    def register_cli_command(self, name: str, **kwargs: Any) -> None:
        self.cli_commands.append({"name": name, **kwargs})

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks.append((name, callback))

    def get_config(self, key: str, default: Any = None) -> Any:
        self.config_reads.append(key)
        return self.settings.get(key, default)

    def has_capability(self, capability: str) -> bool:
        return False

    def dispatch_tool(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        return json.dumps({"success": True, "tool": tool_name})


class _RecordingState:
    """``ctx.state`` as the guide documents it: ``get(key, default)`` / ``set``."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


@pytest.fixture(scope="module")
def plugin():
    name = "jobreach_plugin_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        str(PROJECT_ROOT / "__init__.py"),
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def context(plugin) -> RecordingContext:
    """Register the plugin once; importing the package also loads its siblings."""
    ctx = RecordingContext()
    plugin.register(ctx)
    return ctx


@pytest.fixture(scope="module")
def handlers(plugin, context) -> dict[str, Any]:
    return plugin.tools.HANDLERS


@pytest.fixture(scope="module")
def schemas(plugin, context) -> list[dict[str, Any]]:
    return plugin.schemas.TOOL_SCHEMAS


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return yaml.safe_load((PROJECT_ROOT / "plugin.yaml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Manifest ↔ registration
# --------------------------------------------------------------------------- #


def test_manifest_has_the_required_fields(manifest: dict[str, Any]):
    for field in ("name", "version", "description"):
        assert manifest.get(field), f"plugin.yaml is missing {field}"


def test_every_version_declaration_agrees(manifest: dict[str, Any]):
    """One release has four places to bump, and three of them used to be forgotten.

    ``jobreach doctor`` reports the engine's own ``__version__`` while
    ``hermes plugins list`` reports the manifest's, so a skew does not stay
    hidden — it makes a single install claim two versions to the same user. The
    skill and the package metadata are read by other tools again, so they all
    have to move together.
    """
    import jobreach

    declared = str(manifest["version"])
    engine = jobreach.__version__
    assert engine == declared, f"jobreach.__version__ {engine!r} != plugin.yaml {declared!r}"

    project = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{declared}"' in project, (
        f"pyproject.toml's [project].version does not match plugin.yaml's {declared}"
    )

    frontmatter = skill_frontmatter()
    assert str(frontmatter["version"]) == declared, (
        f"SKILL.md version {frontmatter['version']!r} != plugin.yaml {declared!r}"
    )


def test_manifest_provides_tools_matches_registration(
    context: RecordingContext, manifest: dict[str, Any]
):
    assert sorted(manifest["provides_tools"]) == sorted(t["name"] for t in context.tools)


def test_only_plugin_context_methods_that_hermes_defines_are_used(plugin):
    """A typo in the ctx surface fails silently at runtime, so check the source."""
    hermes_plugins = Path.home() / ".hermes" / "hermes-agent" / "hermes_cli" / "plugins.py"
    if not hermes_plugins.exists():
        pytest.skip("Hermes source checkout not available")
    source = hermes_plugins.read_text(encoding="utf-8")
    used = re.findall(r"ctx\.(register_\w+)", (PROJECT_ROOT / "__init__.py").read_text("utf-8"))
    assert used, "expected the plugin to register something"
    for method in set(used):
        assert f"def {method}(" in source, f"PluginContext has no {method}()"


# --------------------------------------------------------------------------- #
# The rules the guide states explicitly
#
# These are the invariants a plugin author is *told* to keep: the manifest must
# describe what is registered, a declared setting must be read, and a hook
# callback must accept the full payload. Where a check needs Hermes' own source
# (the hook list, the capability registry) it skips cleanly when the checkout
# is absent, because the plugin must be testable on a machine without Hermes.
# --------------------------------------------------------------------------- #

#: Where a Hermes checkout keeps the two files whose contents define the contract.
HERMES_CHECKOUT = Path.home() / ".hermes" / "hermes-agent"
HERMES_PLUGINS_SOURCE = HERMES_CHECKOUT / "hermes_cli" / "plugins.py"
HERMES_CAPABILITY_SOURCES = (
    HERMES_CHECKOUT / "plugin_capabilities.py",
    HERMES_CHECKOUT / "hermes_cli" / "plugin_capabilities.py",
)


def accepts_var_kwargs(callback: Any) -> bool:
    """Mirror of ``hermes_cli/plugin_dev.py``'s check for ``**kwargs``."""
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters)


def hermes_source(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def hermes_valid_hooks() -> set[str] | None:
    """Hook names from the checkout's ``VALID_HOOKS`` set, or ``None`` when absent."""
    source = hermes_source(HERMES_PLUGINS_SOURCE)
    if source is None:
        return None
    block = re.search(r"VALID_HOOKS: Set\[str\] = \{(.*?)\n\}", source, re.S)
    if block is None:
        return None
    # Comments inside the set literal quote example values ("ok", "idle"), so
    # strip them before harvesting the names.
    return set(re.findall(r'"([\w.]+)"', re.sub(r"#.*", "", block.group(1)))) or None


def hermes_valid_capability_ids() -> set[str] | None:
    """Capability ids from the checkout, or ``None`` when it is not there.

    The spec names ``~/.hermes/hermes-agent/plugin_capabilities.py``; the
    current checkout keeps the same module under ``hermes_cli/``, so both are
    tried before giving up.
    """
    for path in HERMES_CAPABILITY_SOURCES:
        source = hermes_source(path)
        if source is None:
            continue
        block = re.search(r"_CAPABILITY_ROWS = \((.*?)\n\)", source, re.S)
        if block is None:
            return None
        return set(re.findall(r'^\s*\("([\w.]+)"', block.group(1), re.M)) or None
    return None


def test_every_config_schema_key_is_read_by_the_plugin(plugin, handlers, manifest: dict[str, Any]):
    """A decorated-but-unread setting is a lie to the user.

    ``plugin.yaml``'s ``config_schema`` is what Hermes shows the operator; if a
    key there never reaches ``ctx.get_config`` the GUI advertises a knob that
    does nothing.

    Checking membership in ``SETTINGS_KEYS`` is not enough — that tuple is a
    literal, so a handler that stopped calling ``ctx.get_config`` would keep the
    test green while the whole feature went dead. This drives a real handler
    through a context that records what it was asked for and asserts every
    declared key was actually requested.
    """
    declared = tuple(manifest.get("config_schema") or ())
    assert declared, "plugin.yaml declares no config_schema keys to check"

    ctx = RecordingContext()
    for handler in handlers.values():
        asyncio.run(handler({"save": False}, ctx=ctx))

    read = {key for key in ctx.config_reads}
    unread = sorted(set(declared) - read)
    assert unread == [], (
        f"plugin.yaml advertises {unread}, but calling every handler asked "
        f"ctx.get_config for only {sorted(read)}"
    )


def test_manifest_provides_hooks_matches_registration(
    context: RecordingContext, manifest: dict[str, Any]
):
    """A declared hook that is never registered is a silent observability hole."""
    declared = manifest.get("provides_hooks")
    if declared is None:
        pytest.fail(
            "plugin.yaml has no provides_hooks key; declare the hooks register(ctx) "
            "subscribes to (SPEC WS-E item 1)."
        )
    registered = [name for name, _callback in context.hooks]
    assert sorted(declared) == sorted(registered), (
        f"plugin.yaml declares provides_hooks={declared} but register(ctx) "
        f"registered {registered}"
    )


def test_the_post_tool_call_callback_accepts_the_full_payload(context: RecordingContext):
    """The guide: *"a callback with ``**kwargs`` receives the complete current payload"*."""
    callbacks = [callback for name, callback in context.hooks if name == "post_tool_call"]
    if not callbacks:
        pytest.fail(
            "SPEC WS-A item 6 has not landed: register(ctx) must call "
            "ctx.register_hook(\"post_tool_call\", record_tool_call) (and SPEC WS-E "
            "item 1 must declare provides_hooks: [post_tool_call] in plugin.yaml)."
        )
    for callback in callbacks:
        assert accepts_var_kwargs(callback), (
            f"{getattr(callback, '__name__', callback)!r} must accept **kwargs; the "
            "plugin doctor rejects a hook callback that does not"
        )
        parameters = inspect.signature(callback).parameters
        for field_name in ("tool_name", "args", "result", "task_id"):
            assert field_name in parameters, (
                f"the post_tool_call callback is missing the documented "
                f"{field_name!r} field"
            )


def test_every_registered_hook_is_a_hook_hermes_fires(context: RecordingContext):
    valid = hermes_valid_hooks()
    if valid is None:
        pytest.skip("Hermes source checkout not available")
    unknown = sorted({name for name, _ in context.hooks} - valid)
    assert unknown == [], f"register(ctx) subscribes to hooks Hermes does not fire: {unknown}"


def test_manifest_is_v2_and_declares_only_known_capabilities(manifest: dict[str, Any]):
    assert manifest.get("manifest_version") == 2, "manifest v2 is the documented shape"
    capabilities = manifest.get("capabilities")
    if not capabilities:
        return  # absent means "declares none", which is the honest default here
    valid = hermes_valid_capability_ids()
    if valid is None:
        pytest.skip("Hermes source checkout not available")
    unknown = sorted(set(capabilities) - valid)
    assert unknown == [], f"plugin.yaml declares capabilities Hermes cannot grant: {unknown}"


# --------------------------------------------------------------------------- #
# Schemas ↔ handlers
# --------------------------------------------------------------------------- #


def test_every_schema_has_a_handler(handlers: dict[str, Any], schemas: list[dict[str, Any]]):
    assert sorted(handlers) == sorted(schema["name"] for schema in schemas)


def test_the_expected_tool_surface_is_exposed(handlers: dict[str, Any]):
    assert set(handlers) == {
        "job_search", "job_ingest", "job_list", "job_note",
        "job_status", "job_setup", "job_cron",
    }


def test_schemas_are_well_formed(schemas: list[dict[str, Any]]):
    for schema in schemas:
        assert schema["name"].startswith("job_")
        assert len(schema["description"]) > 80, "the description is the model's only guide"
        parameters = schema["parameters"]
        assert parameters["type"] == "object"
        assert isinstance(parameters.get("properties"), dict)
        assert isinstance(parameters.get("required", []), list)


def test_every_handler_is_async_and_accepts_kwargs(handlers: dict[str, Any]):
    """The guide's handler contract: ``async def handler(args, **kwargs) -> str``."""
    for name, handler in handlers.items():
        assert inspect.iscoroutinefunction(handler), f"{name} must be async"
        parameters = inspect.signature(handler).parameters
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()), (
            f"{name} must accept **kwargs: Hermes passes extra context (the plugin "
            "context, settings) and a signature without **kwargs breaks on the next "
            "release instead of opting into additive data"
        )


def test_schema_descriptions_are_specific_enough_for_a_model(schemas: list[dict[str, Any]]):
    """The guide's *"vague description"* mistake: the description IS the trigger."""
    for schema in schemas:
        description = schema.get("description", "")
        assert isinstance(description, str) and len(description.strip()) >= 40, (
            f"{schema['name']} needs a description the model can act on, not {description!r}"
        )
        for property_name, spec in schema["parameters"].get("properties", {}).items():
            assert str(spec.get("description", "")).strip(), (
                f"{schema['name']}.{property_name} has no description; the model cannot "
                "fill in an argument it cannot read about"
            )


def test_nested_item_properties_are_described_too(schemas: list[dict[str, Any]]):
    """``job_ingest``'s ``jobs[]`` fields are declared inline, and the model reads them."""
    missing = []
    for schema in schemas:
        for property_name, spec in schema["parameters"].get("properties", {}).items():
            items = spec.get("items") if isinstance(spec, dict) else None
            for item_name, item_spec in (items or {}).get("properties", {}).items():
                if not str(item_spec.get("description", "")).strip():
                    missing.append(f"{schema['name']}.{property_name}[].{item_name}")
    assert missing == [], (
        "these nested item properties have no description: "
        + ", ".join(missing)
        + ". They are declared arguments the model fills in, so the guide's "
        "'vague description' rule covers them: give each a one-line description "
        "in schemas.py (the top-level properties all have one). If the team "
        "decides only top-level properties are in scope, narrow this test to "
        "schema['parameters']['properties'] — but the fix is five short lines."
    )


def test_every_schema_name_is_declared_in_the_manifest(
    schemas: list[dict[str, Any]], manifest: dict[str, Any]
):
    declared = set(manifest.get("provides_tools") or ())
    assert declared, "plugin.yaml declares no provides_tools"
    for schema in schemas:
        assert schema["name"] in declared, f"{schema['name']} is missing from provides_tools"


def test_every_tool_is_documented_in_the_skill(schemas: list[dict[str, Any]]):
    """A tool the skill never mentions is a tool the agent will not reach for."""
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for schema in schemas:
        assert schema["name"] in body, f"{schema['name']} is not documented in SKILL.md"


def test_registered_tools_are_async_and_share_one_toolset(context: RecordingContext):
    assert {tool["toolset"] for tool in context.tools} == {"job_reach"}
    assert all(tool["is_async"] is True for tool in context.tools)
    assert all(tool["handler"] for tool in context.tools)


# --------------------------------------------------------------------------- #
# Handler result contract
# --------------------------------------------------------------------------- #

#: Deliberately hostile inputs. ``job_cron`` is excluded on purpose: it shells
#: out to `hermes cron create` and must not touch the user's real scheduler in a
#: test — it is covered by the unit tests at the bottom of this module.
HOSTILE_INPUTS: dict[str, dict[str, Any]] = {
    "job_search": {"sources": ["monster"]},
    "job_ingest": {"jobs": []},
    "job_list": {"limit": 0},
    "job_note": {"result": "not-an-envelope"},
    "job_status": {},
    "job_setup": {"with_browser": False, "install_skill": False},
}


@pytest.mark.parametrize("name", sorted(HOSTILE_INPUTS))
def test_handlers_always_return_a_json_object(name: str, handlers: dict[str, Any], data_home: Path):
    raw = asyncio.run(handlers[name](HOSTILE_INPUTS[name]))
    assert isinstance(raw, str), f"{name} returned {type(raw)}"
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    assert "success" in payload, f"{name} must report success explicitly"


def test_ingest_rejects_an_empty_job_list(handlers: dict[str, Any], data_home: Path):
    payload = json.loads(asyncio.run(handlers["job_ingest"]({"jobs": []})))
    assert payload["success"] is False
    assert "jobs" in payload["error"]


def test_note_rejects_a_non_envelope(handlers: dict[str, Any], data_home: Path):
    payload = json.loads(asyncio.run(handlers["job_note"]({"result": {"nope": 1}})))
    assert payload["success"] is False
    assert "result" in payload["error"]


# --------------------------------------------------------------------------- #
# End-to-end through the real subprocess bridge
# --------------------------------------------------------------------------- #


def test_status_round_trips_through_the_engine(handlers: dict[str, Any], data_home: Path):
    """Exercises run_engine(): interpreter resolution, PYTHONPATH, JSON decoding."""
    payload = json.loads(asyncio.run(handlers["job_status"]({})))
    assert payload["success"] is True
    assert payload["store"]["total"] == 0
    assert payload["runtime"]["data_dir"] == str(data_home)


def test_ingest_then_list_through_the_engine(handlers: dict[str, Any], data_home: Path):
    """The loop the agent actually performs: ingest browser cards, read them back."""
    ingested = json.loads(
        asyncio.run(
            handlers["job_ingest"](
                {
                    "jobs": [
                        {"title": "UI Designer", "company": "Acme",
                         "url": "https://jp.indeed.com/viewjob?jk=abc123"},
                    ]
                }
            )
        )
    )
    assert ingested["success"] is True
    assert ingested["result"]["summary"]["new"] == 1

    listed = json.loads(asyncio.run(handlers["job_list"]({"text": "designer"})))
    assert listed["success"] is True
    assert [job["title"] for job in listed["result"]["jobs"]] == ["UI Designer"]


def test_note_round_trips_through_the_engine(
    handlers: dict[str, Any], data_home: Path, tmp_path: Path
):
    envelope = {
        "query": {"keyword": "designer", "location": "tokyo", "sources": ["indeed"]},
        "summary": {"total": 1, "new": 1, "by_platform": {"indeed": {"total": 1, "new": 1}}},
        "jobs": [
            {"title": "UI Designer", "company": "Acme", "url": "https://i.test/1",
             "location": "Tokyo", "source_platform": "indeed", "is_new": True}
        ],
    }
    payload = json.loads(
        asyncio.run(handlers["job_note"]({"result": envelope, "vault": str(tmp_path)}))
    )
    assert payload["success"] is True
    assert Path(payload["note"]).exists()


# --------------------------------------------------------------------------- #
# install.py units (no scheduler side effects)
# --------------------------------------------------------------------------- #


def test_install_skill_writes_into_the_hermes_tree(
    data_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from jobreach.install import install_skill

    path = install_skill()
    assert path.exists()
    assert path.parent.name == "job-reach"
    assert path.parent.parent.name == "productivity"
    assert path.read_text(encoding="utf-8").startswith("---")
    assert (path.parent / "references" / "indeed.md").exists()


def test_install_cron_reports_a_missing_hermes(
    data_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("jobreach.install.shutil.which", lambda _name: None)
    from jobreach.errors import ConfigError
    from jobreach.install import install_cron

    with pytest.raises(ConfigError, match="hermes"):
        install_cron()


def _fake_cron_create(refused: list[str], calls: list[list[str]]):
    """Stand in for `hermes cron create`, refusing the arguments in *refused*."""

    def run(command: list[str], **_kwargs: Any) -> Any:
        calls.append(command)
        argument = command[command.index("--monitor-script") + 1]
        if argument in refused:
            return subprocess.CompletedProcess(
                command, 1, "", "Script path must be relative to ~/.hermes/scripts/"
            )
        return subprocess.CompletedProcess(command, 0, "created", "")

    return run


def test_install_cron_hands_hermes_a_bare_monitor_script_name(
    data_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Hermes rejects an absolute ``--monitor-script``: it resolves the path
    under its own scripts directory. Passing the plugin's absolute path made
    `job_cron` fail outright on macOS, so the bare name is what must go out —
    and an older Hermes that insists on the absolute path must still work.
    """
    from jobreach.install import MONITOR_SCRIPT_NAME, install_cron

    monkeypatch.setattr("jobreach.install.shutil.which", lambda _name: "hermes")

    calls: list[list[str]] = []
    monkeypatch.setattr("jobreach.install.subprocess.run", _fake_cron_create([], calls))
    payload = install_cron()
    assert payload["created"] is True
    assert calls[0][calls[0].index("--monitor-script") + 1] == MONITOR_SCRIPT_NAME

    # An older Hermes refuses the bare name: the absolute path is tried second.
    calls = []
    monkeypatch.setattr(
        "jobreach.install.subprocess.run", _fake_cron_create([MONITOR_SCRIPT_NAME], calls)
    )
    payload = install_cron()
    assert payload["created"] is True
    assert len(calls) == 2
    assert calls[1][calls[1].index("--monitor-script") + 1].endswith(MONITOR_SCRIPT_NAME)
    assert Path(payload["monitor_script"]).exists()


def test_command_line_quoting_matches_the_platform(monkeypatch: pytest.MonkeyPatch):
    """A printed command must be pasteable into the shell the user is in."""
    from jobreach.install import command_line

    argv = ["hermes", "cron", "create", "0 9 * * *", "find jobs", "--name", "job-reach-monitor"]
    posix = command_line(argv)
    assert '"0 9 * * *"' in posix and '"find jobs"' in posix

    monkeypatch.setattr("jobreach.install.is_windows", lambda: True)
    windows = command_line(argv)
    assert windows.count('"') >= 4
    assert windows.startswith("hermes cron create")


def test_monitor_script_is_pinned_to_the_engine(data_home: Path):
    """The monitor is Python, not bash — that is what makes Windows work.

    Hermes runs a cron script by extension: ``.sh``/``.bash`` go through bash
    (absent on a stock Windows install), anything else through Hermes' own
    interpreter. So the generated file must be importable Python that names both
    the plugin directory and the engine interpreter.
    """
    from jobreach.install import MONITOR_SCRIPT_NAME, install_monitor_script

    path = install_monitor_script(["--keyword", "designer"])
    assert path.name == MONITOR_SCRIPT_NAME
    assert path.suffix == ".py"

    module = load_python_file(path)
    assert str(PROJECT_ROOT) == module.PLUGIN_DIR
    assert module.ENGINE  # the engine interpreter is baked in
    assert module.ARGS == ["--keyword", "designer"]
    if os.name != "nt":
        assert path.stat().st_mode & 0o111, "the monitor script must be executable"


def load_python_file(path: Path):
    """Import a generated script without running its ``__main__`` block."""
    spec = importlib.util.spec_from_file_location("jobreach_generated_monitor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_cron_only_bakes_boards_the_caller_actually_named(data_home: Path):
    """A scheduled run must be able to follow ``default_sources``.

    The cron job is installed once and then runs unattended, so a ``--source``
    literal baked into its monitor script outranks the setting forever: the user
    changes which boards they want watched and the job they installed last month
    keeps polling the old set. Omission is therefore the default, and the monitor
    resolves its own settings-backed default at run time.

    An explicit selection still has to be honoured — the caller asked for those
    boards on purpose, and the setting must not silently override them.
    """
    from jobreach.install import install_cron, install_monitor_script

    default_script = load_python_file(install_monitor_script(["--location", "tokyo"]))
    assert "--source" not in default_script.ARGS

    chosen_script = load_python_file(
        install_monitor_script(["--location", "tokyo", "--source", "green"])
    )
    assert chosen_script.ARGS[-2:] == ["--source", "green"]

    # And the callers must not reintroduce a literal: `job_cron` forwards the
    # caller's value verbatim, which is ``None`` when the model omitted it.
    import inspect

    signature = inspect.signature(install_cron)
    assert signature.parameters["sources"].default is None, (
        "install_cron must default to None so `monitor`'s settings-backed "
        "default decides; a literal default silently outranks default_sources"
    )


# --------------------------------------------------------------------------- #
# Bundled skill
# --------------------------------------------------------------------------- #


def skill_frontmatter() -> dict[str, Any]:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must open with a YAML frontmatter fence"
    _, frontmatter, _ = text.split("---", 2)
    return yaml.safe_load(frontmatter)


def test_skill_name_matches_its_directory():
    assert skill_frontmatter()["name"] == SKILL_DIR.name


def test_skill_description_fits_the_trigger_budget():
    """Hermes truncates the skills index at 57 chars, and that text IS the trigger."""
    description = skill_frontmatter()["description"]
    assert len(description) <= 60
    assert description.endswith(".")


def test_skill_declares_metadata_and_a_when_to_use_section():
    frontmatter = skill_frontmatter()
    assert frontmatter.get("version")
    assert frontmatter.get("license")
    assert frontmatter["metadata"]["hermes"]["tags"]
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "## When to Use" in body


def test_skill_directory_contains_only_allowed_entries():
    for path in SKILL_DIR.rglob("*"):
        assert path.name not in FORBIDDEN_SKILL_FILES, f"{path} is not allowed in a skill"
    for path in SKILL_DIR.iterdir():
        if path.is_dir():
            assert path.name in ALLOWED_SKILL_SUBDIRS


def test_skill_references_resolve():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    referenced = re.findall(r"references/([\w.-]+\.md)", body)
    assert referenced, "the skill should point at its board references"
    for name in referenced:
        assert (SKILL_DIR / "references" / name).exists(), name


def test_the_plugin_registers_the_skill(context: RecordingContext):
    (skill,) = context.skills
    assert skill["name"] == "job-search"
    assert skill["path"] == SKILL_DIR / "SKILL.md"


def test_the_plugin_offers_a_slash_command_and_a_cli_command(context: RecordingContext):
    assert [command["name"] for command in context.commands] == ["jobs"]
    assert [command["name"] for command in context.cli_commands] == ["job-reach"]

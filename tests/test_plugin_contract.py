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
import json
import re
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
    """Stand-in for Hermes' PluginContext that records what gets registered."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.cli_commands: list[dict[str, Any]] = []
        self.hooks: list[tuple[str, Any]] = []

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


def test_manifest_declares_no_hooks(context: RecordingContext, manifest: dict[str, Any]):
    assert manifest["provides_hooks"] == []
    assert context.hooks == []


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
    assert (path.parent / "references" / "indeed-browser.md").exists()


def test_install_cron_reports_a_missing_hermes(
    data_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("jobreach.install.shutil.which", lambda _name: None)
    from jobreach.errors import ConfigError
    from jobreach.install import install_cron

    with pytest.raises(ConfigError, match="hermes"):
        install_cron()


def test_monitor_script_is_pinned_to_the_engine(data_home: Path):
    from jobreach.install import MONITOR_SCRIPT_NAME, install_monitor_script

    path = install_monitor_script(["--keyword", "designer"])
    assert path.name == MONITOR_SCRIPT_NAME
    body = path.read_text(encoding="utf-8")
    assert "-m jobreach monitor" in body
    assert "--keyword designer" in body
    assert str(PROJECT_ROOT) in body
    assert path.stat().st_mode & 0o111, "the monitor script must be executable"


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
    assert referenced, "the skill should point at its browser reference"
    for name in referenced:
        assert (SKILL_DIR / "references" / name).exists(), name


def test_the_plugin_registers_the_skill(context: RecordingContext):
    (skill,) = context.skills
    assert skill["name"] == "job-search"
    assert skill["path"] == SKILL_DIR / "SKILL.md"


def test_the_plugin_offers_a_slash_command_and_a_cli_command(context: RecordingContext):
    assert [command["name"] for command in context.commands] == ["jobs"]
    assert [command["name"] for command in context.cli_commands] == ["job-reach"]

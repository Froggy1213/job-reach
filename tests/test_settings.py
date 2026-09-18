"""The settings bridge: ``JOBREACH_SETTING_*`` in, typed values out.

Why these tests exist: the engine can never call ``ctx.get_config()`` — it is a
child process — so every user setting crosses exactly one process boundary, as
an environment variable. That makes this module the single place where "the
user configured X" can be silently lost: a blank field that wipes out a default,
a list that arrives as a string and never gets split, an int that raises deep
inside a search instead of falling back. Each is an ordinary typing accident, so
each is pinned here.

The last group pins the *negative* space: ``engine_env()`` / ``run_engine()``
with no settings must stay byte-for-byte what the CLI, the cron monitor and the
setup smoke test already depend on, so the captured argv **and** environment are
compared whole rather than spot-checked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jobreach import runtime, settings
from jobreach.config import DEFAULT_SOURCES, NOTE_SUBFOLDER, plugin_dir


@pytest.fixture(autouse=True)
def no_ambient_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited ``JOBREACH_SETTING_*`` may leak into a test.

    The variables are also a supported manual override (a cron script or a
    direct CLI run exports them), so the developer running this suite may well
    have one set. A settings test that only passes on a clean shell would be
    worse than no test.
    """
    for name in list(os.environ):
        if name.startswith(settings.SETTING_PREFIX):
            monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# The prefix
# --------------------------------------------------------------------------- #


def test_env_name_prefixes_and_uppercases():
    assert settings.env_name("default_sources") == "JOBREACH_SETTING_DEFAULT_SOURCES"
    assert settings.SETTING_PREFIX == "JOBREACH_SETTING_"


def test_env_name_normalises_a_hyphenated_key():
    """A POSIX shell cannot export ``FOO-BAR``; the bridge must not emit one."""
    assert settings.env_name("default-keyword") == "JOBREACH_SETTING_DEFAULT_KEYWORD"


def test_config_schema_keys_match_the_bridge():
    """``describe`` reports these, so a typo here hides a real setting."""
    assert settings.CONFIG_SCHEMA_KEYS == (
        "default_keyword",
        "default_sources",
        "note_subfolder",
        "max_results",
    )


# --------------------------------------------------------------------------- #
# Reading one value
# --------------------------------------------------------------------------- #


def test_get_setting_reads_the_prefixed_variable():
    env = {"JOBREACH_SETTING_DEFAULT_KEYWORD": "UI Designer"}
    assert settings.get_setting("default_keyword", env=env) == "UI Designer"


def test_get_setting_ignores_an_unprefixed_variable():
    assert settings.get_setting("default_keyword", "fallback", {"DEFAULT_KEYWORD": "x"}) == (
        "fallback"
    )


def test_get_setting_returns_the_default_when_unset():
    """An empty mapping, not an empty string, is the "all defaults" case."""
    assert settings.get_setting("default_keyword", "fallback", {}) == "fallback"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_get_setting_treats_blank_as_unset(blank: str):
    """A cleared settings field must mean "plugin default", not "override with nothing"."""
    env = {"JOBREACH_SETTING_DEFAULT_KEYWORD": blank}
    assert settings.get_setting("default_keyword", "fallback", env) == "fallback"


def test_get_setting_trims_surrounding_whitespace():
    env = {"JOBREACH_SETTING_DEFAULT_KEYWORD": "  UI Designer\n"}
    assert settings.get_setting("default_keyword", env=env) == "UI Designer"


def test_get_setting_defaults_to_the_real_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBREACH_SETTING_NOTE_SUBFOLDER", "job-searches/2026")
    assert settings.get_setting("note_subfolder") == "job-searches/2026"


# --------------------------------------------------------------------------- #
# Lists
# --------------------------------------------------------------------------- #


def test_get_setting_list_splits_on_commas_and_trims():
    env = {"JOBREACH_SETTING_DEFAULT_SOURCES": " wantedly , linkedin "}
    assert settings.get_setting_list("default_sources", env=env) == ("wantedly", "linkedin")


def test_get_setting_list_drops_empty_entries():
    """``"a,,b,"`` is what a half-edited form looks like, not a board named ""."""
    env = {"JOBREACH_SETTING_DEFAULT_SOURCES": "wantedly,,linkedin,"}
    assert settings.get_setting_list("default_sources", env=env) == ("wantedly", "linkedin")


def test_get_setting_list_keeps_a_single_value():
    env = {"JOBREACH_SETTING_DEFAULT_SOURCES": "wantedly"}
    assert settings.get_setting_list("default_sources", env=env) == ("wantedly",)


def test_get_setting_list_falls_back_when_unset():
    assert settings.get_setting_list("default_sources", ("a", "b"), {}) == ("a", "b")


@pytest.mark.parametrize("value", ["", "   ", ",", " , "])
def test_get_setting_list_falls_back_when_nothing_survives(value: str):
    """Separators with no entries are a blank field, and must not select nothing.

    The alternative — returning ``()`` — turns into "no valid source selected"
    from ``Sources.parse``, i.e. a traceback-shaped failure for a form the user
    believed they had cleared.
    """
    env = {"JOBREACH_SETTING_DEFAULT_SOURCES": value}
    assert settings.get_setting_list("default_sources", ("a",), env) == ("a",)


def test_get_setting_list_copies_a_list_default():
    """The default is copied into a tuple, so callers cannot mutate config state."""
    given = ["a", "b"]
    result = settings.get_setting_list("default_sources", given, {})
    assert isinstance(result, tuple)
    given.append("c")
    assert result == ("a", "b")


# --------------------------------------------------------------------------- #
# Integers
# --------------------------------------------------------------------------- #


def test_get_setting_int_parses_a_number():
    env = {"JOBREACH_SETTING_MAX_RESULTS": "50"}
    assert settings.get_setting_int("max_results", 25, env) == 50


def test_get_setting_int_tolerates_padding():
    env = {"JOBREACH_SETTING_MAX_RESULTS": " 50 "}
    assert settings.get_setting_int("max_results", 25, env) == 50


@pytest.mark.parametrize("junk", ["many", "25.5", "twenty-five", "1e3", "0x10"])
def test_get_setting_int_rejects_junk(junk: str):
    env = {"JOBREACH_SETTING_MAX_RESULTS": junk}
    assert settings.get_setting_int("max_results", 25, env) == 25


@pytest.mark.parametrize("value", ["0", "-1", "-100"])
def test_get_setting_int_rejects_non_positive(value: str):
    """Every integer setting is a count or a cap, where 0 and negatives mean "unset"."""
    env = {"JOBREACH_SETTING_MAX_RESULTS": value}
    assert settings.get_setting_int("max_results", 25, env) == 25


def test_get_setting_int_falls_back_when_unset():
    assert settings.get_setting_int("max_results", 25, {}) == 25


def test_get_setting_int_default_is_zero():
    assert settings.get_setting_int("max_results") == 0


# --------------------------------------------------------------------------- #
# The two named settings
# --------------------------------------------------------------------------- #


def test_default_sources_fall_back_to_config():
    assert settings.default_sources({}) == DEFAULT_SOURCES


def test_default_sources_read_the_setting():
    env = {"JOBREACH_SETTING_DEFAULT_SOURCES": "indeed,green"}
    assert settings.default_sources(env) == ("indeed", "green")


def test_default_sources_are_not_validated_here():
    """``Sources.parse`` owns the board names; a bad value must reach its message.

    Validating here would duplicate ``config._VALID_SOURCES`` and let the two
    drift, and argparse would surface a bad default as a traceback.
    """
    env = {"JOBREACH_SETTING_DEFAULT_SOURCES": "monster"}
    assert settings.default_sources(env) == ("monster",)


def test_note_subfolder_falls_back_to_config():
    assert settings.note_subfolder({}) == NOTE_SUBFOLDER


def test_note_subfolder_reads_the_setting():
    env = {"JOBREACH_SETTING_NOTE_SUBFOLDER": "job-searches/2026-spring"}
    assert settings.note_subfolder(env) == "job-searches/2026-spring"


# --------------------------------------------------------------------------- #
# Writing the bridge (plugin process → child environment)
# --------------------------------------------------------------------------- #


def test_apply_default_settings_writes_prefixed_names():
    env: dict[str, str] = {}
    written = settings.apply_default_settings(env, {"default_keyword": "UI Designer"})
    assert env == {"JOBREACH_SETTING_DEFAULT_KEYWORD": "UI Designer"}
    assert written == ["default_keyword"]


def test_apply_default_settings_drops_none_and_blank():
    """``ctx.get_config`` returns ``None`` for an unset key; that is not a value."""
    env: dict[str, str] = {}
    written = settings.apply_default_settings(
        env,
        {"default_keyword": None, "note_subfolder": "   ", "max_results": 0},
    )
    assert written == ["max_results"], "0 is a value (the readers decide it means unset)"
    assert env == {"JOBREACH_SETTING_MAX_RESULTS": "0"}


def test_apply_default_settings_joins_lists():
    env: dict[str, str] = {}
    written = settings.apply_default_settings(
        env, {"default_sources": ["wantedly", " linkedin ", "", None]}
    )
    assert env["JOBREACH_SETTING_DEFAULT_SOURCES"] == "wantedly,linkedin"
    assert written == ["default_sources"]


def test_apply_default_settings_joins_tuples():
    """``ctx.get_config`` may hand back a YAML list *or* a tuple from our own code."""
    env: dict[str, str] = {}
    settings.apply_default_settings(env, {"default_sources": ("indeed", "green")})
    assert env["JOBREACH_SETTING_DEFAULT_SOURCES"] == "indeed,green"


def test_apply_default_settings_stringifies_scalars():
    env: dict[str, str] = {}
    settings.apply_default_settings(env, {"max_results": 50})
    assert env == {"JOBREACH_SETTING_MAX_RESULTS": "50"}


def test_apply_default_settings_keeps_an_existing_override_untouched():
    """The env var is also a manual override; applying settings must not clear others."""
    env = {"JOBREACH_SETTING_DEFAULT_KEYWORD": "exported-by-the-operator"}
    settings.apply_default_settings(env, {"max_results": 50})
    assert env["JOBREACH_SETTING_DEFAULT_KEYWORD"] == "exported-by-the-operator"


def test_apply_default_settings_overwrites_a_key_it_is_given():
    env = {"JOBREACH_SETTING_MAX_RESULTS": "5"}
    settings.apply_default_settings(env, {"max_results": 50})
    assert env["JOBREACH_SETTING_MAX_RESULTS"] == "50"


def test_apply_default_settings_with_nothing_writes_nothing():
    env = {"PATH": "/usr/bin"}
    assert settings.apply_default_settings(env, {}) == []
    assert env == {"PATH": "/usr/bin"}


def test_settings_round_trip_through_the_bridge():
    """Write like the plugin process, read like the engine: one contract, both ends."""
    env: dict[str, str] = {}
    settings.apply_default_settings(
        env,
        {
            "default_keyword": "UI Designer",
            "default_sources": ["wantedly", "linkedin"],
            "note_subfolder": "job-searches/2026",
            "max_results": 50,
        },
    )
    assert settings.get_setting("default_keyword", env=env) == "UI Designer"
    assert settings.default_sources(env) == ("wantedly", "linkedin")
    assert settings.note_subfolder(env) == "job-searches/2026"
    assert settings.get_setting_int("max_results", 25, env) == 50


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def test_describe_omits_unset_keys():
    """An empty dict is the answer to "which settings did Hermes hand me?": none."""
    assert settings.describe({}) == {}


def test_describe_omits_blank_values():
    env = {"JOBREACH_SETTING_DEFAULT_KEYWORD": "   "}
    assert settings.describe(env) == {}


def test_describe_reports_only_the_schema_keys_in_manifest_order():
    env = {
        "JOBREACH_SETTING_MAX_RESULTS": "50",
        "JOBREACH_SETTING_DEFAULT_KEYWORD": " UI Designer ",
        "JOBREACH_SETTING_SOMETHING_ELSE": "not-a-setting",
    }
    assert list(settings.describe(env)) == ["default_keyword", "max_results"]
    assert settings.describe(env)["default_keyword"] == "UI Designer"


def test_describe_reads_the_real_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBREACH_SETTING_DEFAULT_KEYWORD", "designer")
    assert settings.describe() == {"default_keyword": "designer"}


def test_config_does_not_import_the_bridge():
    """``settings`` may import ``config``; the reverse would be an import cycle.

    Checked in a fresh interpreter on purpose: by the time this test runs, every
    other module in the process has already imported ``settings``, so an
    in-process ``sys.modules`` check could never fail.
    """
    repo_root = Path(__file__).resolve().parent.parent
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, jobreach.config; print('jobreach.settings' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False"


# --------------------------------------------------------------------------- #
# The child environment
# --------------------------------------------------------------------------- #


def test_engine_env_with_no_settings_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, data_home: Path
):
    """The one rule the refactor must not break: no settings argument, no change.

    The expected environment is reconstructed independently of ``engine_env``
    rather than compared against it, so dropping or altering one of the three
    things it injects fails here.
    """
    monkeypatch.setenv("VIRTUAL_ENV", "/hermes/runtime/venv")
    env = runtime.engine_env()

    expected = dict(os.environ)
    expected.pop("VIRTUAL_ENV", None)
    expected["PYTHONPATH"] = str(plugin_dir())
    expected["JOBREACH_PLUGIN_DIR"] = str(plugin_dir())
    assert env == expected
    assert settings.describe(env) == {}


def test_engine_env_mirrors_settings(monkeypatch: pytest.MonkeyPatch, data_home: Path):
    monkeypatch.setenv("VIRTUAL_ENV", "/hermes/runtime/venv")
    env = runtime.engine_env({"default_keyword": "UI Designer", "max_results": 50})
    assert env["JOBREACH_SETTING_DEFAULT_KEYWORD"] == "UI Designer"
    assert env["JOBREACH_SETTING_MAX_RESULTS"] == "50"
    assert env["PYTHONPATH"] == str(plugin_dir())
    assert "VIRTUAL_ENV" not in env


def test_engine_env_with_an_empty_mapping_matches_no_settings(data_home: Path):
    """``settings={}`` is what a context without ``get_config`` produces."""
    assert runtime.engine_env({}) == runtime.engine_env()


def test_engine_env_keeps_an_operator_exported_setting(
    monkeypatch: pytest.MonkeyPatch, data_home: Path
):
    """Direct CLI and cron runs export the variables themselves; do not wipe them."""
    monkeypatch.setenv("JOBREACH_SETTING_DEFAULT_KEYWORD", "exported-by-the-operator")
    env = runtime.engine_env({"max_results": 50})
    assert env["JOBREACH_SETTING_DEFAULT_KEYWORD"] == "exported-by-the-operator"


class _RecordingRunner:
    """Stand-in for :func:`jobreach.proc.run_captured` that keeps its arguments."""

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.timeout: float | None = None
        self.stdin: str | None = None
        self.cwd: Any = None
        self.env: dict[str, str] = {}
        self.calls = 0

    def __call__(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        stdin: str | None = None,
        cwd: Any = None,
        env: Any = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        self.argv = list(argv)
        self.timeout = timeout
        self.stdin = stdin
        self.cwd = cwd
        self.env = dict(env or {})
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")


def test_run_engine_without_settings_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, data_home: Path
):
    """argv, cwd, timeout and the whole environment, exactly as before settings."""
    runner = _RecordingRunner()
    monkeypatch.setattr(runtime, "run_captured", runner)
    monkeypatch.setenv("VIRTUAL_ENV", "/hermes/runtime/venv")

    result = runtime.run_engine(["doctor", "--json"], timeout=12.5, stdin="payload")

    assert result.returncode == 0
    assert runner.argv == [*runtime.engine_argv(), "doctor", "--json"]
    assert runner.timeout == 12.5
    assert runner.stdin == "payload"
    assert runner.cwd == plugin_dir()

    expected = dict(os.environ)
    expected.pop("VIRTUAL_ENV", None)
    expected["PYTHONPATH"] = str(plugin_dir())
    expected["JOBREACH_PLUGIN_DIR"] = str(plugin_dir())
    assert runner.env == expected
    assert not any(name.startswith(settings.SETTING_PREFIX) for name in runner.env)


def test_run_engine_forwards_settings_to_the_child(
    monkeypatch: pytest.MonkeyPatch, data_home: Path
):
    runner = _RecordingRunner()
    monkeypatch.setattr(runtime, "run_captured", runner)

    runtime.run_engine(["doctor"], settings={"default_sources": ["wantedly"]})

    assert runner.argv == [*runtime.engine_argv(), "doctor"], "settings must not touch argv"
    assert runner.env["JOBREACH_SETTING_DEFAULT_SOURCES"] == "wantedly"


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_diagnostics_reports_the_active_settings(
    monkeypatch: pytest.MonkeyPatch, data_home: Path
):
    """``jobreach doctor`` must answer "which settings did Hermes actually hand me?"."""
    monkeypatch.setattr(
        runtime,
        "backend_report",
        lambda: {
            "preference": "",
            "scrapling": {"ready": False, "python": None, "source": "", "version": "", "problem": ""},
            "playwright": {"ready": False, "python": "", "problem": ""},
            "selected": None,
            "problem": "stubbed: no backend in this test",
            "hint": "",
        },
    )
    monkeypatch.setenv("JOBREACH_SETTING_DEFAULT_SOURCES", "indeed,green")

    payload = runtime.diagnostics()

    assert payload["settings"] == {"default_sources": "indeed,green"}
    assert payload["data_dir"] == str(data_home)


def test_diagnostics_settings_are_empty_by_default(data_home: Path):
    assert runtime.diagnostics()["settings"] == {}

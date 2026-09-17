"""Configuration: paths, board selection, vault discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobreach.config import (
    DEFAULT_SOURCES,
    PLUGIN_ID,
    Sources,
    default_db_path,
    find_vault,
    hermes_home,
    jobreach_home,
    plugin_dir,
)


def test_default_sources_are_the_design_feed():
    """Indeed joined the scrapers, but the *default* feed is unchanged.

    A no-argument search is the project's original design-in-Tokyo feed; Indeed
    covers all of Japan's job market and is a deliberate choice, not a default.
    """
    assert DEFAULT_SOURCES == ("wantedly", "mynavi2027", "linkedin")


def test_data_home_follows_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JOBREACH_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert jobreach_home() == tmp_path / "plugin-data" / PLUGIN_ID


def test_data_home_honours_explicit_override(data_home: Path):
    assert jobreach_home() == data_home
    assert data_home.is_dir()


def test_db_path_override_wins(data_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBREACH_DB", str(data_home / "custom.db"))
    assert default_db_path() == data_home / "custom.db"


def test_db_path_defaults_inside_the_data_home(data_home: Path):
    assert default_db_path() == data_home / "jobs.db"


def test_plugin_dir_is_the_project_root():
    assert (plugin_dir() / "plugin.yaml").exists()


def test_hermes_home_defaults_to_dot_hermes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert hermes_home() == Path.home() / ".hermes"


def test_find_vault_prefers_the_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path))
    assert find_vault() == tmp_path


def test_find_vault_returns_none_when_nothing_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    monkeypatch.setattr("jobreach.config.Path.home", lambda: tmp_path)
    assert find_vault() is None


# --- Sources ---------------------------------------------------------------


def test_sources_default():
    assert Sources.parse().as_list() == list(DEFAULT_SOURCES)


def test_sources_parses_a_comma_string():
    assert Sources.parse("wantedly, linkedin").as_list() == ["wantedly", "linkedin"]


def test_sources_parses_a_list_and_deduplicates():
    assert Sources.parse(["wantedly", "wantedly"]).as_list() == ["wantedly"]


def test_all_expands_to_every_board():
    assert Sources.parse("all").as_list() == [
        "wantedly", "mynavi2027", "linkedin", "indeed"
    ]


def test_all_boards_are_scrapable():
    """``Sources.scraped`` used to drop Indeed (ingest-only); now it keeps it."""
    assert Sources.parse("all").scraped == ("wantedly", "mynavi2027", "linkedin", "indeed")


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError, match="unknown source"):
        Sources.parse("monster")


def test_empty_selection_is_rejected():
    with pytest.raises(ValueError, match="no valid source"):
        Sources.parse(",")


def test_sources_supports_membership():
    sources = Sources.parse("wantedly")
    assert "wantedly" in sources
    assert "linkedin" not in sources
    assert len(sources) == 1

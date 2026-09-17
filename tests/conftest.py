"""Shared test fixtures.

The plugin directory is put on ``sys.path`` so tests import ``jobreach`` the
same way the subprocess-based tool layer does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test's runtime state from the developer's real ~/.hermes.

    Both roots are redirected: ``HERMES_HOME`` (so skills/scripts/cron writes
    land in the sandbox) and ``JOBREACH_HOME`` (the plugin's own state).
    """
    hermes = tmp_path / "hermes-home"
    home = tmp_path / "job-reach-data"
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("JOBREACH_HOME", str(home))
    monkeypatch.delenv("JOBREACH_DB", raising=False)
    monkeypatch.delenv("JOBREACH_PYTHON", raising=False)
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    return home


@pytest.fixture()
def db_path(data_home: Path) -> Path:
    """Path to a throwaway SQLite database."""
    return data_home / "jobs.db"

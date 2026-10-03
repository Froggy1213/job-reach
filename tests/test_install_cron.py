"""Tests for install_cron idempotency and reuse of existing jobs."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from jobreach.config import plugin_dir
from jobreach.install import install_cron


@pytest.fixture()
def fake_hermes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake `hermes` binary on PATH that records its argv to a log file."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_file = bin_dir / "hermes_calls.log"

    script = bin_dir / "hermes"
    script.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

log_path = Path({str(log_file)!r})
with log_path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")

# Stand-in for hermes cron create
print("created")
sys.exit(0)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log_file


def _read_calls(log_file: Path) -> list[list[str]]:
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jobs(data_home: Path, jobs: list[dict[str, Any]]) -> Path:
    cron_dir = Path(os.environ["HERMES_HOME"]) / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    jobs_file = cron_dir / "jobs.json"
    jobs_file.write_text(json.dumps({"jobs": jobs, "updated_at": 1000}), encoding="utf-8")
    return jobs_file


def test_matching_existing_job_is_reused_without_invoking_create(
    data_home: Path, fake_hermes: Path
):
    """An existing job with the same configuration must be reused and never duplicated."""
    _write_jobs(
        data_home,
        [
            {
                "id": "job-42",
                "name": "job-reach-monitor",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "monitor_script": "job-reach-monitor.py",
                "workdir": str(plugin_dir()),
                "deliver": None,
                "enabled": True,
            }
        ],
    )

    payload = install_cron()

    assert payload["created"] is False
    assert payload["reused"] is True
    assert payload["id"] == "job-42"
    assert "cron edit job-42" in payload["command"]
    assert "already installed and up to date; nothing created" in payload["message"]
    assert payload["existing"]["enabled"] is True
    assert payload["existing"]["workdir"] == str(plugin_dir())
    assert _read_calls(fake_hermes) == [], "hermes cron create must never be invoked"


def test_differing_schedule_carries_schedule_flag_in_edit_command(
    data_home: Path, fake_hermes: Path
):
    """When the schedule differs, the edit command must include the --schedule flag."""
    _write_jobs(
        data_home,
        [
            {
                "id": "job-99",
                "name": "job-reach-monitor",
                "schedule": {"kind": "cron", "expr": "0 12 * * *"},
                "monitor_script": "job-reach-monitor.py",
                "workdir": str(plugin_dir()),
                "enabled": True,
            }
        ],
    )

    payload = install_cron(schedule="0 9 * * *")

    assert payload["created"] is False
    assert payload["reused"] is True
    assert "--schedule" in payload["command"]
    assert "0 9 * * *" in payload["command"]
    assert "--schedule" in payload["hint"]
    assert "differs" in payload["message"]
    assert _read_calls(fake_hermes) == []


def test_differing_deliver_and_workdir_included_in_edit_command(
    data_home: Path, fake_hermes: Path, tmp_path: Path
):
    """Differing workdir or deliver targets must be flagged for edit."""
    existing_dir = tmp_path / "valid-workdir"
    existing_dir.mkdir()
    _write_jobs(
        data_home,
        [
            {
                "id": "job-101",
                "name": "job-reach-monitor",
                "schedule": "0 9 * * *",
                "monitor_script": "other-monitor.py",
                "workdir": str(existing_dir),
                "deliver": "telegram",
            }
        ],
    )

    payload = install_cron(schedule="0 9 * * *", deliver="discord")

    assert payload["reused"] is True
    assert "--monitor-script" in payload["command"]
    assert "job-reach-monitor.py" in payload["command"]
    assert "--workdir" in payload["command"]
    assert str(plugin_dir()) in payload["command"]
    assert "--deliver" in payload["command"]
    assert "discord" in payload["command"]


def test_stale_workdir_is_flagged_in_payload_and_message(
    data_home: Path, fake_hermes: Path, tmp_path: Path
):
    """A job pointing to a vanished directory (like deleted worktrees) must be marked stale."""
    vanished_dir = tmp_path / "deleted-worktree"
    # Ensure it definitely does not exist
    if vanished_dir.exists():
        vanished_dir.rmdir()

    _write_jobs(
        data_home,
        [
            {
                "id": "job-stale",
                "name": "job-reach-monitor",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "monitor_script": "job-reach-monitor.py",
                "workdir": str(vanished_dir),
            }
        ],
    )

    payload = install_cron()

    assert payload["reused"] is True
    assert payload.get("stale") is True
    assert "stale workdir" in payload["message"].lower() or "no longer exists" in payload["message"]
    assert str(vanished_dir) in payload["message"]
    assert "--workdir" in payload["command"]
    assert _read_calls(fake_hermes) == []


def test_missing_or_malformed_jobs_json_falls_through_to_create(
    data_home: Path, fake_hermes: Path
):
    """If jobs.json is missing or corrupted, install_cron must still create the job."""
    # 1. Missing jobs.json
    payload = install_cron()
    assert payload["created"] is True
    calls = _read_calls(fake_hermes)
    assert len(calls) == 1
    assert calls[0][:2] == ["cron", "create"]

    # 2. Corrupted JSON file
    jobs_file = Path(os.environ["HERMES_HOME"]) / "cron" / "jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_file.write_text("not json at all", encoding="utf-8")
    payload2 = install_cron()
    assert payload2["created"] is True
    calls = _read_calls(fake_hermes)
    assert len(calls) == 2

    # 3. Not our shape (jobs is not a list)
    jobs_file.write_text('{"jobs": "invalid"}', encoding="utf-8")
    payload3 = install_cron()
    assert payload3["created"] is True
    calls = _read_calls(fake_hermes)
    assert len(calls) == 3

"""The CLI contract: exit codes, JSON on stdout, logs on stderr.

These run the real argparse layer in-process (fast), rather than spawning
subprocesses, so they assert the interface the Hermes tools depend on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobreach.cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, main


class FakeStdin:
    """Stand-in for a piped stdin (or a TTY) so the CLI can be driven in-process."""

    def __init__(self, data: str = "", tty: bool = False) -> None:
        self._data = data
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def read(self) -> str:
        return self._data


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_doctor_json_reports_paths(data_home: Path, capsys: pytest.CaptureFixture[str]):
    code, out, _ = run(capsys, "doctor", "--json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["plugin_version"]
    assert payload["data_dir"] == str(data_home)
    assert "indeed" in payload["boards"]
    assert payload["boards"]["indeed"]["ready"] is True


def test_stats_on_an_empty_database(data_home: Path, capsys: pytest.CaptureFixture[str]):
    code, out, _ = run(capsys, "stats", "--json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["total"] == 0
    assert payload["recent_runs"] == []


def test_ingest_reads_stdin_and_emits_json(
    data_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "sys.stdin", FakeStdin('[{"title":"UI Designer","url":"https://i.test/1"}]')
    )
    code, out, _ = run(capsys, "ingest", "--json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["summary"]["new"] == 1


def test_ingest_without_stdin_is_a_usage_error(
    data_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=True))
    code, _, err = run(capsys, "ingest", "--json")
    assert code == EXIT_USAGE
    assert "no JSON on stdin" in err


def test_ingest_rejects_broken_json(
    data_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("sys.stdin", FakeStdin("{oops"))
    code, _, err = run(capsys, "ingest", "--json")
    assert code == EXIT_USAGE
    assert "invalid JSON" in err


def test_list_reports_an_empty_store(
    data_home: Path, capsys: pytest.CaptureFixture[str]
):
    code, out, _ = run(capsys, "list")
    assert code == EXIT_OK
    assert "No stored listings" in out


def test_unknown_source_is_a_usage_error(
    data_home: Path, capsys: pytest.CaptureFixture[str]
):
    code, _, err = run(capsys, "search", "--source", "monster", "--json")
    assert code == EXIT_USAGE
    assert "unknown source" in err


def test_source_parse_happens_before_any_browser_launch(
    data_home: Path, capsys: pytest.CaptureFixture[str]
):
    """A malformed --source must fail fast, not after starting Playwright."""
    code, _, err = run(capsys, "search", "--source", "", "--json")
    assert code == EXIT_USAGE
    assert "no valid source" in err


def test_note_requires_an_envelope(
    data_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=True))
    code, _, err = run(capsys, "note")
    assert code == EXIT_USAGE
    assert "no search result" in err


def test_setup_without_uv_fails_with_a_hint(
    data_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("jobreach.runtime.uv_path", lambda: None)
    code, out, _ = run(capsys, "setup", "--no-browser")
    assert code == EXIT_FAILURE
    assert "uv was not found" in out


def test_install_skill_writes_into_a_fake_hermes_home(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("JOBREACH_HOME", str(tmp_path / "data"))
    code, out, _ = run(capsys, "install-skill", "--json")
    assert code == EXIT_OK
    skill = Path(json.loads(out)["skill"])
    assert skill.exists()
    assert skill.parent.name == "job-reach"
    assert skill.read_text(encoding="utf-8").startswith("---")


def test_version_flag(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "jobreach" in capsys.readouterr().out

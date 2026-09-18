"""The CLI contract: exit codes, JSON on stdout, logs on stderr.

These run the real argparse layer in-process (fast), rather than spawning
subprocesses, so they assert the interface the Hermes tools depend on.

The last section covers the *settings* contract: a flag that Hermes can answer
takes its argparse default from ``jobreach.settings`` (the
``JOBREACH_SETTING_*`` bridge), and an explicit flag always wins. Those tests
drive the parser and the handlers directly — never the network — because the
only question is which value reaches the request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobreach import settings
from jobreach.cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, build_parser, main
from jobreach.config import DEFAULT_LOCATION, DEFAULT_SOURCES, NOTE_SUBFOLDER, Sources
from jobreach.notes import write_note
from jobreach.pipeline import SearchRequest, run_search


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


def test_doctor_json_reports_paths_and_backend(
    data_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """``doctor`` is the first thing an agent calls when a search fails.

    The backends are stubbed to "nothing installed" so the assertions describe
    the *contract* — a board's readiness follows from what it needs, not from
    what happens to be installed on the machine running the tests.
    """
    monkeypatch.setattr("jobreach.scrapling.scrapling_python", lambda: (None, ""))
    monkeypatch.setattr("jobreach.scrapers.base.probe_playwright", lambda: (False, "no playwright"))

    code, out, _ = run(capsys, "doctor", "--json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["plugin_version"]
    assert payload["data_dir"] == str(data_home)
    assert payload["backend"]["selected"] is None
    assert "job-reach setup" in payload["backend"]["hint"]

    boards = payload["boards"]
    assert set(boards) == {
        "wantedly", "indeed", "green", "daijob", "japandev", "mynavi2027", "linkedin",
    }
    # Board readiness follows from what each board needs.
    assert boards["wantedly"]["needs_browser"] is False
    assert boards["wantedly"]["ready"] is True  # JSON over HTTP needs nothing
    assert "JSON" in boards["wantedly"]["note"]
    assert boards["green"]["needs_browser"] is False
    assert boards["green"]["ready"] is True  # payload embedded in the page
    assert boards["indeed"]["needs_browser"] is True
    assert boards["indeed"]["ready"] is False  # …and there is no browser here


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
    """With no Scrapling, setup falls back to the Playwright venv — and needs uv."""
    monkeypatch.setattr(
        "jobreach.runtime.scrapling_status",
        lambda: {"ready": False, "python": None, "source": "", "version": "", "problem": ""},
    )
    monkeypatch.setattr("jobreach.runtime.uv_path", lambda: None)
    code, out, _ = run(capsys, "setup", "--no-browser")
    assert code == EXIT_FAILURE
    assert "uv was not found" in out


def test_setup_uses_scrapling_when_it_is_installed(
    data_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """The whole point of the Scrapling backend: setup downloads nothing."""
    monkeypatch.setattr(
        "jobreach.runtime.scrapling_status",
        lambda: {
            "ready": True,
            "python": "/opt/scrapling/bin/python",
            "source": "test",
            "version": "0.4.15",
            "problem": "",
        },
    )
    monkeypatch.setattr("jobreach.runtime.uv_path", lambda: None)  # would fail if reached
    monkeypatch.setattr("jobreach.runtime.smoke_test", lambda: (True, "{}"))

    code, out, _ = run(capsys, "setup", "--json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["backend"] == "scrapling"
    assert payload["ok"] is True
    assert all(step["ok"] for step in payload["steps"])


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


# --------------------------------------------------------------------------- #
# Settings-aware defaults
# --------------------------------------------------------------------------- #


def setting(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    """Set one setting exactly as the plugin process bridges it."""
    monkeypatch.setenv(settings.env_name(key), value)


def clear_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detach every bridged setting, so a test sees the built-in default.

    The value is normally absent, but the engine inherits the shell it was
    started from — a developer who exported ``JOBREACH_SETTING_DEFAULT_SOURCES``
    to reproduce something must not turn these assertions into lies.
    """
    for key in settings.CONFIG_SCHEMA_KEYS:
        monkeypatch.delenv(settings.env_name(key), raising=False)


def test_search_source_default_is_the_built_in_feed(
    monkeypatch: pytest.MonkeyPatch,
):
    """With no setting, ``--source`` is still ``config.DEFAULT_SOURCES``."""
    clear_settings(monkeypatch)
    args = build_parser().parse_args(["search"])
    assert Sources.parse(args.source).as_list() == list(DEFAULT_SOURCES)
    # The settings work moved nothing else: a bare search is still the design
    # feed in Tokyo with no keyword.
    assert args.keyword is None
    assert args.location == DEFAULT_LOCATION


def test_the_configured_default_sources_reach_search_and_monitor(
    monkeypatch: pytest.MonkeyPatch,
):
    """One setting feeds both subcommands — they are not allowed to drift.

    ``monitor`` used to hard-code ``wantedly,linkedin`` while ``search`` used
    the config default; now both answer "no ``--source``" identically.
    """
    setting(monkeypatch, "default_sources", "green, japandev")
    parser = build_parser()
    assert Sources.parse(parser.parse_args(["search"]).source).as_list() == [
        "green", "japandev",
    ]
    assert Sources.parse(parser.parse_args(["monitor"]).source).as_list() == [
        "green", "japandev",
    ]


def test_an_explicit_source_beats_the_setting(monkeypatch: pytest.MonkeyPatch):
    setting(monkeypatch, "default_sources", "green")
    parser = build_parser()
    for argv, expected in (
        (["search", "--source", "daijob"], "daijob"),
        (["monitor", "-s", "wantedly"], "wantedly"),
    ):
        assert Sources.parse(parser.parse_args(argv).source).as_list() == [expected]


def test_help_shows_the_effective_source_default(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """The effective default has to be visible where a human looks for it."""
    setting(monkeypatch, "default_sources", "green,japandev")
    with pytest.raises(SystemExit) as excinfo:
        main(["search", "--help"])
    assert excinfo.value.code == 0
    assert "green,japandev" in capsys.readouterr().out


def test_a_bogus_configured_source_is_a_usage_error(
    data_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """A typo in ``config.yaml`` must come back as an error, not a traceback.

    ``settings`` deliberately does not validate the board names; ``Sources``
    does, and its message is the actionable one.
    """
    setting(monkeypatch, "default_sources", "monster")
    code, _, err = run(capsys, "search", "--json")
    assert code == EXIT_USAGE
    assert "unknown source" in err


class FakeRepository:
    """The only part of a repository the search/monitor handlers touch."""

    def close(self) -> None:
        pass


def capture_search(monkeypatch: pytest.MonkeyPatch) -> list[SearchRequest]:
    """Replace the scraping half of ``search`` with a recorder (no browser)."""
    seen: list[SearchRequest] = []

    async def fake_search(request: SearchRequest, repository: object) -> dict:
        seen.append(request)
        return {
            "mode": "search",
            "query": {"keyword": request.keyword, "location": request.location,
                      "sources": request.sources.as_list()},
            "summary": {"total": 0, "new": 0, "saved": 0, "shown": 0,
                        "by_platform": {}, "errors": {}},
            "jobs": [],
        }

    monkeypatch.setattr("jobreach.cli.search", fake_search)
    monkeypatch.setattr("jobreach.cli._preflight", lambda sources: None)
    monkeypatch.setattr("jobreach.cli.open_db", lambda path: FakeRepository())
    return seen


def test_search_dispatch_runs_the_configured_boards(
    data_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    setting(monkeypatch, "default_sources", "green,japandev")
    seen = capture_search(monkeypatch)
    code, _, _ = run(capsys, "search", "--json")
    assert code == EXIT_OK
    assert seen[-1].sources.as_list() == ["green", "japandev"]


def test_monitor_dispatch_watches_the_configured_boards(
    data_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    setting(monkeypatch, "default_sources", "green")
    seen = capture_search(monkeypatch)
    code, out, _ = run(capsys, "monitor")
    assert code == EXIT_OK
    assert out == ""  # no new listings → cron stays silent
    assert seen[-1].sources.as_list() == ["green"]
    assert seen[-1].new_only is True


NOTE_ENVELOPE = json.dumps(
    {
        "mode": "search",
        "query": {"keyword": "designer", "location": "tokyo"},
        "summary": {"total": 0, "new": 0},
        "jobs": [],
    }
)


def write_a_note(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    vault: Path,
    *extra: str,
) -> Path:
    """Run ``note`` against a temp vault and return where the note landed."""
    monkeypatch.setattr("sys.stdin", FakeStdin(NOTE_ENVELOPE))
    code, out, _ = run(capsys, "note", "--vault", str(vault), "--json", *extra)
    assert code == EXIT_OK
    return Path(json.loads(out)["note"])


def test_note_subfolder_default_comes_from_the_setting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    setting(monkeypatch, "note_subfolder", "hermes-notes")
    note = write_a_note(capsys, monkeypatch, tmp_path)
    assert note.parent == tmp_path / "hermes-notes"
    assert note.exists()


def test_note_subfolder_default_is_the_built_in_one(
    monkeypatch: pytest.MonkeyPatch,
):
    clear_settings(monkeypatch)
    assert build_parser().parse_args(["note"]).subfolder == NOTE_SUBFOLDER


def test_an_explicit_subfolder_beats_the_setting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    setting(monkeypatch, "note_subfolder", "hermes-notes")
    note = write_a_note(capsys, monkeypatch, tmp_path, "--subfolder", "from-the-flag")
    assert note.parent == tmp_path / "from-the-flag"


def test_a_direct_write_note_call_uses_the_same_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``notes.write_note`` must not keep a second, private default.

    The CLI passes ``--subfolder`` explicitly; a caller that omits it (the
    Hermes tools, a script) has to land in the same folder as the flag would.
    """
    setting(monkeypatch, "note_subfolder", "hermes-notes")
    path = write_note(
        {"query": {"keyword": "designer", "location": "tokyo"}, "summary": {}, "jobs": []},
        vault=tmp_path,
    )
    assert path.parent == tmp_path / "hermes-notes"


def test_a_default_search_request_uses_the_configured_sources(
    monkeypatch: pytest.MonkeyPatch,
):
    """``SearchRequest()`` is the pipeline's "no ``--source``" — the same knob."""
    clear_settings(monkeypatch)
    assert SearchRequest().sources.as_list() == list(DEFAULT_SOURCES)
    setting(monkeypatch, "default_sources", "daijob")
    assert SearchRequest().sources.as_list() == ["daijob"]


def test_run_search_reads_the_same_setting(monkeypatch: pytest.MonkeyPatch):
    """The in-process façade goes through the same resolver as the CLI."""
    seen: list[SearchRequest] = []

    async def fake_search(request: SearchRequest, repository: object) -> dict:
        seen.append(request)
        return {}

    monkeypatch.setattr("jobreach.pipeline.search", fake_search)
    monkeypatch.setattr("jobreach.pipeline.open_db", lambda path: FakeRepository())
    setting(monkeypatch, "default_sources", "green")
    run_search()
    assert seen[-1].sources.as_list() == ["green"]

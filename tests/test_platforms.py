"""Portability: the platform-specific logic, and the promise that there is little of it.

The plugin is meant to be installable by *any* Hermes on macOS, Linux or
Windows, so the tests here pin two things:

1. the handful of platform decisions (virtualenv layout, process-tree teardown,
   uv discovery, cron-script shape) resolve per platform **as data**, which is
   how they are verified on a machine that is not that platform; and
2. importing the engine pulls in **nothing outside the standard library** —
   measured in a real subprocess against a baseline, not by reading source.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jobreach import proc
from jobreach.platforms import (
    is_windows,
    popen_kwargs,
    process_tree_kill_argv,
    uv_candidates,
    venv_bin_dir,
    venv_python,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --- platform detection ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [("win32", True), ("Windows", True), ("linux", False), ("darwin", False)],
)
def test_is_windows_reads_the_pinned_name(name: str, expected: bool):
    assert is_windows(name) is expected


def test_is_windows_without_a_name_uses_the_real_platform():
    assert is_windows() is (os.name == "nt")


# --- virtualenv layout -----------------------------------------------------


def test_venv_layout_follows_the_platform():
    assert venv_bin_dir("/x/venv", "linux") == Path("/x/venv/bin")
    assert venv_python("/x/venv", "linux") == Path("/x/venv/bin/python")
    assert venv_bin_dir("/x/venv", "win32") == Path("/x/venv/Scripts")
    assert venv_python("/x/venv", "win32") == Path("/x/venv/Scripts/python.exe")


def test_the_engine_looks_for_windows_interpreters_on_windows(monkeypatch: pytest.MonkeyPatch):
    """The Scrapling probe must find ``Scripts/python.exe``, not ``bin/python``."""
    monkeypatch.setattr("jobreach.platforms.is_windows", lambda platform_name=None: True)
    monkeypatch.setenv("JOBREACH_SCRAPLING_PYTHON", r"C:\tools\scrapling\Scripts\python.exe")

    from jobreach.scrapling import _candidate_pythons

    candidates = [path for path, _source in _candidate_pythons()]
    assert candidates[0].endswith("python.exe")
    assert all(path.endswith("python.exe") for path in candidates), candidates


def test_the_engine_looks_for_posix_interpreters_otherwise(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("jobreach.platforms.is_windows", lambda platform_name=None: False)
    monkeypatch.setenv("JOBREACH_SCRAPLING_PYTHON", "/opt/scrapling/bin/python")

    from jobreach.scrapling import _candidate_pythons

    candidates = [path for path, _source in _candidate_pythons()]
    assert candidates[0] == "/opt/scrapling/bin/python"
    assert all(not path.endswith(".exe") for path in candidates), candidates


# --- uv discovery ----------------------------------------------------------


def test_uv_candidates_match_the_platform():
    windows = uv_candidates("win32")
    posix = uv_candidates("linux")
    assert all(path.name == "uv.exe" for path in windows), windows
    assert all(path.name == "uv" for path in posix), posix
    assert str(windows[0]).endswith(os.path.join("uv", "uv.exe"))
    assert posix[0] == Path.home() / ".hermes" / "bin" / "uv"


# --- process-tree teardown -------------------------------------------------


def test_windows_kills_the_tree_with_taskkill():
    argv = process_tree_kill_argv(4242, "win32")
    assert argv == ["taskkill", "/F", "/T", "/PID", "4242"]


def test_posix_signals_the_process_group_instead():
    assert process_tree_kill_argv(4242, "linux") is None


def test_popen_kwargs_isolate_the_child_per_platform():
    posix = popen_kwargs("linux")
    assert posix == {"start_new_session": True}

    windows = popen_kwargs("win32")
    assert "start_new_session" not in windows, "POSIX-only kwarg must not leak to Windows"


def test_terminating_on_windows_uses_taskkill(monkeypatch: pytest.MonkeyPatch):
    """A timed-out scrape on Windows must kill the whole tree, not just Python."""
    calls: list[list[str]] = []

    class FakeProcess:
        pid = 31337

        def wait(self, timeout=None):
            return 0

        def kill(self):  # pragma: no cover - only on the taskkill fallback
            calls.append(["kill"])

    monkeypatch.setattr(
        "jobreach.proc.process_tree_kill_argv", lambda pid: ["taskkill", "/F", "/T", "/PID", str(pid)]
    )
    monkeypatch.setattr(
        "jobreach.proc.subprocess.run",
        lambda argv, **kwargs: calls.append(list(argv)),
    )

    proc._terminate_tree(FakeProcess(), None)
    assert calls[0][0] == "taskkill"
    assert "31337" in calls[0]


def test_taskkill_falls_back_to_the_direct_child(monkeypatch: pytest.MonkeyPatch):
    """If taskkill is missing, the child still dies."""
    killed: list[str] = []

    class FakeProcess:
        pid = 5

        def wait(self, timeout=None):
            return 0

        def kill(self):
            killed.append("direct")

    def boom(argv, **kwargs):
        raise FileNotFoundError("taskkill not found")

    monkeypatch.setattr("jobreach.proc.subprocess.run", boom)
    proc._taskkill(["taskkill"], FakeProcess())
    assert killed == ["direct"]


# --- the stdlib-only promise ----------------------------------------------


IMPORT_PROBE = """
import json, sys

before = {m.split(".")[0] for m in sys.modules}
__IMPORTS__
after = {m.split(".")[0] for m in sys.modules}
extra = sorted(after - before - set(sys.stdlib_module_names) - {"jobreach", "_distutils_hack"})
print(json.dumps(extra))
"""

ENGINE_IMPORTS = "\n".join(
    f"import {name}"
    for name in (
        "jobreach",
        "jobreach.cli",
        "jobreach.config",
        "jobreach.domain",
        "jobreach.fetchers",
        "jobreach.htmlextract",
        "jobreach.install",
        "jobreach.notes",
        "jobreach.pipeline",
        "jobreach.platforms",
        "jobreach.proc",
        "jobreach.runtime",
        "jobreach.scrapers",
        "jobreach.scrapling",
        "jobreach.store",
        "jobreach.webclient",
    )
)


def run_probe(imports: str) -> list[str]:
    """Import the engine in a clean subprocess and report non-stdlib additions."""
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    env.pop("VIRTUAL_ENV", None)
    code = IMPORT_PROBE.replace("__IMPORTS__", imports)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_the_engine_pulls_in_nothing_third_party():
    """The plugin must load into *any* Hermes runtime, with no packages installed.

    Measured as a delta against the same interpreter without our imports, so
    whatever the host's ``site`` startup already loads does not muddy the result.
    """
    baseline = run_probe("pass")
    with_engine = run_probe(ENGINE_IMPORTS)
    assert with_engine == baseline, f"the engine imported extra modules: {sorted(set(with_engine) - set(baseline))}"


# --- generated monitor script runtime resolution ---------------------------


def test_generated_monitor_script_fails_gracefully_when_plugin_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When baked plugin dir is missing and no candidate exists: exit 1, no traceback."""
    from jobreach.install import MONITOR_SCRIPT_TEMPLATE

    hermes_home = tmp_path / "hermes-empty"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("JOBREACH_PLUGIN_DIR", raising=False)

    baked_path = "/nonexistent/baked/plugin-path"
    script_path = tmp_path / "monitor.py"
    script_path.write_text(
        MONITOR_SCRIPT_TEMPLATE.format(
            plugin=baked_path,
            python=sys.executable,
            args=[],
            job_name="job-reach-monitor",
            # Baked explicitly: the template has no defaults, and an omitted
            # snapshot path would silently resolve against the ambient
            # HERMES_HOME (the developer's real one) while rendering.
            snapshot=str(tmp_path / "settings.json"),
        ),
        encoding="utf-8",
    )

    proc_res = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
    assert proc_res.returncode == 1
    assert baked_path in proc_res.stderr
    assert "Traceback" not in proc_res.stderr
    assert "job-reach monitor: plugin directory not found" in proc_res.stderr


def test_generated_monitor_script_prefers_installed_candidate_over_baked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An installed candidate under $HERMES_HOME/plugins/job-reach wins over baked PLUGIN_DIR."""
    from jobreach.install import MONITOR_SCRIPT_TEMPLATE

    hermes_home = tmp_path / "hermes-home"
    installed_dir = hermes_home / "plugins" / "job-reach"
    installed_pkg = installed_dir / "jobreach"
    installed_pkg.mkdir(parents=True, exist_ok=True)
    (installed_pkg / "__main__.py").write_text(
        "import sys; print('FROM_INSTALLED'); sys.exit(0)\n", encoding="utf-8"
    )

    baked_dir = tmp_path / "baked-repo"
    baked_pkg = baked_dir / "jobreach"
    baked_pkg.mkdir(parents=True, exist_ok=True)
    (baked_pkg / "__main__.py").write_text(
        "import sys; print('FROM_BAKED'); sys.exit(0)\n", encoding="utf-8"
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("JOBREACH_PLUGIN_DIR", raising=False)

    script_path = tmp_path / "monitor.py"
    script_path.write_text(
        MONITOR_SCRIPT_TEMPLATE.format(
            plugin=str(baked_dir),
            python=sys.executable,
            args=[],
            job_name="job-reach-monitor",
            snapshot=str(tmp_path / "settings.json"),
        ),
        encoding="utf-8",
    )

    proc_res = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
    assert proc_res.returncode == 0
    assert "FROM_INSTALLED" in proc_res.stdout
    assert "FROM_BAKED" not in proc_res.stdout


def test_generated_monitor_script_guards_vanished_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the engine interpreter is gone: exit 1, naming the path, no traceback."""
    from jobreach.install import MONITOR_SCRIPT_TEMPLATE

    script_path = tmp_path / "monitor.py"
    script_path.write_text(
        MONITOR_SCRIPT_TEMPLATE.format(
            plugin=str(PROJECT_ROOT),
            python="/nonexistent/bin/python",
            args=[],
            job_name="job-reach-monitor",
            snapshot=str(tmp_path / "settings.json"),
        ),
        encoding="utf-8",
    )

    proc_res = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
    assert proc_res.returncode == 1
    assert "/nonexistent/bin/python" in proc_res.stderr
    assert "Traceback" not in proc_res.stderr

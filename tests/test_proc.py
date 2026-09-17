"""Process execution: output capture, timeouts, and process-tree teardown.

The tree test is the important one. ``subprocess.run(timeout=...)`` kills only
the direct child, which is not the process doing the work here: the engine
launches Playwright, which launches a Node driver, which launches Chromium. A
regression that went back to plain ``subprocess.run`` would leave those
grandchildren running, and a monitoring cron job hitting it repeatedly would
accumulate orphaned browsers — so this behaviour is pinned by an actual process
tree, not a mock.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jobreach.proc import run_captured

#: Long enough that a leaked process is unmistakably still alive.
SLEEP_SECONDS = 300


def test_captures_stdout_stderr_and_exit_code():
    result = run_captured(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ]
    )
    assert result.returncode == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_non_zero_exit_is_not_an_exception():
    """The caller decides what an exit code means; the runner must not guess."""
    assert run_captured([sys.executable, "-c", "raise SystemExit(1)"]).returncode == 1


def test_stdin_is_piped():
    result = run_captured(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        stdin="hello",
    )
    assert result.stdout == "HELLO"


def test_cwd_and_env_are_honoured(tmp_path: Path):
    result = run_captured(
        [sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ['MARKER'])"],
        cwd=tmp_path,
        env={"MARKER": "set", "PATH": os.environ.get("PATH", "")},
    )
    lines = result.stdout.splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1] == "set"


def test_env_is_not_inherited():
    os.environ["LEAKY_VARIABLE"] = "should-not-leak"
    try:
        result = run_captured(
            [sys.executable, "-c", "import os; print(os.environ.get('LEAKY_VARIABLE', 'absent'))"],
            env={"PATH": os.environ.get("PATH", "")},
        )
    finally:
        del os.environ["LEAKY_VARIABLE"]
    assert result.stdout.strip() == "absent"


def test_timeout_raises_and_reports_the_command():
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        run_captured(
            [sys.executable, "-c", f"import time; time.sleep({SLEEP_SECONDS})"], timeout=1.0
        )
    assert excinfo.value.timeout == 1.0


def test_timeout_keeps_whatever_was_printed_before_it_fired():
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        run_captured(
            [
                sys.executable,
                "-c",
                f"import time; print('partial', flush=True); time.sleep({SLEEP_SECONDS})",
            ],
            timeout=1.0,
        )
    assert "partial" in (excinfo.value.output or "")


def test_missing_executable_raises_oserror():
    with pytest.raises(OSError):
        run_captured(["/nonexistent/definitely-not-a-binary"])


# --- the reason this module exists -----------------------------------------


def _pid_is_running(pid: int) -> bool:
    """Whether *pid* is a live process. A zombie counts as dead: it no longer runs."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    probe = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    )
    return not probe.stdout.strip().upper().startswith("Z")


def _wait_until_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.1)
    return False


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="needs POSIX process groups")
def test_timeout_kills_the_whole_process_tree(tmp_path: Path):
    """A grandchild must die with its parent, not be reparented and survive."""
    pidfile = tmp_path / "grandchild.pid"
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep({SLEEP_SECONDS})'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        f"time.sleep({SLEEP_SECONDS})\n",
        encoding="utf-8",
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_captured([sys.executable, str(parent), str(pidfile)], timeout=3.0)

    assert pidfile.exists(), "the parent never got far enough to spawn a child"
    grandchild = int(pidfile.read_text())
    assert _wait_until_dead(grandchild), (
        f"grandchild {grandchild} outlived the timeout — the process group was "
        "not torn down, which is how orphaned Chromium processes accumulate"
    )


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="needs POSIX process groups")
def test_a_clean_exit_is_not_disturbed(tmp_path: Path):
    """Teardown must only run on failure — a normal call leaves everything alone."""
    result = run_captured([sys.executable, "-c", "print('fine')"], timeout=30.0)
    assert result.returncode == 0
    assert result.stdout.strip() == "fine"

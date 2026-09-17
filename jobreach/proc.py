"""Subprocess execution that cleans up after itself.

The engine spawns two kinds of child: the plugin spawns the engine itself, and
the LinkedIn scraper spawns ``opencli``. Both can outlive a naive timeout,
because the process we start is rarely the process doing the work.

``subprocess.run(timeout=...)`` kills only the **direct child** it started, and
that is not enough here: ``python -m jobreach`` launches Playwright, which
launches a Node driver, which launches Chromium. Kill the Python parent and the
three descendants below it are reparented to init and keep running — so a hung
scrape inside a frequently-ticking ``hermes cron`` job would quietly accumulate
orphaned browsers.

The fix is to start each child in its own session (which makes it a process
group leader) and signal the whole group: SIGTERM first so Chromium can shut
down cleanly, then SIGKILL for anything that ignores it. The group id is
captured *before* any waiting, because ``os.getpgid()`` stops resolving once the
direct child has been reaped.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

#: Grace period between SIGTERM and SIGKILL when tearing a process group down.
TERM_GRACE_SECONDS = 5.0

#: How long to wait for the pipes to close after a kill. Anything still holding
#: them belongs to a process we already signalled, so a short wait is enough.
DRAIN_SECONDS = 5.0

#: Polling interval while waiting for a process group to go quiet.
POLL_SECONDS = 0.05


def run_captured(
    argv: Sequence[str],
    *,
    timeout: float | None = None,
    stdin: str | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *argv*, capture its output, and kill its whole tree on timeout.

    Args:
        argv: Command and arguments.
        timeout: Seconds to wait. On expiry the entire process group is
            terminated and :class:`subprocess.TimeoutExpired` is raised.
        stdin: Text piped to the child's stdin (closed right after).
        cwd: Working directory for the child.
        env: Complete environment for the child (not merged with ``os.environ``).

    Returns:
        A :class:`subprocess.CompletedProcess`. A non-zero exit is **not** an
        exception — callers decide what a given exit code means.

    Raises:
        subprocess.TimeoutExpired: the child outlived *timeout*.
        OSError: the command could not be started at all.
    """
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        # Own session => own process group => one signal reaches every
        # descendant, Chromium included.
        start_new_session=True,
    )
    # Resolve the group id now: once the direct child is reaped, getpgid() on
    # its pid raises, and we would lose the handle to its still-running
    # descendants.
    pgid = _process_group(process)

    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_tree(process, pgid)
        out, err = _drain(process)
        raise subprocess.TimeoutExpired(
            exc.cmd, exc.timeout, output=out, stderr=err
        ) from None
    except BaseException:
        # Ctrl+C, SystemExit, a cancellation: the child is in its own session,
        # so the terminal's signal never reached it. Take it down explicitly
        # rather than leaving an orphaned browser behind.
        _terminate_tree(process, pgid)
        _drain(process)
        raise

    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


# --------------------------------------------------------------------------- #
# Process-tree teardown
# --------------------------------------------------------------------------- #


def _process_group(process: subprocess.Popen) -> int | None:
    """Return the child's process-group id, or ``None`` where unsupported."""
    if not hasattr(os, "killpg"):
        return None
    try:
        return os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        return None


def _terminate_tree(process: subprocess.Popen, pgid: int | None) -> None:
    """SIGTERM the group, then SIGKILL whatever is still standing."""
    _signal(process, pgid, signal.SIGTERM)
    if _wait_for_quiet(process, pgid, TERM_GRACE_SECONDS):
        return
    _signal(process, pgid, signal.SIGKILL)
    _wait_for_quiet(process, pgid, TERM_GRACE_SECONDS)


def _signal(process: subprocess.Popen, pgid: int | None, sig: int) -> None:
    """Send *sig* to the whole group, falling back to the direct child."""
    if pgid is not None:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, sig)
            return
    with suppress(ProcessLookupError, OSError):
        process.send_signal(sig)


def _wait_for_quiet(process: subprocess.Popen, pgid: int | None, timeout: float) -> bool:
    """Wait until the direct child has exited *and* nothing is left in its group.

    Waiting on the child alone would be wrong: it can exit while the browser it
    started keeps running, which is exactly the leak this module exists to
    prevent.
    """
    if pgid is None:
        return _wait(process, timeout)

    deadline = time.monotonic() + timeout
    while True:
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0)  # reap the direct child if it has exited
        if _group_is_empty(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def _wait(process: subprocess.Popen, timeout: float) -> bool:
    """Wait for the direct child; ``True`` when it has exited."""
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _group_is_empty(pgid: int) -> bool:
    """Whether the process group has no members left (zombies included)."""
    try:
        os.killpg(pgid, 0)  # signal 0 is an existence probe
    except (ProcessLookupError, OSError):
        return True
    except PermissionError:
        return False
    return False


def _drain(process: subprocess.Popen) -> tuple[str, str]:
    """Collect whatever output is left without blocking forever.

    A surviving grandchild can hold the stdout pipe open; the bounded wait keeps
    that from turning a timeout into a hang.
    """
    try:
        stdout, stderr = process.communicate(timeout=DRAIN_SECONDS)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return "", ""
    return stdout or "", stderr or ""

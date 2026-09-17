"""Everything that differs between operating systems, in one place.

The plugin is meant to be installable by *any* Hermes, on any platform it
supports, so the handful of places that cannot be written portably are collected
here instead of being sprinkled as ``os.name`` checks:

* **Virtualenv layout** — ``venv/bin/python`` on POSIX, ``venv\\Scripts\\python.exe``
  on Windows. Getting this wrong is a silent "no interpreter found" that looks
  like a missing install.
* **uv's likely homes** — the installer puts it in different places per platform.
* **Process-tree teardown** — POSIX signals a process group; Windows has no
  process groups to signal, so the tree is killed with
  ``taskkill /F /T /PID``. Without it a timed-out scrape leaves Chromium
  running (see :mod:`jobreach.proc`).

Every function takes an optional ``platform_name`` so tests can pin the answer
without pretending to be another OS — the logic is data in, data out.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Suffix used by Python executables on Windows.
_WINDOWS = "win"


def is_windows(platform_name: str | None = None) -> bool:
    """Whether we are on Windows; *platform_name* (e.g. ``"win32"``) pins it."""
    if platform_name is not None:
        return platform_name.lower().startswith(_WINDOWS)
    return os.name == "nt"


def venv_bin_dir(venv_root: str | Path, platform_name: str | None = None) -> Path:
    """The directory holding a virtualenv's executables."""
    return Path(venv_root) / ("Scripts" if is_windows(platform_name) else "bin")


def venv_python(venv_root: str | Path, platform_name: str | None = None) -> Path:
    """The path of a virtualenv's interpreter."""
    name = "python.exe" if is_windows(platform_name) else "python"
    return venv_bin_dir(venv_root, platform_name) / name


def uv_candidates(platform_name: str | None = None) -> tuple[Path, ...]:
    """Places to look for the ``uv`` binary, most likely first."""
    home = Path.home()
    if is_windows(platform_name):
        local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        return (
            local / "uv" / "uv.exe",
            home / ".local" / "bin" / "uv.exe",
            home / ".cargo" / "bin" / "uv.exe",
            home / ".hermes" / "bin" / "uv.exe",
        )
    return (
        home / ".hermes" / "bin" / "uv",
        home / ".local" / "bin" / "uv",
        Path("/opt/homebrew/bin/uv"),
        Path("/usr/local/bin/uv"),
    )


def process_tree_kill_argv(pid: int, platform_name: str | None = None) -> list[str] | None:
    """argv that kills *pid* **and its descendants**, or ``None`` on POSIX.

    POSIX callers signal the child's process group themselves; Windows has no
    such group, so the tree is taken down by ``taskkill``.
    """
    if is_windows(platform_name):
        return ["taskkill", "/F", "/T", "/PID", str(pid)]
    return None


def popen_kwargs(platform_name: str | None = None) -> dict:
    """Popen kwargs that keep a child from sharing our console signals.

    POSIX: its own session (which also makes it a process-group leader).
    Windows: a new process group, so Ctrl+C in the console is not delivered to
    the scrape we merely *started* on the user's behalf.
    """
    if is_windows(platform_name):
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True}

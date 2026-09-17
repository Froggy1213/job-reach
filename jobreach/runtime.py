"""Runtime resolution and one-time setup.

The plugin is unusual among Hermes plugins in that it prefers *not* to import
its engine into Hermes' own interpreter. Two reasons:

* Hermes' runtime venv is Python 3.14. The scraping extra (Playwright) is a
  large binary dependency that has no business living in — and being broken by
  — Hermes' own environment.
* A scrape takes 30–90 seconds. Running it in a subprocess keeps it cancellable
  and keeps a crashed browser from taking the agent down with it.

So the tools spawn ``python -m jobreach`` and this module decides *which*
python:

1. ``$JOBREACH_PYTHON``                  — explicit operator override
2. ``$JOBREACH_HOME/venv/bin/python``    — created by ``hermes job-reach setup``
3. ``sys.executable``                     — works for everything except scraping,
                                             because the core is stdlib-only

The venv lives under ``$JOBREACH_HOME`` (default ``~/.hermes/job-reach``), not
inside the plugin directory, so ``hermes plugins update`` cannot wipe it.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .config import jobreach_home, plugin_dir, python_override
from .logging_setup import get_logger
from .scrapers.base import PLAYWRIGHT_HINT

logger = get_logger("runtime")

#: Extra packages the scrapers need, installed into the plugin venv.
SCRAPE_PACKAGES = ("playwright", "playwright-stealth")

#: Python version requested when creating the venv. 3.11 is the floor declared
#: in ``pyproject.toml`` and has the widest prebuilt-wheel coverage.
VENV_PYTHON = "3.11"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def uv_path() -> str | None:
    """Locate ``uv``: env override, PATH, then the copy Hermes ships."""
    override = os.environ.get("JOBREACH_UV", "").strip()
    if override and Path(override).exists():
        return override
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (
        Path.home() / ".hermes" / "bin" / "uv",
        Path.home() / ".local" / "bin" / "uv",
        Path("/opt/homebrew/bin/uv"),
        Path("/usr/local/bin/uv"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def venv_dir() -> Path:
    """Directory of the optional scraping venv."""
    return jobreach_home() / "venv"


def venv_python() -> Path | None:
    """The venv's interpreter, if the venv exists."""
    candidate = venv_dir() / "bin" / "python"
    return candidate if candidate.exists() else None


def resolve_interpreter() -> tuple[str, str]:
    """Pick the interpreter used to run the engine.

    Returns:
        ``(python_path, source)`` where *source* names the rule that matched —
        surfaced by ``doctor`` so a surprising choice is diagnosable.
    """
    override = python_override()
    if override:
        return override, "JOBREACH_PYTHON"
    venv = venv_python()
    if venv is not None:
        return str(venv), "plugin venv"
    return sys.executable, "current interpreter (no plugin venv yet)"


def engine_argv() -> list[str]:
    """Full argv prefix for running the engine, e.g. ``[python, "-m", "jobreach"]``."""
    python, _ = resolve_interpreter()
    return [python, "-m", "jobreach"]


def engine_env() -> dict[str, str]:
    """Environment for a child engine process.

    ``PYTHONPATH`` is set explicitly to the plugin directory so the child finds
    ``jobreach`` even when the cwd is not the plugin directory, and Hermes'
    own ``VIRTUAL_ENV`` is dropped so it cannot leak into the child.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(plugin_dir())
    env.pop("VIRTUAL_ENV", None)
    env["JOBREACH_PLUGIN_DIR"] = str(plugin_dir())
    return env


def run_engine(
    args: Sequence[str],
    *,
    timeout: float = 600.0,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the engine CLI and capture its output. Never raises on exit code."""
    argv = [*engine_argv(), *args]
    logger.debug("running engine", extra={"argv": argv})
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        input=stdin,
        timeout=timeout,
        cwd=str(plugin_dir()),
        env=engine_env(),
    )


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SetupReport:
    """Outcome of :func:`setup_runtime`, step by step."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True
    venv: str | None = None
    hint: str = ""

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": name, "ok": ok, "detail": detail})
        if not ok:
            self.ok = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "venv": self.venv,
            "steps": self.steps,
            "hint": self.hint,
            "interpreter": resolve_interpreter()[0],
        }


def setup_runtime(*, with_browser: bool = True, force: bool = False) -> SetupReport:
    """Create the plugin venv and install the scraping extra.

    Args:
        with_browser: also download the Chromium build Playwright drives.
        force: recreate the venv even if it already exists.
    """
    report = SetupReport()

    uv = uv_path()
    target = venv_dir()
    existing = venv_python()

    if existing is not None and not force:
        report.add("venv", True, f"already present at {target}")
    else:
        if uv is None:
            report.add(
                "uv",
                False,
                "uv was not found. Install it (https://astral.sh/uv) or set "
                "JOBREACH_UV to its path.",
            )
            return report
        if force and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        result = subprocess.run(
            [uv, "venv", "--python", VENV_PYTHON, str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            report.add("venv", False, (result.stderr or result.stdout).strip()[:500])
            return report
        report.add("venv", True, f"created at {target} (python {VENV_PYTHON})")

    python = venv_python()
    if python is None:  # pragma: no cover - only if creation silently failed
        report.add("venv", False, f"no interpreter at {target}/bin/python")
        return report
    report.venv = str(python)

    result = subprocess.run(
        [uv or "uv", "pip", "install", "--python", str(python), *SCRAPE_PACKAGES],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        report.add("scraping extra", False, (result.stderr or result.stdout).strip()[:500])
        return report
    report.add("scraping extra", True, ", ".join(SCRAPE_PACKAGES))

    if with_browser:
        result = subprocess.run(
            [str(python), "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            report.add(
                "chromium",
                False,
                (result.stderr or result.stdout).strip()[:500],
            )
            report.hint = (
                "The venv is ready but Chromium is missing. Re-run "
                "`hermes job-reach setup`, or install it manually:\n"
                f"  {python} -m playwright install chromium"
            )
            return report
        report.add("chromium", True, "playwright chromium downloaded")
    else:
        report.add("chromium", True, "skipped (--no-browser)")

    probe = subprocess.run(
        [str(python), "-m", "jobreach", "doctor", "--json"],
        capture_output=True,
        text=True,
        cwd=str(plugin_dir()),
        env=engine_env(),
    )
    report.add(
        "engine smoke test",
        probe.returncode == 0,
        (probe.stdout or probe.stderr).strip()[:300],
    )
    return report


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def probe_browser_support(python: str, *, timeout: float = 60.0) -> tuple[bool, str]:
    """Ask *python* whether it can import the browser stack.

    The probe runs in a subprocess on purpose. ``doctor`` is frequently executed
    by an interpreter that is *not* the engine interpreter — Hermes' own Python,
    or a system ``python3`` — and reporting that interpreter's missing
    Playwright would be a lie about what the engine can actually do.
    """
    try:
        proc = subprocess.run(
            [python, "-c", "import playwright, playwright_stealth"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run {python}: {exc}"
    if proc.returncode == 0:
        return True, ""
    lines = (proc.stderr or "").strip().splitlines()
    detail = lines[-1] if lines else f"exit code {proc.returncode}"
    return False, f"{detail}\n{PLAYWRIGHT_HINT}"


def diagnostics() -> dict[str, Any]:
    """Describe the runtime: paths, interpreter, optional deps, board readiness."""
    from .config import default_db_path, find_vault
    from .scrapers import SCRAPERS, is_cli_scraper

    python, source = resolve_interpreter()
    db = default_db_path()
    vault = find_vault()
    browser_ok, browser_problem = probe_browser_support(python)

    boards: dict[str, dict[str, Any]] = {}
    for name, scraper_class in SCRAPERS.items():
        if is_cli_scraper(name):
            executable = scraper_class.cli_base[0] if scraper_class.cli_base else ""
            missing = bool(executable) and shutil.which(executable) is None
            boards[name] = {
                "ready": not missing,
                "backend": "cli",
                "problem": (
                    f"{executable!r} is not on PATH\n{scraper_class.install_hint}"
                    if missing
                    else None
                ),
            }
        else:
            boards[name] = {
                "ready": browser_ok,
                "backend": "playwright",
                "problem": browser_problem or None,
            }
    boards["indeed"] = {
        "ready": True,
        "backend": "browser-ingest",
        "problem": None,
        "note": (
            "ingest-only: Cloudflare blocks headless clients, so the agent "
            "drives a real browser and feeds the cards to job_ingest"
        ),
    }

    return {
        "plugin_version": __version__,
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "engine_interpreter": python,
        "engine_interpreter_source": source,
        "browser_ready": browser_ok,
        "plugin_dir": str(plugin_dir()),
        "data_dir": str(jobreach_home()),
        "database": str(db),
        "database_exists": db.exists(),
        "venv": str(venv_dir()) if venv_python() else None,
        "uv": uv_path(),
        "sqlite": sqlite3.sqlite_version,
        "obsidian_vault": str(vault) if vault else None,
        "boards": boards,
    }

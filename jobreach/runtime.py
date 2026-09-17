"""Runtime resolution and one-time setup.

The plugin is unusual among Hermes plugins in that it prefers *not* to import
its engine into Hermes' own interpreter. Two reasons:

* Hermes' runtime venv is Python 3.14. The scraping stack (Playwright, or
  Scrapling with its patched Chromium) is a large binary dependency that has no
  business living in — and being broken by — Hermes' own environment.
* A scrape takes 30–90 seconds. Running it in a subprocess keeps it cancellable
  and keeps a crashed browser from taking the agent down with it.

So the tools spawn ``python -m jobreach`` and this module decides *which*
python:

1. ``$JOBREACH_PYTHON``                  — explicit operator override
2. ``$JOBREACH_HOME/venv``              — created by ``hermes job-reach setup``
   (its interpreter is ``bin/python`` or ``Scripts/python.exe``)
3. ``sys.executable``                     — works for everything except scraping,
                                             because the core is stdlib-only

Browsers are a separate question, and a cheaper one to answer. The preferred
backend is **Scrapling** — usually already installed for its MCP server, it
solves Cloudflare challenges, and it needs no venv of ours. Only when Scrapling
is absent does ``setup`` build the plugin's own venv with Playwright plus
Chromium (~150 MB). Both paths are reported by ``doctor``, board by board.

The venv lives under ``$JOBREACH_HOME`` (default
``~/.hermes/plugin-data/job-reach``), not inside the plugin directory, so
``hermes plugins update`` cannot wipe it. Its interpreter is found in the
platform's own layout (``venv/bin/python``, or ``venv\\Scripts\\python.exe`` on
Windows) — see :mod:`jobreach.platforms`.
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
from .platforms import uv_candidates, venv_bin_dir
from .platforms import venv_python as venv_python_path
from .proc import run_captured
from .scrapers.base import PLAYWRIGHT_HINT

logger = get_logger("runtime")

#: Extra packages the Playwright fallback needs, installed into the plugin venv.
SCRAPE_PACKAGES = ("playwright", "playwright-stealth")

#: Python version requested when creating the venv. 3.11 is the floor declared
#: in ``pyproject.toml`` and has the widest prebuilt-wheel coverage.
VENV_PYTHON = "3.11"

#: Ceilings for the one-time setup steps. Chromium is a ~150 MB download, so it
#: gets the most room; the point is that a stalled transfer eventually fails
#: with a message instead of hanging the agent forever.
UV_VENV_TIMEOUT = 300.0
PIP_TIMEOUT = 600.0
BROWSER_DOWNLOAD_TIMEOUT = 1800.0
SMOKE_TEST_TIMEOUT = 120.0


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def uv_path() -> str | None:
    """Locate ``uv``: env override, PATH, then the usual per-platform homes."""
    override = os.environ.get("JOBREACH_UV", "").strip()
    if override and Path(override).exists():
        return override
    found = shutil.which("uv")
    if found:
        return found
    for candidate in uv_candidates():
        if candidate.exists():
            return str(candidate)
    return None


def venv_dir() -> Path:
    """Directory of the optional scraping venv."""
    return jobreach_home() / "venv"


def venv_python() -> Path | None:
    """The venv's interpreter, if the venv exists (``Scripts`` on Windows)."""
    candidate = venv_python_path(venv_dir())
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
    """Run the engine CLI and capture its output. Never raises on exit code.

    Uses :func:`jobreach.proc.run_captured` rather than ``subprocess.run`` so a
    timeout tears down the engine's **whole process tree**. The engine starts a
    browser (and its driver processes); killing only the Python child would
    leave Chromium running, and a monitoring cron job that hits this repeatedly
    would accumulate orphans.
    """
    argv = [*engine_argv(), *args]
    logger.debug("running engine", extra={"argv": argv})
    return run_captured(
        argv,
        timeout=timeout,
        stdin=stdin,
        cwd=plugin_dir(),
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
    backend: str = ""
    hint: str = ""

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": name, "ok": ok, "detail": detail})
        if not ok:
            self.ok = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "venv": self.venv,
            "backend": self.backend,
            "steps": self.steps,
            "hint": self.hint,
            "interpreter": resolve_interpreter()[0],
        }


def scrapling_status() -> dict[str, Any]:
    """Describe the Scrapling install this machine has, if any."""
    from .scrapling import probe, scrapling_python

    python, source = scrapling_python()
    if not python:
        return {"ready": False, "python": None, "source": "", "version": "", "problem": ""}
    outcome = probe(python)
    return {
        "ready": outcome.ok,
        "python": python,
        "source": source,
        "version": outcome.version,
        "problem": "" if outcome.ok else outcome.detail,
    }


def setup_runtime(*, with_browser: bool = True, force: bool = False) -> SetupReport:
    """Prepare the scraping runtime.

    Scrapling is preferred and, when present, *is* the setup: it is checked and
    reported, and nothing is downloaded. Otherwise the plugin builds its own
    venv with Playwright and Chromium.

    Args:
        with_browser: download the Playwright Chromium build (ignored when
            Scrapling is available — the browser download exists only for the
            fallback path).
        force: recreate the plugin venv even if it already exists.
    """
    report = SetupReport()

    scrapling = scrapling_status()
    if scrapling["ready"]:
        report.backend = "scrapling"
        report.add(
            "scrapling",
            True,
            f"{scrapling['version']} at {scrapling['python']} [{scrapling['source']}] — "
            "no download needed",
        )
        report.add("playwright", True, "skipped: Scrapling is already the backend")
        report.add("chromium", True, "skipped: Scrapling drives its own browser")
        report.add("engine smoke test", *smoke_test())
        return report

    report.backend = "playwright"
    report.add(
        "scrapling",
        True,
        "not installed — using the plugin's own Playwright browser instead "
        f"(install it for Cloudflare-capable fetching: {PLAYWRIGHT_HINT.splitlines()[0]})",
    )

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
        ok, detail = _setup_step(
            [uv, "venv", "--python", VENV_PYTHON, str(target)], UV_VENV_TIMEOUT
        )
        if not ok:
            report.add("venv", False, detail)
            return report
        report.add("venv", True, f"created at {target} (python {VENV_PYTHON})")

    python = venv_python()
    if python is None:  # pragma: no cover - only if creation silently failed
        report.add("venv", False, f"no interpreter at {venv_bin_dir(venv_dir())}")
        return report
    report.venv = str(python)

    ok, detail = _setup_step(
        [uv or "uv", "pip", "install", "--python", str(python), *SCRAPE_PACKAGES],
        PIP_TIMEOUT,
    )
    if not ok:
        report.add("scraping extra", False, detail)
        return report
    report.add("scraping extra", True, ", ".join(SCRAPE_PACKAGES))

    if with_browser:
        ok, detail = _setup_step(
            [str(python), "-m", "playwright", "install", "chromium"],
            BROWSER_DOWNLOAD_TIMEOUT,
        )
        if not ok:
            report.add("chromium", False, detail)
            report.hint = (
                "The venv is ready but Chromium is missing. Re-run "
                "`hermes job-reach setup`, or install it manually:\n"
                f"  {python} -m playwright install chromium"
            )
            return report
        report.add("chromium", True, "playwright chromium downloaded")
    else:
        report.add("chromium", True, "skipped (--no-browser)")

    report.add("engine smoke test", *smoke_test())
    return report


def smoke_test() -> tuple[bool, str]:
    """Run ``jobreach doctor`` through the engine interpreter."""
    python, _ = resolve_interpreter()
    return _setup_step(
        [python, "-m", "jobreach", "doctor", "--json"],
        SMOKE_TEST_TIMEOUT,
        cwd=plugin_dir(),
        env=engine_env(),
    )


def _setup_step(
    argv: list[str],
    timeout: float,
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Run one setup command and turn any failure into a report line.

    Setup talks to the network, so "it hung" and "it could not start" are as
    likely as a non-zero exit. All three have to come back as a readable step
    result instead of an exception escaping into the agent's tool call.
    """
    try:
        result = run_captured(argv, timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {int(timeout)}s: {' '.join(argv)}"
    except OSError as exc:
        return False, f"could not start {argv[0]}: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[:500]
    return True, (result.stdout or "").strip()[:300]


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def probe_browser_support(python: str, *, timeout: float = 60.0) -> tuple[bool, str]:
    """Ask *python* whether it can import the Playwright stack.

    The probe runs in a subprocess on purpose. ``doctor`` is frequently executed
    by an interpreter that is *not* the engine interpreter — Hermes' own Python,
    or a system ``python3`` — and reporting that interpreter's missing
    Playwright would be a lie about what the engine can actually do.
    """
    try:
        proc = run_captured(
            [python, "-c", "import playwright, playwright_stealth"], timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run {python}: {exc}"
    if proc.returncode == 0:
        return True, ""
    lines = (proc.stderr or "").strip().splitlines()
    detail = lines[-1] if lines else f"exit code {proc.returncode}"
    return False, f"{detail}\n{PLAYWRIGHT_HINT}"


def backend_report() -> dict[str, Any]:
    """Describe the backends: which one a scrape would use, and why.

    Never raises: a missing backend is a *finding* here (``doctor`` exists to
    report exactly that), not an error.
    """
    from .fetchers import backend_preference, select_backend

    scrapling = scrapling_status()
    python, _ = resolve_interpreter()
    playwright_ok, playwright_problem = probe_browser_support(python)

    payload: dict[str, Any] = {
        "preference": backend_preference(),
        "scrapling": scrapling,
        "playwright": {
            "ready": playwright_ok,
            "python": python,
            "problem": playwright_problem,
        },
        "selected": None,
        "problem": "",
        "hint": "",
    }
    try:
        backend = select_backend()
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        payload["problem"] = str(exc)
        payload["hint"] = getattr(exc, "hint", "")
    else:
        payload["selected"] = backend.to_dict()
    return payload


def diagnostics() -> dict[str, Any]:
    """Describe the runtime: paths, interpreter, backends, board readiness."""
    from .config import default_db_path, find_vault
    from .scrapers import SCRAPERS, is_cli_scraper, needs_browser

    python, source = resolve_interpreter()
    db = default_db_path()
    vault = find_vault()
    backends = backend_report()
    backend_ready = backends["selected"] is not None

    boards: dict[str, dict[str, Any]] = {}
    for name, scraper_class in SCRAPERS.items():
        if is_cli_scraper(name):
            executable = scraper_class.cli_base[0] if scraper_class.cli_base else ""
            missing = bool(executable) and shutil.which(executable) is None
            boards[name] = {
                "ready": not missing,
                "backend": "cli",
                "needs_browser": False,
                "problem": (
                    f"{executable!r} is not on PATH\n{scraper_class.install_hint}"
                    if missing
                    else None
                ),
            }
        elif not needs_browser(name):
            boards[name] = {
                "ready": True,
                "backend": "http",
                "needs_browser": False,
                "problem": None,
                "note": scraper_class.http_note,
            }
        else:
            boards[name] = {
                "ready": backend_ready,
                "backend": (backends["selected"] or {}).get("name") or "none",
                "needs_browser": True,
                "problem": None if backend_ready else (backends["problem"] or "no browser backend"),
            }

    return {
        "plugin_version": __version__,
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "engine_interpreter": python,
        "engine_interpreter_source": source,
        "browser_ready": backend_ready,
        "backend": backends,
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

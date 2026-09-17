"""Locate and drive a Scrapling installation.

Scrapling is not a dependency of this plugin — it is a *capability we look for*.
That is deliberate: the plugin must keep working on a machine that has only the
standard library (the read-only tools), on one that has the plugin's own
Playwright venv, and on one (like the author's) that already runs Scrapling for
its MCP server. Looking for what is present is cheaper and more honest than
declaring a new mandatory dependency.

Resolution order for the interpreter that provides Scrapling:

1. ``$JOBREACH_SCRAPLING_PYTHON`` — explicit operator override, always wins.
2. The ``scrapling`` executable on ``PATH`` → the ``bin/python`` beside it.
3. The plugin's own venv (``$JOBREACH_HOME/venv``) — used when ``job_setup``
   installed Scrapling there.
4. Well-known local installs, including the layout used for Agent Reach / MCP
   servers (``~/MCP_for_AI/Scrapling/venv``).

Each candidate is *probed* — ``import scrapling; print(__version__)`` in a
subprocess — because a path that exists is not a path that works, and probing
in-process would import a foreign package into Hermes' own runtime.

The driver itself (:mod:`jobreach.drivers.scrapling_driver`) receives a JSON
spec on stdin and answers with a single marked JSON line, so Scrapling's own
log chatter on stdout cannot corrupt the result.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import MissingDependencyError, ScraperError
from .logging_setup import get_logger
from .platforms import venv_python
from .proc import run_captured

logger = get_logger("scrapling")

#: Environment variable naming the interpreter that has Scrapling installed.
SCRAPLING_PYTHON_ENV = "JOBREACH_SCRAPLING_PYTHON"

#: Marker prepended to the driver's result line, so log noise cannot break it.
RESULT_MARKER = "__JOBREACH_RESULT__ "

#: Interpreter probe timeout. Importing Scrapling pulls in lxml and the browser
#: toolbelt; a slow cold start on a network mount is still a second or two.
PROBE_TIMEOUT = 60.0

#: Extra seconds added to the fetch timeout when waiting on the driver, so the
#: driver's own (better) error message wins the race against our kill.
DRIVER_GRACE = 30.0

#: Where a Scrapling install is likely to live when the user did not tell us.
#: The MCP layout is first because that is how the plugin's own author runs it.
KNOWN_VENVS: tuple[str, ...] = (
    "~/MCP_for_AI/Scrapling/venv",
    "~/.local/share/scrapling/venv",
    "~/.scrapling/venv",
)

INSTALL_HINT = (
    "Scrapling provides the stealth browser this board needs:\n"
    "  uv tool install 'scrapling[fetchers]' && scrapling install\n"
    "then point the plugin at it if it is not on PATH:\n"
    f"  {SCRAPLING_PYTHON_ENV}=/path/to/venv/Scripts/python.exe   (Windows)\n"
    f"  {SCRAPLING_PYTHON_ENV}=/path/to/venv/bin/python           (macOS/Linux)"
)

PLAYWRIGHT_FALLBACK_HINT = (
    "Install a browser backend:\n"
    "  hermes job-reach setup            # plugin venv + Playwright + Chromium (~150 MB)\n"
    "or use Scrapling if it is already installed:\n"
    f"  {SCRAPLING_PYTHON_ENV}=/path/to/venv/<Scripts|bin>/python\n"
    "Note: the four browser-free boards (wantedly, green, daijob, japandev) "
    "work without any of this."
)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def driver_path() -> Path:
    """Absolute path of the shipped fetch driver."""
    return Path(__file__).resolve().parent / "drivers" / "scrapling_driver.py"


def _candidate_pythons() -> list[tuple[str, str]]:
    """Candidate interpreters with the rule that produced each one.

    Paths follow the platform's virtualenv layout (``bin`` vs ``Scripts``), so a
    Windows install is found by the same logic as a POSIX one.
    """
    candidates: list[tuple[str, str]] = []

    override = os.environ.get(SCRAPLING_PYTHON_ENV, "").strip()
    if override:
        candidates.append((str(Path(override).expanduser()), SCRAPLING_PYTHON_ENV))

    executable = shutil.which("scrapling")
    if executable:
        # A console script lives beside its interpreter (``bin``/``Scripts``).
        # Resolve the symlink first: uv and pipx shim the binary.
        candidates.append((str(venv_python(Path(executable).resolve().parent.parent)), "scrapling on PATH"))

    from .config import jobreach_home

    candidates.append((str(venv_python(jobreach_home() / "venv")), "plugin venv"))

    for template in KNOWN_VENVS:
        candidates.append(
            (str(venv_python(Path(template).expanduser())), f"known install {template}")
        )

    return candidates


@dataclass(slots=True)
class Probe:
    """Outcome of asking an interpreter whether it can run Scrapling."""

    ok: bool
    detail: str
    version: str = ""


#: Probe results, keyed by interpreter path. Scraping runs several times per
#: session (one probe per scrape at worst), and importing Scrapling is the
#: expensive part — a process-lifetime cache is worth it.
_PROBE_CACHE: dict[str, Probe] = {}

PROBE_CODE = "import scrapling; print(scrapling.__version__)"


def probe(python: str, *, use_cache: bool = True) -> Probe:
    """Ask *python* whether it can import Scrapling.

    The probe runs in a **subprocess**: importing a foreign package into Hermes'
    own runtime is exactly the kind of thing this plugin exists not to do.
    """
    if use_cache and python in _PROBE_CACHE:
        return _PROBE_CACHE[python]

    if not Path(python).exists():
        result = Probe(False, "interpreter does not exist")
    else:
        try:
            completed = run_captured([python, "-c", PROBE_CODE], timeout=PROBE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = Probe(False, f"could not run {python}: {exc}")
        else:
            version = (completed.stdout or "").strip().splitlines()
            if completed.returncode == 0 and version:
                result = Probe(True, f"Scrapling {version[-1]}", version=version[-1])
            else:
                tail = ((completed.stderr or "").strip().splitlines() or ["import failed"])[-1]
                result = Probe(False, tail[:200])

    if use_cache:
        _PROBE_CACHE[python] = result
    return result


def scrapling_python() -> tuple[str | None, str]:
    """Return ``(interpreter, source)`` for the first working Scrapling install.

    ``(None, "")`` means nothing usable was found — the caller then either falls
    back to Playwright or reports a dependency error with a fix.
    """
    for python, source in _candidate_pythons():
        if probe(python).ok:
            return python, source
    return None, ""


def available() -> bool:
    """Whether any interpreter can run Scrapling."""
    return scrapling_python()[0] is not None


def version_of(python: str | None = None) -> str:
    """Scrapling version string, or ``""`` when unavailable."""
    interpreter = python or scrapling_python()[0]
    if not interpreter:
        return ""
    return probe(interpreter).version


# --------------------------------------------------------------------------- #
# Running the driver
# --------------------------------------------------------------------------- #


def driver_env() -> dict[str, str]:
    """Environment for the driver process.

    ``PYTHONPATH`` is dropped rather than set: the driver imports Scrapling and
    nothing of ours, and an inherited ``PYTHONPATH`` (Hermes sets one) has a
    habit of shadowing packages inside foreign interpreters.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    env["JOBREACH_PLUGIN_DIR"] = str(Path(__file__).resolve().parent.parent)
    return env


def run_driver(
    spec: dict,
    *,
    timeout: float | None = None,
    python: str | None = None,
) -> dict:
    """Run the fetch driver with *spec* and return its JSON payload.

    Raises:
        MissingDependencyError: no interpreter with Scrapling.
        ScraperError: the driver crashed, timed out, or answered with a failure.
    """
    interpreter = python or scrapling_python()[0]
    if not interpreter:
        raise MissingDependencyError(
            "no interpreter with Scrapling installed was found", hint=INSTALL_HINT
        )

    script = driver_path()
    if not script.exists():  # pragma: no cover - packaging error
        raise ScraperError(f"the Scrapling driver is missing at {script}")

    budget = float(spec.get("timeout_ms", 90_000)) / 1000.0
    total_timeout = timeout if timeout is not None else budget + DRIVER_GRACE

    logger.info(
        "fetching with scrapling",
        extra={"url": spec.get("url"), "mode": spec.get("mode"), "python": interpreter},
    )
    try:
        completed = run_captured(
            [interpreter, str(script)],
            timeout=total_timeout,
            stdin=json.dumps(spec, ensure_ascii=False),
            env=driver_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ScraperError(
            f"the browser did not finish within {int(total_timeout)}s "
            f"({spec.get('mode')} fetch of {spec.get('url')})"
        ) from exc
    except OSError as exc:
        raise MissingDependencyError(
            f"could not start {interpreter}: {exc}", hint=INSTALL_HINT
        ) from exc

    payload = _extract_payload(completed.stdout or "")
    if payload is None:
        detail = (completed.stderr or completed.stdout or "").strip()[-1200:]
        raise ScraperError(
            f"the Scrapling driver produced no result (exit {completed.returncode}): {detail}"
        )

    if not payload.get("ok"):
        raise ScraperError(str(payload.get("error") or "the fetch failed"))
    return payload


def _extract_payload(stdout: str) -> dict | None:
    """Pull the marked JSON line out of the driver's stdout.

    Scrapling logs through its own handler, and a browser can write to stdout
    too (a crashed Chromium has been known to). Reading the *last* marked line
    means noise can never be mistaken for the result.
    """
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                payload = json.loads(line[len(RESULT_MARKER):])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return None

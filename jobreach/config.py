"""Configuration and on-disk locations.

Everything is derived from environment variables with sane defaults, so the
plugin works immediately after ``hermes plugins install`` with no config file
at all. The one hard rule: **user data never lives inside the plugin
directory**, because ``hermes plugins update`` replaces that directory.

Layout::

    $JOBREACH_HOME/                       (default: $HERMES_HOME/plugin-data/job-reach)
    ├── jobs.db                            listings + run history
    └── venv/                              optional venv with playwright installed

``$HERMES_HOME/plugin-data/<plugin>/`` is Hermes' sanctioned writable state
directory for plugins — it survives plugin updates by design.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Plugin id, used to derive the state directory.
PLUGIN_ID = "job-reach"

#: Wantedly location slug used when the caller does not specify one.
DEFAULT_LOCATION = "tokyo"

#: Boards enabled for a default (no-argument) search, in display order.
#: Indeed is absent on purpose: it has no scraper (see ``SourcePlatform``).
DEFAULT_SOURCES = ("wantedly", "mynavi2027", "linkedin")

#: Where Obsidian notes are written, relative to the vault root.
NOTE_SUBFOLDER = "job-searches"

_VALID_SOURCES = ("wantedly", "mynavi2027", "linkedin", "indeed")

#: Places searched for an Obsidian vault when none is configured.
_VAULT_CANDIDATES = ("Obsidian/Adi", "Documents/Obsidian Vault", "Obsidian")


def hermes_home() -> Path:
    """Return Hermes' own home directory (``$HERMES_HOME`` or ``~/.hermes``)."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def jobreach_home() -> Path:
    """Return Job Reach's data directory, creating it if necessary.

    Precedence: ``$JOBREACH_HOME`` → ``$HERMES_HOME/plugin-data/job-reach``
    → ``~/.hermes/plugin-data/job-reach``.
    """
    raw = os.environ.get("JOBREACH_HOME", "").strip()
    home = Path(raw).expanduser() if raw else hermes_home() / "plugin-data" / PLUGIN_ID
    home.mkdir(parents=True, exist_ok=True)
    return home


def plugin_dir() -> Path:
    """Absolute path of the installed plugin (this file's grandparent)."""
    return Path(__file__).resolve().parent.parent


def default_db_path() -> Path:
    """Return the SQLite file used when the caller passes no ``--db``."""
    raw = os.environ.get("JOBREACH_DB", "").strip()
    if raw:
        return Path(raw).expanduser()
    return jobreach_home() / "jobs.db"


def find_vault() -> Path | None:
    """Locate the Obsidian vault, or ``None`` when nothing is configured.

    Honours ``$OBSIDIAN_VAULT_PATH`` first, then probes the well-known
    locations. A directory counts as a vault only if it contains ``.obsidian``
    (or is empty, which means the user created it deliberately).
    """
    raw = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    for candidate in _VAULT_CANDIDATES:
        path = Path.home() / candidate
        if (path / ".obsidian").is_dir():
            return path
    return None


def python_override() -> str | None:
    """Interpreter explicitly chosen by the user, if any."""
    raw = os.environ.get("JOBREACH_PYTHON", "").strip()
    return raw or None


@dataclass(frozen=True, slots=True)
class Sources:
    """Normalised, validated selection of job boards for one run.

    Kept as a tiny value object so the same parsing rules are shared by the
    CLI, the Hermes tools, and the tests.
    """

    names: tuple[str, ...]

    @classmethod
    def parse(cls, value: str | list[str] | tuple[str, ...] | None = None) -> Sources:
        """Parse ``"all"``, ``"wantedly,linkedin"``, or a list of board names.

        ``None`` selects :data:`DEFAULT_SOURCES`, which is also what makes this
        usable as a dataclass ``default_factory``.

        Raises:
            ValueError: on an unknown board name.
        """
        if value is None:
            return cls(DEFAULT_SOURCES)
        parts = value.split(",") if isinstance(value, str) else list(value)
        chosen: list[str] = []
        for part in parts:
            name = str(part).strip().lower()
            if not name:
                continue
            if name in {"all", "*"}:
                for candidate in _VALID_SOURCES:
                    if candidate not in chosen:
                        chosen.append(candidate)
                continue
            if name not in _VALID_SOURCES:
                valid = ", ".join(_VALID_SOURCES)
                raise ValueError(f"unknown source {name!r}; valid: {valid}, all")
            if name not in chosen:
                chosen.append(name)
        if not chosen:
            raise ValueError("no valid source selected")
        return cls(tuple(chosen))

    def __iter__(self):
        return iter(self.names)

    def __contains__(self, item: object) -> bool:
        return item in self.names

    def __len__(self) -> int:
        return len(self.names)

    def as_list(self) -> list[str]:
        return list(self.names)

    @property
    def scraped(self) -> tuple[str, ...]:
        """Boards that have a scraper (every board the plugin ships today).

        Boards without one used to be listed in the scrapers package's
        ``INGEST_ONLY``; nothing needs ingesting-only today, but the hook stays
        so a future board can be marked that way in one place.
        """
        from .scrapers import INGEST_ONLY

        return tuple(name for name in self.names if name not in INGEST_ONLY)

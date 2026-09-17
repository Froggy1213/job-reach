"""Persistence: the ``JobRepository`` port and its SQLite adapter.

The original project used SQLAlchemy + aiosqlite. That was replaced by the
standard library's ``sqlite3`` for three concrete reasons, all of them about
being a good Hermes plugin:

1. **Zero install surface.** Hermes runs plugins inside its own runtime venv
   (Python 3.14 today). The core must import there without pip-installing
   anything, so a plugin update can never break Hermes itself.
2. **One process, one file.** This is a personal job tracker on SQLite. An ORM
   bought us nothing but a dependency.
3. **Honest API.** ``sqlite3`` calls are synchronous, so the port is
   synchronous too, instead of pretending an OSCK driver is async.

The architecture is unchanged in spirit: application code talks to the
:class:`JobRepository` abstraction and never sees SQL. Swapping in Postgres
later means writing one more adapter, not touching the pipeline.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from .domain import JobPosting, SourcePlatform
from .errors import StoreError

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    url             TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location        TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    salary          TEXT,
    description     TEXT,
    posted_at       TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    times_seen      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jobs_source     ON jobs(source_platform);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company    ON jobs(company);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,
    keyword     TEXT,
    location    TEXT,
    sources     TEXT,
    total       INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    saved       INTEGER NOT NULL DEFAULT 0,
    errors      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
"""


def normalize_url(url: str) -> str:
    """Normalise a listing URL into its deduplication key.

    Lowercases scheme and host, drops the fragment, strips a trailing slash —
    but **keeps the query string**, because some boards (Indeed, whose id
    lives in ``?jk=``) identify a listing entirely by a query parameter.
    Boards whose id is in the path (Wantedly, Mynavi, LinkedIn) already strip
    the query before it reaches here, so keeping it is a no-op for them.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            parsed.query,
            "",
        )
    )


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One search/ingest execution, kept so monitoring runs can report deltas."""

    id: int
    started_at: str
    finished_at: str | None
    mode: str
    keyword: str | None
    location: str | None
    sources: list[str]
    total: int
    new: int
    saved: int
    errors: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "mode": self.mode,
            "keyword": self.keyword,
            "location": self.location,
            "sources": self.sources,
            "total": self.total,
            "new": self.new,
            "saved": self.saved,
            "errors": self.errors,
        }


class JobRepository(ABC):
    """Port: everything the pipeline needs from a store. Adapters implement it."""

    @abstractmethod
    def existing_urls(self, urls: Iterable[str]) -> set[str]:
        """Return the subset of *urls* already present (i.e. seen in a prior run)."""

    @abstractmethod
    def save_many(self, jobs: Iterable[JobPosting]) -> int:
        """Upsert postings; return how many were genuinely new rows."""

    @abstractmethod
    def query(
        self,
        *,
        text: str | None = None,
        source: SourcePlatform | None = None,
        limit: int = 50,
        offset: int = 0,
        new_since: datetime | None = None,
    ) -> list[JobPosting]:
        """Return stored postings, newest first, optionally filtered."""

    @abstractmethod
    def count(self, *, source: SourcePlatform | None = None) -> int:
        """Total number of stored postings (optionally for one board)."""

    @abstractmethod
    def stats(self) -> dict[str, Any]:
        """Counts by board plus first/last-seen timestamps."""

    @abstractmethod
    def start_run(self, mode: str, keyword: str | None, location: str | None,
                  sources: Iterable[str]) -> int:
        """Record the start of a run and return its id."""

    @abstractmethod
    def finish_run(self, run_id: int, *, total: int, new: int, saved: int,
                   errors: dict[str, str]) -> None:
        """Record the outcome of a run."""

    @abstractmethod
    def recent_runs(self, limit: int = 10) -> list[RunRecord]:
        """Return the most recent runs, newest first."""

    @abstractmethod
    def close(self) -> None:
        """Release the underlying connection."""


class SQLiteJobRepository(JobRepository):
    """SQLite adapter. Not thread-safe: create one per task/process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover - environment failure
            raise StoreError(
                f"cannot open database {self.path}: {exc}\n"
                f"Check that {self.path.parent} exists and is writable, or point "
                f"JOBREACH_DB at a directory you own."
            ) from exc

    # -- reads ---------------------------------------------------------------

    def existing_urls(self, urls: Iterable[str]) -> set[str]:
        keys = [normalize_url(u) for u in urls]
        if not keys:
            return set()
        found: set[str] = set()
        try:
            # Chunked to stay well under SQLite's variable limit.
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT url FROM jobs WHERE url IN ({placeholders})", chunk
                ).fetchall()
                found.update(row["url"] for row in rows)
        except sqlite3.Error as exc:
            raise StoreError(f"existing_urls failed: {exc}") from exc
        return found

    def query(
        self,
        *,
        text: str | None = None,
        source: SourcePlatform | None = None,
        limit: int = 50,
        offset: int = 0,
        new_since: datetime | None = None,
    ) -> list[JobPosting]:
        where: list[str] = []
        params: list[Any] = []
        if text:
            where.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)")
            needle = f"%{text.strip()}%"
            params.extend([needle, needle, needle])
        if source is not None:
            where.append("source_platform = ?")
            params.append(str(source))
        if new_since is not None:
            where.append("first_seen_at >= ?")
            params.append(_iso(new_since))
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY first_seen_at DESC, title ASC LIMIT ? OFFSET ?"
        params.extend([max(1, int(limit)), max(0, int(offset))])
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"query failed: {exc}") from exc
        return [_row_to_job(row) for row in rows]

    def count(self, *, source: SourcePlatform | None = None) -> int:
        try:
            if source is None:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE source_platform = ?",
                    (str(source),),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"count failed: {exc}") from exc
        return int(row["n"])

    def stats(self) -> dict[str, Any]:
        try:
            by_platform = {
                row["source_platform"]: row["n"]
                for row in self._conn.execute(
                    "SELECT source_platform, COUNT(*) AS n FROM jobs "
                    "GROUP BY source_platform ORDER BY n DESC"
                )
            }
            window = self._conn.execute(
                "SELECT MIN(first_seen_at) AS oldest, MAX(first_seen_at) AS newest FROM jobs"
            ).fetchone()
            last_run = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"stats failed: {exc}") from exc
        return {
            "total": sum(by_platform.values()),
            "by_platform": by_platform,
            "oldest_first_seen": window["oldest"],
            "newest_first_seen": window["newest"],
            "last_run": _row_to_run(last_run).to_dict() if last_run else None,
            "database": str(self.path),
        }

    # -- writes --------------------------------------------------------------

    def save_many(self, jobs: Iterable[JobPosting]) -> int:
        """Insert new postings and bump ``times_seen`` for ones already known.

        Returns the number of rows that did **not** exist before this call —
        which is exactly the "new since last run" count.
        """
        now = _iso(datetime.now(UTC))
        inserted = 0
        try:
            with self._conn:  # one transaction per batch
                for job in jobs:
                    # RETURNING lets one statement both upsert and tell us
                    # whether the row was fresh: a brand-new row always comes
                    # back with times_seen == 1, an updated one with >= 2.
                    row = self._conn.execute(
                        """
                        INSERT INTO jobs (url, title, company, location, source_platform,
                                          salary, description, posted_at,
                                          first_seen_at, last_seen_at, times_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(url) DO UPDATE SET
                            last_seen_at = excluded.last_seen_at,
                            times_seen   = jobs.times_seen + 1,
                            title        = excluded.title,
                            company      = excluded.company,
                            location     = excluded.location,
                            salary       = COALESCE(excluded.salary, jobs.salary)
                        RETURNING times_seen
                        """,
                        (
                            normalize_url(job.url),
                            job.title,
                            job.company,
                            job.location,
                            job.platform,
                            job.salary,
                            job.description,
                            _iso(job.posted_at) if job.posted_at else None,
                            now,
                            now,
                        ),
                    ).fetchone()
                    if row is not None and int(row["times_seen"]) == 1:
                        inserted += 1
        except sqlite3.Error as exc:
            raise StoreError(f"save_many failed: {exc}") from exc
        return inserted

    def start_run(self, mode: str, keyword: str | None, location: str | None,
                  sources: Iterable[str]) -> int:
        try:
            with self._conn:
                cursor = self._conn.execute(
                    "INSERT INTO runs (started_at, mode, keyword, location, sources) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        _iso(datetime.now(UTC)),
                        mode,
                        keyword,
                        location,
                        json.dumps(list(sources), ensure_ascii=False),
                    ),
                )
            return int(cursor.lastrowid or 0)
        except sqlite3.Error as exc:
            raise StoreError(f"start_run failed: {exc}") from exc

    def finish_run(self, run_id: int, *, total: int, new: int, saved: int,
                   errors: dict[str, str]) -> None:
        if not run_id:
            return
        try:
            with self._conn:
                self._conn.execute(
                    "UPDATE runs SET finished_at = ?, total = ?, new = ?, saved = ?, errors = ? "
                    "WHERE id = ?",
                    (
                        _iso(datetime.now(UTC)),
                        int(total),
                        int(new),
                        int(saved),
                        json.dumps(errors, ensure_ascii=False),
                        int(run_id),
                    ),
                )
        except sqlite3.Error as exc:
            raise StoreError(f"finish_run failed: {exc}") from exc

    def recent_runs(self, limit: int = 10) -> list[RunRecord]:
        try:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"recent_runs failed: {exc}") from exc
        return [_row_to_run(row) for row in rows]

    def close(self) -> None:
        # Closing twice is harmless; a failure here must never mask the real
        # error a caller is already handling.
        with suppress(sqlite3.Error):
            self._conn.close()

    def __enter__(self) -> SQLiteJobRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Row mapping
# --------------------------------------------------------------------------- #


def _iso(value: datetime) -> str:
    """Serialise a datetime as UTC ISO-8601 (a stable, sortable string)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _row_to_job(row: sqlite3.Row) -> JobPosting:
    return JobPosting(
        title=row["title"],
        company=row["company"],
        url=row["url"],
        location=row["location"],
        source_platform=SourcePlatform(row["source_platform"]),
        description=row["description"],
        salary=row["salary"],
        posted_at=_parse_dt(row["posted_at"]),
        scraped_at=_parse_dt(row["first_seen_at"]) or datetime.now(UTC),
    )


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    try:
        sources = json.loads(row["sources"] or "[]")
    except (TypeError, ValueError):
        sources = []
    try:
        errors = json.loads(row["errors"] or "{}")
    except (TypeError, ValueError):
        errors = {}
    return RunRecord(
        id=int(row["id"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        mode=row["mode"],
        keyword=row["keyword"],
        location=row["location"],
        sources=list(sources),
        total=int(row["total"] or 0),
        new=int(row["new"] or 0),
        saved=int(row["saved"] or 0),
        errors=dict(errors),
    )


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def open_repository(path: str | Path) -> JobRepository:
    """Open the default SQLite repository at *path*."""
    return SQLiteJobRepository(path)

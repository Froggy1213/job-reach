"""Domain model: the single representation of a job listing.

``JobPosting`` is a frozen dataclass, not a Pydantic model. That is a
deliberate change from the original Telegram-bot project: the core is now
standard-library-only so Hermes can import and run it without installing
anything into its own runtime venv (Python 3.14, where third-party binary
wheels such as ``pydantic_core`` may not exist yet).

Validation is explicit and cheap, which is all a scraper pipeline needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

MAX_TITLE = 512
MAX_COMPANY = 256
MAX_LOCATION = 256
MAX_SALARY = 256
MAX_URL = 2048
MAX_DESCRIPTION = 10_000


class SourcePlatform(StrEnum):
    """Identifies which job board a :class:`JobPosting` came from.

    A single enum shared by the domain model, the storage layer, the
    scrapers, and the Hermes tools — so adding a board means adding exactly
    one member here plus one scraper class (see ``scrapers/__init__.py``).
    """

    WANTEDLY = "wantedly"
    """Wantedly — startup/tech project board, keyword + location search."""

    MYNAVI_2027 = "mynavi_2027"
    """Mynavi 2027 新卒 — new-graduate board, scraped by occupation code."""

    LINKEDIN = "linkedin"
    """LinkedIn Jobs — fetched through the ``opencli linkedin`` CLI."""

    INDEED = "indeed"
    """Indeed Japan — the widest market; scraped with a stealth browser, and
    the one board that can be bot-blocked (see ``job_ingest`` for the fallback)."""

    GREEN = "green"
    """Green — IT/Web industry board; read from the JSON its pages embed."""

    DAIJOB = "daijob"
    """Daijob — bilingual / foreign-capital postings; server-rendered HTML."""

    JAPAN_DEV = "japan_dev"
    """Japan Dev — English-speaking tech jobs; server-rendered HTML."""


#: Human-facing labels, used in notes and tool output.
PLATFORM_LABELS: dict[SourcePlatform, str] = {
    SourcePlatform.WANTEDLY: "Wantedly",
    SourcePlatform.MYNAVI_2027: "Mynavi 2027",
    SourcePlatform.LINKEDIN: "LinkedIn",
    SourcePlatform.INDEED: "Indeed Japan",
    SourcePlatform.GREEN: "Green",
    SourcePlatform.DAIJOB: "Daijob",
    SourcePlatform.JAPAN_DEV: "Japan Dev",
}

#: Aliases accepted on the command line / in tool arguments.
PLATFORM_ALIASES: dict[str, SourcePlatform] = {
    "wantedly": SourcePlatform.WANTEDLY,
    "mynavi": SourcePlatform.MYNAVI_2027,
    "mynavi2027": SourcePlatform.MYNAVI_2027,
    "mynavi_2027": SourcePlatform.MYNAVI_2027,
    "linkedin": SourcePlatform.LINKEDIN,
    "indeed": SourcePlatform.INDEED,
    "green": SourcePlatform.GREEN,
    "greenjapan": SourcePlatform.GREEN,
    "green-japan": SourcePlatform.GREEN,
    "daijob": SourcePlatform.DAIJOB,
    "japandev": SourcePlatform.JAPAN_DEV,
    "japan_dev": SourcePlatform.JAPAN_DEV,
    "japan-dev": SourcePlatform.JAPAN_DEV,
}


def resolve_platform(name: str | SourcePlatform) -> SourcePlatform:
    """Map a user-supplied board name onto a :class:`SourcePlatform`.

    Raises:
        ValueError: if *name* is not a known board or alias.
    """
    if isinstance(name, SourcePlatform):
        return name
    key = str(name).strip().lower()
    try:
        return PLATFORM_ALIASES[key]
    except KeyError:
        valid = ", ".join(sorted(PLATFORM_ALIASES))
        raise ValueError(f"unknown source {name!r}; valid: {valid}") from None


class ValidationError(ValueError):
    """Raised when a job record is structurally unusable."""


@dataclass(frozen=True, slots=True)
class JobPosting:
    """One listing scraped (or ingested) from one job board.

    Frozen so it cannot be mutated while flowing through the pipeline.
    Use :func:`dataclasses.replace` when a modified copy is needed.
    """

    title: str
    company: str
    url: str
    location: str
    source_platform: SourcePlatform
    description: str | None = None
    salary: str | None = None
    posted_at: datetime | None = None
    scraped_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    #: Set by the pipeline, never by a scraper: ``True`` when this run was the
    #: first to see the listing. Not part of the persisted identity.
    is_new: bool = False

    #: ``True`` when the *scraper invented* the title rather than reading it off
    #: the listing, so it is evidence about the board's filing cabinet and not
    #: about the job. Mynavi is the case in point: it titles every card from the
    #: occupation code it searched ("<company> (WEBデザイナー)"), which makes the
    #: title useless — actively misleading — as filter input. The relevance
    #: filter then judges such a listing on its description alone.
    title_is_synthetic: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _clean(self.title, MAX_TITLE, "title"))
        object.__setattr__(self, "company", _clean(self.company, MAX_COMPANY, "company"))
        object.__setattr__(self, "location", _clean(self.location, MAX_LOCATION, "location"))
        object.__setattr__(self, "url", _clean_url(self.url))
        if not isinstance(self.source_platform, SourcePlatform):
            object.__setattr__(
                self, "source_platform", resolve_platform(self.source_platform)
            )
        object.__setattr__(self, "salary", _optional(self.salary, MAX_SALARY))
        object.__setattr__(self, "description", _optional(self.description, MAX_DESCRIPTION))

    def with_new_flag(self, is_new: bool) -> JobPosting:
        """Return a copy carrying the pipeline-computed ``is_new`` flag."""
        return replace(self, is_new=is_new)

    @property
    def platform(self) -> str:
        """The platform's string value — the form used in JSON and the DB."""
        return str(self.source_platform)

    @property
    def label(self) -> str:
        """Human-facing board name ("Wantedly", "Mynavi 2027", …)."""
        return PLATFORM_LABELS.get(self.source_platform, self.platform)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict — the stable wire format of the plugin."""
        return {
            "title": self.title,
            "company": self.company,
            "url": self.url,
            "location": self.location,
            "source_platform": self.platform,
            "source_label": self.label,
            "salary": self.salary,
            "is_new": self.is_new,
            "scraped_at": self.scraped_at.isoformat(),
            #: When the board published the listing, if it said so. None is
            #: common (LinkedIn and Mynavi do not expose it at all).
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
        }

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> JobPosting:
        """Build a posting from an untrusted dict (``job_ingest`` input).

        Only ``title`` and ``url`` are required; everything else degrades to
        a sensible default. Unknown keys are ignored.

        Raises:
            ValidationError: if the record is not a dict or lacks title/url.
        """
        if not isinstance(record, dict):
            raise ValidationError(f"expected a JSON object, got {type(record).__name__}")
        missing = [k for k in ("title", "url") if not record.get(k)]
        if missing:
            raise ValidationError(f"missing required field(s): {', '.join(missing)}")
        return cls(
            title=record["title"],
            company=record.get("company") or "Unknown",
            url=record["url"],
            location=record.get("location") or "Japan",
            source_platform=resolve_platform(record.get("source_platform") or "indeed"),
            description=record.get("description"),
            salary=record.get("salary"),
        )


def _clean(value: Any, limit: int, field_name: str) -> str:
    """Trim, truncate, and require a non-empty string."""
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{field_name} must not be empty")
    return text[:limit]


def _optional(value: Any, limit: int) -> str | None:
    """Trim and truncate an optional string, collapsing blanks to ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _clean_url(value: Any) -> str:
    """Validate a listing URL and drop its fragment."""
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(f"url must be an absolute http(s) URL, got {url!r}")
    return url.split("#", 1)[0][:MAX_URL]

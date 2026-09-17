"""Scraper registry — the one place that maps a board name to its strategy.

Adding a board is three edits and no more:

1. add a member to :class:`~jobreach.domain.SourcePlatform`
2. write the scraper class in this package
3. register it in :data:`SCRAPERS` below

The CLI, the pipeline, and the Hermes tools all read this registry, so nothing
else needs to learn about the new board.

Board selection also feeds the two capability questions the CLI asks before a
run: *does this board need a browser at all?* (Wantedly does not — it is read
over HTTP) and *is one available?* (:func:`check_source_requirements` only
probes the browser stack when a selected board actually needs it).
"""

from __future__ import annotations

from collections.abc import Callable

from ..domain import SourcePlatform
from . import base
from .base import BaseScraper
from .cli_base import CliScraper
from .indeed import IndeedScraper
from .linkedin import LinkedInScraper
from .mynavi2027 import Mynavi2027Scraper
from .wantedly import WantedlyScraper

#: Board name (as used on the CLI and in tool arguments) → scraper class.
SCRAPERS: dict[str, type[BaseScraper]] = {
    "wantedly": WantedlyScraper,
    "mynavi2027": Mynavi2027Scraper,
    "linkedin": LinkedInScraper,
    "indeed": IndeedScraper,
}

#: Boards that cannot be scraped and must be fed in via ``job_ingest``.
#:
#: Empty on purpose, and worth a note: Indeed Japan used to live here, because
#: Cloudflare blocked every headless client. A stealth browser now reads it, so
#: the board graduated to a scraper — and ``job_ingest`` remains as the fallback
#: for the runs automation cannot win.
INGEST_ONLY: dict[str, SourcePlatform] = {}

__all__ = [
    "BaseScraper",
    "CliScraper",
    "IndeedScraper",
    "LinkedInScraper",
    "Mynavi2027Scraper",
    "WantedlyScraper",
    "SCRAPERS",
    "INGEST_ONLY",
    "available_sources",
    "build_scrapers",
    "check_source_requirements",
    "is_cli_scraper",
    "needs_browser",
    "scraper_for",
]


def available_sources() -> list[str]:
    """Every board name accepted by ``--source``, scrapers first."""
    return [*SCRAPERS, *INGEST_ONLY]


def scraper_for(name: str) -> type[BaseScraper] | None:
    """Return the scraper class for *name*, or ``None`` if it is unknown."""
    return SCRAPERS.get(str(name).strip().lower())


def is_cli_scraper(name: str) -> bool:
    """Whether *name* is served by an external CLI rather than a browser."""
    cls: Callable[..., BaseScraper] | None = scraper_for(name)
    return bool(cls and issubclass(cls, CliScraper))


def needs_browser(name: str) -> bool:
    """Whether *name* requires a browser stack (CLI and HTTP boards do not)."""
    cls = scraper_for(name)
    if cls is None:
        return False
    return bool(cls.needs_browser) and not is_cli_scraper(name)


def build_scrapers(
    names: list[str] | tuple[str, ...],
    *,
    keyword: str | None = None,
    location: str | None = None,
    headless: bool = True,
    timeout_ms: int = 30_000,
) -> list[BaseScraper]:
    """Instantiate one scraper per requested board name.

    Raises:
        ValueError: if a name has no scraper.
    """
    scrapers: list[BaseScraper] = []
    for name in names:
        key = str(name).strip().lower()
        if key in INGEST_ONLY:
            raise ValueError(
                f"{key!r} has no scraper — fetch it with a real browser and push "
                f"the cards through the job_ingest tool instead."
            )
        try:
            scraper_class = SCRAPERS[key]
        except KeyError:
            valid = ", ".join(available_sources())
            raise ValueError(f"unknown source {key!r}; valid: {valid}") from None
        scrapers.append(
            scraper_class(
                headless=headless,
                timeout_ms=timeout_ms,
                keyword=keyword,
                location=location,
            )
        )
    return scrapers


def check_source_requirements(names: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Report which requested boards cannot run in this interpreter.

    Called by the CLI before a search so a missing browser stack fails
    immediately with a fix, instead of after every board has timed out.

    Only boards that actually need a browser are probed: Wantedly is read over
    HTTP and LinkedIn through ``opencli``, so neither may be blocked by an
    unrelated missing dependency. The probe is reached through the
    :mod:`~jobreach.scrapers.base` module rather than a name imported into this
    one, so backend selection has exactly one interception point —
    ``base.probe_browser_stack``. Binding the function directly here would
    silently ignore a patch applied at its definition site.

    Returns:
        Mapping of board name → human-readable problem. Empty means all good.
    """
    problems: dict[str, str] = {}
    browser_boards = [str(name).strip().lower() for name in names if needs_browser(str(name))]
    if not browser_boards:
        return problems
    try:
        base.probe_browser_stack()
    except Exception as exc:  # noqa: BLE001 — the hint is the whole point
        hint = getattr(exc, "hint", "") or str(exc)
        for name in browser_boards:
            problems[name] = hint
    return problems

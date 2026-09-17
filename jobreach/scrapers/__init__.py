"""Scraper registry — the one place that maps a board name to its strategy.

Adding a board is three edits and no more:

1. add a member to :class:`~jobreach.domain.SourcePlatform`
2. write the scraper class in this package
3. register it in :data:`SCRAPERS` below

The CLI, the pipeline, and the Hermes tools all read this registry, so nothing
else needs to learn about the new board.
"""

from __future__ import annotations

from collections.abc import Callable

from ..domain import SourcePlatform
from .base import BaseScraper, require_playwright
from .cli_base import CliScraper
from .linkedin import LinkedInScraper
from .mynavi2027 import Mynavi2027Scraper
from .wantedly import WantedlyScraper

#: Board name (as used on the CLI and in tool arguments) → scraper class.
SCRAPERS: dict[str, type[BaseScraper]] = {
    "wantedly": WantedlyScraper,
    "mynavi2027": Mynavi2027Scraper,
    "linkedin": LinkedInScraper,
}

#: Boards that cannot be scraped and must be fed in via ``job_ingest``.
INGEST_ONLY: dict[str, SourcePlatform] = {
    "indeed": SourcePlatform.INDEED,
}

__all__ = [
    "BaseScraper",
    "CliScraper",
    "LinkedInScraper",
    "Mynavi2027Scraper",
    "WantedlyScraper",
    "SCRAPERS",
    "INGEST_ONLY",
    "available_sources",
    "build_scrapers",
    "check_source_requirements",
]


def available_sources() -> list[str]:
    """Every board name accepted by ``--source``, scrapers first."""
    return [*SCRAPERS, *INGEST_ONLY]


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
        ValueError: if a name has no scraper (``indeed`` is ingest-only).
    """
    scrapers: list[BaseScraper] = []
    for name in names:
        key = str(name).strip().lower()
        if key in INGEST_ONLY:
            raise ValueError(
                f"{key!r} has no scraper — Cloudflare blocks headless clients. "
                f"Fetch it with a real browser and push the cards through "
                f"the job_ingest tool instead."
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

    Used by ``jobreach doctor`` and the ``job_doctor`` Hermes tool to fail
    loudly *before* a scrape instead of halfway through one.

    Returns:
        Mapping of board name → human-readable problem. Empty means all good.
    """
    problems: dict[str, str] = {}
    browser_boards = [
        str(name).strip().lower()
        for name in names
        if (cls := scraper_for(str(name))) is not None and not issubclass(cls, CliScraper)
    ]
    if not browser_boards:
        return problems
    try:
        require_playwright()
    except Exception as exc:  # noqa: BLE001 — the hint is the whole point
        hint = getattr(exc, "hint", "") or str(exc)
        for name in browser_boards:
            problems[name] = hint
    return problems


def scraper_for(name: str) -> type[BaseScraper] | None:
    """Return the scraper class for *name*, or ``None`` if it is ingest-only."""
    return SCRAPERS.get(str(name).strip().lower())


def is_cli_scraper(name: str) -> bool:
    """Whether *name* is served by an external CLI rather than Playwright."""
    cls: Callable[..., BaseScraper] | None = scraper_for(name)
    return bool(cls and issubclass(cls, CliScraper))

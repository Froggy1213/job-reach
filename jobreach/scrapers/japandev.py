"""Japan Dev (japan-dev.com) — English-speaking tech jobs in Japan.

Japan Dev lists roles at companies that hire without Japanese, which is the
board to reach for when the user wants the English-language market (it is the
same niche as TokyoDev, which needs a browser and a challenge token, so this one
is the cheap way in).

Its listing page is server-rendered by Nuxt: the first 60 postings are in the
HTML, and — this is the part worth knowing — **the search box filters on the
client**. ``?query=`` and ``?page=`` change nothing server-side (verified: three
different queries returned the identical set of 60 job URLs), so this scraper
downloads one page and applies the same relevance filter as every other board,
which means :attr:`url_encodes_keyword` is ``False``.

Verified card structure (``li.job-item``):

* ``a.job-item__title`` — the title, and its href is ``/jobs/<company>/<slug>``;
* ``div.job-item__contract-type`` — the company, as ``"Two Sigma・Science-based
  fintech"`` (the tagline separated by a Japanese middle dot);
* ``div.job__tag-desc`` — the location (``Tokyo``), when the card has one;
* tag lists carry the useful flags: ``Apply from Abroad``, ``Partial Remote``,
  ``No Japanese required``, plus the technologies.

The site shows salary on the detail page rather than the card, so ``salary``
stays ``None`` here — unlike Green and Indeed, where it is on the card.
"""

from __future__ import annotations

import asyncio

from ..domain import JobPosting, SourcePlatform
from ..errors import ScraperError
from ..htmlextract import cards, first_link, leaf_text, strip_tags
from ..logging_setup import get_logger
from ..webclient import HttpError, request_text
from .base import BaseScraper

logger = get_logger("scrapers.japandev")

BASE_URL = "https://japan-dev.com"
JOBS_URL = f"{BASE_URL}/jobs"

#: One listing per ``<li class="job-item">``.
CARD_TAG = "li"
CARD_CLASS = "job-item"

#: The server sends the first page of postings; there is no paging parameter.
MAX_LISTINGS = 60

#: The company tagline is appended after a Japanese middle dot.
_COMPANY_SEPARATOR = "・"

REQUEST_TIMEOUT = 30.0

#: Tags that are worth keeping in the description: they answer the questions a
#: job seeker actually asks (Japanese level, remote, relocation). Keys are
#: matched as case-insensitive substrings of the card text; the first entry
#: wins over the more general one it contains.
_FLAG_TAGS: tuple[tuple[str, str], ...] = (
    ("no japanese required", "No Japanese required"),
    ("business japanese", "Business Japanese"),
    ("fluent japanese", "Fluent Japanese"),
    ("japanese required", "Japanese required"),
    ("residents only", "Japan residents only"),
    ("apply from abroad", "Apply from abroad"),
    ("partial remote", "Partial remote"),
    ("no remote", "No remote"),
    ("visa support", "Visa support"),
    ("english required", "English required"),
)


class JapanDevScraper(BaseScraper):
    """Search the Japan Dev listing page."""

    url_encodes_keyword = False  # the site filters client-side; so do we
    needs_browser = False  # server-rendered HTML
    http_note = "server-rendered HTML, filtered here — no browser involved"

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.JAPAN_DEV

    def build_list_url(self) -> str:
        """The listing URL — no parameters, because the site ignores them."""
        return JOBS_URL

    async def fetch_jobs(self) -> list[JobPosting]:
        """Fetch the listing page once and parse every card."""
        try:
            html = await asyncio.to_thread(
                request_text, self.build_list_url(), timeout=REQUEST_TIMEOUT
            )
        except HttpError as exc:
            raise ScraperError(f"Japan Dev request failed: {exc}") from exc

        jobs = self._parse_cards(self.card_chunks(html))
        logger.info("Japan Dev search complete", extra={"jobs": len(jobs)})
        return jobs

    @staticmethod
    def card_chunks(html: str) -> list[str]:
        """Split the listing page into one HTML chunk per posting."""
        return cards(html, tag=CARD_TAG, class_contains=CARD_CLASS, limit=MAX_LISTINGS)

    # -- parsing -------------------------------------------------------------

    def _parse_cards(self, chunks: list[str]) -> list[JobPosting]:
        """Map card HTML onto domain objects, skipping malformed ones."""
        jobs: list[JobPosting] = []
        for chunk in chunks:
            try:
                link = first_link(chunk, href_contains="/jobs/", class_contains="job-item__title")
                if link is None:
                    continue
                href, title = link
                title = title.strip()
                if not title:
                    continue
                if not self.matches(title):
                    continue
                jobs.append(
                    JobPosting(
                        title=title,
                        company=self._company(chunk),
                        url=f"{BASE_URL}{href.split('?')[0]}",
                        location=self._location(chunk),
                        source_platform=self.platform,
                        description=self._description(chunk),
                    )
                )
            except Exception:  # noqa: BLE001 — malformed card, keep going
                logger.debug("skipping malformed Japan Dev card")
        return jobs

    @staticmethod
    def _company(chunk: str) -> str:
        """Company name, taken from the tagline line of the card.

        Read with :func:`leaf_text` rather than the depth-tracking parser: the
        site's SSR leaves elements unclosed, and a container read would swallow
        the rest of the card (verified: it appended the Apply button).
        """
        text = leaf_text(chunk, class_contains="job-item__contract-type")
        if not text:
            return "Unknown"
        name = text.split(_COMPANY_SEPARATOR, 1)[0].strip()
        return name or "Unknown"

    @staticmethod
    def _location(chunk: str) -> str:
        """The card's location chip, or a neutral default."""
        text = leaf_text(chunk, class_contains="job__tag-desc")
        return text.strip() or "Japan"

    @staticmethod
    def _description(chunk: str) -> str | None:
        """Keep the card's flags (they are the reason to use this board).

        Read from the card's whole text rather than one container: the tag
        lists nest, so a targeted lookup would only see the first tag.
        """
        body = strip_tags(chunk).lower()
        if not body:
            return None
        matched = {needle: label for needle, label in _FLAG_TAGS if needle in body}
        # "No Japanese required" contains "Japanese required": keep the
        # informative one and drop the substring it is a negation of.
        if "no japanese required" in matched:
            matched.pop("japanese required", None)
        flags = [label for needle, label in _FLAG_TAGS if needle in matched]
        return " · ".join(flags) if flags else None

"""Application-level exception hierarchy.

A single base class (``JobReachError``) lets a caller — the CLI, the Hermes
tool layer, or a test — catch every *expected* failure in one clause while
still being able to branch on the specific layer that failed.

Deliberately kept in the zero-dependency core: no third-party exception
types leak into this module.
"""

from __future__ import annotations


class JobReachError(Exception):
    """Base class for every expected Job Reach failure."""


class ConfigError(JobReachError):
    """Raised when configuration or the runtime environment is unusable."""


class ScraperError(JobReachError):
    """Raised when a scraper cannot fetch or parse a job board.

    The original exception is preserved as ``__cause__`` (usually a Playwright
    timeout, an HTTP error, or a bad CLI exit code).
    """


class MissingDependencyError(JobReachError):
    """A scraper needs an optional dependency that is not installed.

    Carries a ready-to-print ``hint`` so the Hermes agent can tell the user
    exactly which command to run instead of guessing.
    """

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class StoreError(JobReachError):
    """Raised when a database operation fails."""


class FilterError(JobReachError):
    """Raised when the relevance filter cannot run (e.g. missing API key)."""

"""Manual dependency injection container.

We intentionally avoid heavy DI frameworks.  All service wiring is
done explicitly in ``main.py`` -- every dependency assignment is
visible at the composition root, making the dependency graph
auditable without tooling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

from core.exceptions import ConfigurationError

if TYPE_CHECKING:
    from config.settings import Settings
    from database.repository import JobRepository
    from database.sqlalchemy_repository import SQLAlchemySubscriberRepository
    from scrapers.orchestrator import ScraperOrchestrator

_T = TypeVar("_T")


@dataclass
class Container:
    """Holds all application-wide service references.

    Built by ``main.py`` during startup and injected into aiogram
    handlers via ``ContainerMiddleware``.  Handlers access services
    through their ``container`` parameter.

    Fields start as ``None`` and are assigned in ``main.py`` so the
    container can be instantiated before all services are ready.
    """

    settings: Settings | None = None
    """Application configuration from environment/.env."""

    repository: JobRepository | None = None
    """Job posting repository (abstract interface)."""

    orchestrator: ScraperOrchestrator | None = None
    """Scraper orchestrator for manual scrape triggers."""

    subscriber_repository: SQLAlchemySubscriberRepository | None = None
    """Subscriber persistence (chat IDs for notifications)."""

    logger: logging.Logger | None = field(default=None, repr=False)
    """Configured root logger for the application."""

    @staticmethod
    def require(value: _T | None, name: str) -> _T:
        """Return *value* if it is not ``None``, else raise.

        Handlers should call this to get a clear error message when a
        dependency was not wired during startup::

            repo = Container.require(container.repository, "repository")
        """
        if value is None:
            raise ConfigurationError(
                f"Container.{name} is None — did you forget to wire it in main.py?"
            )
        return value


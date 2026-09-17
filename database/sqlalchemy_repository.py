"""Concrete SQLAlchemy implementation of ``JobRepository``.

Each method opens and closes its own ``AsyncSession`` so there is no
shared session state between calls.  This avoids the most common class
of async SQLAlchemy bugs (lazy loads after session close, stale
identity maps, accidental cross-request state leakage).

Model ↔ Domain mapping is handled by two private static methods,
keeping the translation logic co-located and testable in isolation.
"""

from __future__ import annotations

import logging

from pydantic import HttpUrl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exceptions import RepositoryError
from database.models import JobModel, SubscriberModel
from database.repository import JobRepository
from models.enums import SourcePlatform
from models.job_posting import JobPosting

logger = logging.getLogger("job_hunter.repository")


class SQLAlchemyJobRepository(JobRepository):
    """Persists ``JobPosting`` domain objects via SQLAlchemy async sessions.

    Args:
        session_factory: An ``async_sessionmaker`` bound to the async engine.
            Each method calls the factory to get a fresh session.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def save(self, job: JobPosting) -> JobPosting:
        """Persist a new job posting as a new ``JobModel`` row."""
        try:
            async with self._session_factory() as session:
                model = self._domain_to_model(job)
                session.add(model)
                await session.commit()
                await session.refresh(model)
                logger.debug(
                    "Saved job",
                    extra={"url": str(job.url), "id": model.id},
                )
                return self._model_to_domain(model)
        except Exception as exc:
            raise RepositoryError(f"Failed to save job '{job.url}': {exc}") from exc

    async def exists(self, url: str) -> bool:
        """Check existence by URL (the deduplication key)."""
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(JobModel.id).where(JobModel.url == url).limit(1)
                )
                return result.scalar_one_or_none() is not None
        except Exception as exc:
            raise RepositoryError(f"Failed to check existence for '{url}': {exc}") from exc

    async def get_all(self) -> list[JobPosting]:
        """Return all jobs, newest first."""
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(JobModel).order_by(JobModel.scraped_at.desc())
                )
                return [self._model_to_domain(row) for row in result.scalars().all()]
        except Exception as exc:
            raise RepositoryError(f"Failed to fetch all jobs: {exc}") from exc

    async def get_by_source(self, platform: SourcePlatform) -> list[JobPosting]:
        """Return jobs for a specific source platform, newest first."""
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(JobModel)
                    .where(JobModel.source_platform == platform)
                    .order_by(JobModel.scraped_at.desc())
                )
                return [self._model_to_domain(row) for row in result.scalars().all()]
        except Exception as exc:
            raise RepositoryError(
                f"Failed to fetch jobs for source '{platform}': {exc}"
            ) from exc

    async def get_jobs_page(
        self,
        limit: int,
        offset: int,
        source: SourcePlatform | None = None,
    ) -> list[JobPosting]:
        """Return a page of jobs, newest first, with optional source filter."""
        try:
            async with self._session_factory() as session:
                stmt = select(JobModel).order_by(JobModel.scraped_at.desc())
                if source is not None:
                    stmt = stmt.where(JobModel.source_platform == source)
                stmt = stmt.limit(limit).offset(offset)
                result = await session.execute(stmt)
                return [self._model_to_domain(row) for row in result.scalars().all()]
        except Exception as exc:
            raise RepositoryError(f"Failed to fetch jobs page: {exc}") from exc

    async def count_jobs(self, source: SourcePlatform | None = None) -> int:
        """Count jobs, with optional source filter."""
        try:
            async with self._session_factory() as session:
                stmt = select(func.count(JobModel.id))
                if source is not None:
                    stmt = stmt.where(JobModel.source_platform == source)
                result = await session.execute(stmt)
                return result.scalar_one()
        except Exception as exc:
            raise RepositoryError(f"Failed to count jobs: {exc}") from exc

    async def get_existing_urls(self, urls: list[str]) -> set[str]:
        """Return the subset of *urls* that already exist in the store."""
        if not urls:
            return set()
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(JobModel.url).where(JobModel.url.in_(urls))
                )
                return {row for row in result.scalars().all()}
        except Exception as exc:
            raise RepositoryError(f"Failed to check existing URLs: {exc}") from exc

    # ------------------------------------------------------------------
    # Model ↔ Domain mappers
    # ------------------------------------------------------------------

    @staticmethod
    def _model_to_domain(model: JobModel) -> JobPosting:
        """Map an ORM row back to a domain ``JobPosting``."""
        return JobPosting(
            title=model.title,
            company=model.company,
            url=HttpUrl(model.url),
            location=model.location,
            source_platform=model.source_platform,
            description=model.description,
            salary=model.salary,
            posted_at=model.posted_at,
            scraped_at=model.scraped_at,
        )

    @staticmethod
    def _domain_to_model(job: JobPosting) -> JobModel:
        """Map a domain ``JobPosting`` to a new ``JobModel`` row."""
        return JobModel(
            title=job.title,
            company=job.company,
            url=str(job.url),
            location=job.location,
            source_platform=job.source_platform,
            description=job.description,
            salary=job.salary,
            posted_at=job.posted_at,
            scraped_at=job.scraped_at,
        )

    async def save_many(self, jobs: list[JobPosting]) -> list[JobPosting]:
        """Persist multiple job postings in a single transaction."""
        if not jobs:
            return []
        try:
            async with self._session_factory() as session:
                models = [self._domain_to_model(j) for j in jobs]
                session.add_all(models)
                await session.commit()
                for m in models:
                    await session.refresh(m)
                logger.debug(
                    "Batch saved jobs",
                    extra={"count": len(models)},
                )
                return [self._model_to_domain(m) for m in models]
        except Exception as exc:
            raise RepositoryError(f"Failed to batch save {len(jobs)} jobs: {exc}") from exc


# ---------------------------------------------------------------------------
# Subscriber repository
# ---------------------------------------------------------------------------


class SQLAlchemySubscriberRepository:
    """Persists Telegram subscriber chat IDs via SQLAlchemy async sessions.

    Args:
        session_factory: An ``async_sessionmaker`` bound to the async engine.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_subscriber(self, chat_id: int) -> bool:
        """Add a chat ID to the subscribers table.

        Returns:
            ``True`` if the chat was newly subscribed, ``False`` if it
            was already subscribed.
        """
        try:
            async with self._session_factory() as session:
                existing = await session.get(SubscriberModel, chat_id)
                if existing is not None:
                    return False
                session.add(SubscriberModel(chat_id=chat_id))
                await session.commit()
                logger.info("New subscriber added", extra={"chat_id": chat_id})
                return True
        except Exception as exc:
            raise RepositoryError(f"Failed to add subscriber {chat_id}: {exc}") from exc

    async def remove_subscriber(self, chat_id: int) -> bool:
        """Remove a chat ID from the subscribers table.

        Returns:
            ``True`` if the chat was unsubscribed, ``False`` if it wasn't
            subscribed.
        """
        try:
            async with self._session_factory() as session:
                existing = await session.get(SubscriberModel, chat_id)
                if existing is None:
                    return False
                await session.delete(existing)
                await session.commit()
                logger.info("Subscriber removed", extra={"chat_id": chat_id})
                return True
        except Exception as exc:
            raise RepositoryError(
                f"Failed to remove subscriber {chat_id}: {exc}"
            ) from exc

    async def get_all_subscribers(self) -> list[int]:
        """Return all subscribed chat IDs."""
        try:
            async with self._session_factory() as session:
                result = await session.execute(select(SubscriberModel.chat_id))
                return [row for row in result.scalars().all()]
        except Exception as exc:
            raise RepositoryError(f"Failed to fetch subscribers: {exc}") from exc

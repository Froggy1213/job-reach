"""Shared test fixtures.

Provides:
- An in-memory SQLite repository for integration tests.
- A Settings instance with fake tokens for unit tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import Settings
from database.models import Base
from database.sqlalchemy_repository import SQLAlchemyJobRepository


@pytest.fixture
def settings() -> Settings:
    """Return a Settings instance with fake values for testing."""
    return Settings(
        BOT_TOKEN="0000000000:test_token_for_pytest",
        DATABASE_URL="sqlite+aiosqlite:///./test_jobs.db",
        LOG_LEVEL="WARNING",
        PLAYWRIGHT_HEADLESS="true",
        ADMIN_CHAT_ID=0,
    )


@pytest.fixture
async def repository():
    """Create an in-memory SQLite repository with tables pre-created.

    Each test that uses this fixture gets a fresh, empty database.
    """
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    repo = SQLAlchemyJobRepository(session_factory)
    yield repo

    await engine.dispose()

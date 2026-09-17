"""Tests for the Hermes integration API (database operations only, no scraping)."""

import pytest
from hermes import get_stats, get_recent_jobs


class TestHermesAPI:
    """Tests for hermes.py functions that query the database."""

    @pytest.mark.asyncio
    async def test_get_stats_returns_valid_structure(self):
        stats = await get_stats()
        assert "total" in stats
        assert "by_platform" in stats
        assert isinstance(stats["total"], int)
        assert isinstance(stats["by_platform"], dict)
        assert stats["total"] >= 0

    @pytest.mark.asyncio
    async def test_get_stats_platform_counts_sum_to_total(self):
        stats = await get_stats()
        platform_sum = sum(stats["by_platform"].values())
        assert platform_sum == stats["total"]

    @pytest.mark.asyncio
    async def test_get_recent_jobs_returns_list(self):
        jobs = await get_recent_jobs(limit=5)
        assert isinstance(jobs, list)
        assert len(jobs) <= 5
        for job in jobs:
            assert "title" in job
            assert "company" in job
            assert "url" in job
            assert "source_platform" in job

    @pytest.mark.asyncio
    async def test_get_recent_jobs_default_limit(self):
        jobs = await get_recent_jobs()
        assert len(jobs) <= 10

    @pytest.mark.asyncio
    async def test_get_recent_jobs_empty_db(self, tmp_path):
        """Use a temporary empty database."""
        db_path = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
        # Create tables
        from database.engine import create_engine_and_session
        from database.models import Base
        engine, _ = create_engine_and_session(db_path)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

        jobs = await get_recent_jobs(limit=5, db_path=db_path)
        assert jobs == []

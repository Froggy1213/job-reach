"""Tests for the agent validation filter (local mode only)."""

import pytest
from job_hunter.agent_filter import _local_match, filter_jobs, FILTER_PROFILES


class TestLocalFilterDesigner:
    """Local filter — designer profile."""

    def test_keeps_ui_designer(self):
        r = _local_match("UI Designer", "designer")
        assert r.keep is True
        assert r.score > 0.5

    def test_keeps_japanese_designer(self):
        r = _local_match("Webデザイナー", "designer")
        assert r.keep is True

    def test_keeps_product_designer(self):
        r = _local_match("Product Designer", "designer")
        assert r.keep is True

    def test_rejects_game_designer(self):
        r = _local_match("Game Designer", "designer")
        assert r.keep is False

    def test_rejects_fashion_designer(self):
        r = _local_match("Fashion Designer", "designer")
        assert r.keep is False

    def test_keeps_frontend_engineer(self):
        """Frontend engineer is close enough to design to keep."""
        r = _local_match("Frontend Engineer", "designer")
        assert r.keep is True

    def test_rejects_backend_engineer(self):
        r = _local_match("Backend Engineer", "designer")
        assert r.keep is False

    def test_rejects_sales(self):
        r = _local_match("Sales Representative", "designer")
        assert r.keep is False

    def test_rejects_architect(self):
        r = _local_match("Architect (Building)", "designer")
        assert r.keep is False

    def test_rejects_3d_artist(self):
        r = _local_match("3D CG Artist", "designer")
        assert r.keep is False


class TestLocalFilterFrontend:
    """Local filter — frontend profile."""

    def test_keeps_frontend_engineer(self):
        r = _local_match("Frontend Engineer", "frontend")
        assert r.keep is True

    def test_keeps_react_developer(self):
        r = _local_match("React Developer", "frontend")
        assert r.keep is True

    def test_keeps_japanese_frontend(self):
        r = _local_match("フロントエンドエンジニア", "frontend")
        assert r.keep is True

    def test_rejects_backend_engineer(self):
        r = _local_match("Backend Engineer", "frontend")
        assert r.keep is False

    def test_rejects_game_programmer(self):
        r = _local_match("Game Programmer", "frontend")
        assert r.keep is False

    def test_rejects_ui_designer(self):
        """UI Designer doesn't match frontend (engineering) profile."""
        r = _local_match("UI Designer", "frontend")
        assert r.keep is False

    def test_rejects_qa_engineer(self):
        r = _local_match("QA Engineer", "frontend")
        assert r.keep is False


class TestLocalFilterEngineering:
    """Local filter — engineering profile."""

    def test_keeps_software_engineer(self):
        r = _local_match("Software Engineer", "engineering")
        assert r.keep is True

    def test_keeps_backend_engineer(self):
        r = _local_match("Backend Engineer", "engineering")
        assert r.keep is True

    def test_keeps_python_developer(self):
        r = _local_match("Python Developer", "engineering")
        assert r.keep is True

    def test_keeps_japanese_se(self):
        r = _local_match("システムエンジニア", "engineering")
        assert r.keep is True

    def test_rejects_it_support(self):
        r = _local_match("IT Support Specialist", "engineering")
        assert r.keep is False

    def test_rejects_sales_engineer(self):
        r = _local_match("Sales Engineer", "engineering")
        assert r.keep is False

    def test_rejects_network_engineer(self):
        r = _local_match("Network Engineer", "engineering")
        assert r.keep is False


class TestLocalFilterAny:
    """Local filter — 'any' profile rejects universal stops only."""

    def test_keeps_designer(self):
        r = _local_match("UI Designer", "any")
        assert r.keep is True

    def test_keeps_engineer(self):
        r = _local_match("Software Engineer", "any")
        assert r.keep is True

    def test_rejects_sales(self):
        r = _local_match("Sales Manager", "any")
        assert r.keep is False

    def test_rejects_teacher(self):
        r = _local_match("English Teacher", "any")
        assert r.keep is False

    def test_rejects_driver(self):
        r = _local_match("Taxi Driver", "any")
        assert r.keep is False

    def test_keeps_it_support(self):
        """IT support is tech-adjacent, keep in 'any' mode."""
        r = _local_match("IT Support Engineer", "any")
        assert r.keep is True


class TestFilterJobsAsync:
    """Integration tests for filter_jobs()."""

    @pytest.mark.asyncio
    async def test_filter_jobs_local_designer(self):
        jobs = [
            {"title": "UI Designer", "company": "A", "url": "https://a.com"},
            {"title": "Sales Manager", "company": "B", "url": "https://b.com"},
            {"title": "Backend Engineer", "company": "C", "url": "https://c.com"},
            {"title": "Graphic Designer", "company": "D", "url": "https://d.com"},
        ]
        result = await filter_jobs(jobs, profile="designer", mode="local")
        assert result.stats["kept"] == 2  # UI + Graphic
        assert result.stats["rejected"] == 2
        assert result.stats["total"] == 4
        kept_titles = {j["title"] for j in result.kept}
        assert "UI Designer" in kept_titles
        assert "Graphic Designer" in kept_titles

    @pytest.mark.asyncio
    async def test_filter_jobs_empty(self):
        result = await filter_jobs([], profile="designer", mode="local")
        assert result.stats["total"] == 0
        assert result.kept == []

    @pytest.mark.asyncio
    async def test_filter_jobs_any(self):
        jobs = [
            {"title": "Software Engineer", "url": "https://a.com"},
            {"title": "Sales Rep", "url": "https://b.com"},
        ]
        result = await filter_jobs(jobs, profile="any", mode="local")
        assert result.stats["kept"] == 1
        assert result.stats["rejected"] == 1


class TestFilterProfiles:
    """Ensure all defined profiles have descriptions."""

    def test_profiles_exist(self):
        for p in ["designer", "frontend", "engineering", "any"]:
            assert p in FILTER_PROFILES
            assert len(FILTER_PROFILES[p]) > 50  # non-trivial prompt

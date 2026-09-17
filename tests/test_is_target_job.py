"""Tests for the job title filtering logic in BaseScraper."""

import pytest

from scrapers.base import BaseScraper


@pytest.mark.parametrize(
    "title",
    [
        "UI Designer",
        "Web Developer / UI Designer",
        "Frontend Engineer",
        "Senior UX Researcher and Designer",
        "グラフィックデザイナー",
        "【急募】Webデザイナー",
        "フロントエンドエンジニア",
        "ui/ux designer",
    ],
)
def test_is_target_job_accepts_valid_titles(title: str):
    """Target jobs containing keywords should be accepted."""
    assert BaseScraper.is_target_job(title) is True


@pytest.mark.parametrize(
    "title",
    [
        # Stop words present
        "3D CAD Operator",
        "Mechanical Engineer",
        "Game UI Designer",  # Has "UI Designer" but also "Game" (stop word)
        "Fashion Designer",
        "Architect (Building)",
        "機械設計エンジニア",
        "ゲームUIデザイナー",  # Stop word + target word
        "映像クリエイター",
        "アパレル販売員",
        "施工管理",
    ],
)
def test_is_target_job_rejects_stop_words(title: str):
    """Titles containing stop words must be rejected immediately."""
    assert BaseScraper.is_target_job(title) is False


@pytest.mark.parametrize(
    "title",
    [
        # Irrelevant jobs
        "Software Engineer (Backend)",
        "Sales Representative",
        "Project Manager",
        "Accountant",
        "営業職",
        "サーバーサイドエンジニア",
        "人事担当",
    ],
)
def test_is_target_job_rejects_irrelevant_jobs(title: str):
    """Titles without any target keywords should be rejected."""
    assert BaseScraper.is_target_job(title) is False

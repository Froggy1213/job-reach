"""Relevance filtering: profile heuristics and the batching entry point."""

from __future__ import annotations

import pytest

from jobreach.errors import FilterError
from jobreach.filters import available_profiles, filter_jobs, local_match


@pytest.mark.parametrize(
    ("title", "profile", "expected"),
    [
        ("UI Designer", "designer", True),
        ("Webデザイナー募集", "designer", True),
        ("Art Director", "designer", True),
        ("Frontend Engineer", "designer", True),      # explicit exception
        ("Game Designer", "designer", False),          # stop word
        ("営業スタッフ", "designer", False),            # universal stop
        ("Backend Engineer", "designer", False),
        ("Frontend Engineer", "frontend", True),
        ("React Developer", "frontend", True),
        ("iOS Engineer", "frontend", False),
        ("バックエンドエンジニア", "engineering", True),
        ("Helpdesk Support", "engineering", False),
        ("プロダクトマネージャー", "product", True),
        ("経理スタッフ", "product", False),
        ("Anything At All", "any", True),
    ],
)
def test_local_profile_decisions(title: str, profile: str, expected: bool):
    assert local_match(title, profile).keep is expected


def test_universal_stop_wins_over_any_profile():
    decision = local_match("Sales Engineer", "any")
    assert decision.keep is False
    assert "universal stop" in decision.reason


def test_unknown_profile_behaves_like_any():
    assert local_match("Product Designer", "does-not-exist").keep is True


def test_filter_jobs_splits_and_annotates():
    result = filter_jobs(
        [{"title": "UI Designer"}, {"title": "Sales Manager"}],
        profile="designer",
        mode="local",
    )
    assert [job["title"] for job in result.kept] == ["UI Designer"]
    assert [job["title"] for job in result.rejected] == ["Sales Manager"]
    assert result.kept[0]["filter_score"] > 0
    assert result.kept[0]["filter_reason"]
    assert result.stats == {
        "mode": "local",
        "total": 2,
        "kept": 1,
        "rejected": 1,
        "profile": "designer",
    }


def test_filter_jobs_handles_an_empty_batch():
    result = filter_jobs([], profile="designer", mode="local")
    assert result.kept == [] and result.rejected == []
    assert result.stats["total"] == 0


def test_unknown_mode_is_rejected():
    with pytest.raises(FilterError, match="unknown validation mode"):
        filter_jobs([{"title": "x"}], mode="vibes")


def test_llm_mode_without_a_key_fails_loudly(monkeypatch: pytest.MonkeyPatch):
    for variable in ("JOBREACH_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(FilterError, match="API key"):
        filter_jobs([{"title": "UI Designer"}], mode="llm")


def test_llm_batches_fall_back_to_local_on_failure(monkeypatch: pytest.MonkeyPatch):
    """A broken endpoint must degrade, not lose the scrape."""
    monkeypatch.setenv("JOBREACH_LLM_API_KEY", "test-key")

    def boom(**_kwargs: object):
        raise FilterError("endpoint down")

    monkeypatch.setattr("jobreach.filters._chat_completion", boom)
    result = filter_jobs(
        [{"title": "UI Designer"}, {"title": "Sales Manager"}],
        profile="designer",
        mode="llm",
    )
    assert [job["title"] for job in result.kept] == ["UI Designer"]
    assert result.stats["fallbacks"] == 1


def test_builtin_profiles_are_listed():
    assert {"designer", "frontend", "engineering", "product", "any"} <= set(
        available_profiles()
    )

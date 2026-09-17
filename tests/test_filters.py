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


def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "JOBREACH_LLM_API_KEY",
        "JOBREACH_LLM_BASE_URL",
        "JOBREACH_LLM_MODEL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_llm_mode_without_a_key_fails_loudly(monkeypatch: pytest.MonkeyPatch):
    _clear_llm_env(monkeypatch)
    with pytest.raises(FilterError, match="API key"):
        filter_jobs([{"title": "UI Designer"}], mode="llm")


def test_llm_batches_fall_back_to_local_on_failure(monkeypatch: pytest.MonkeyPatch):
    """A broken endpoint must degrade, not lose the scrape."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

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


# --- provider resolution ---------------------------------------------------
#
# These pin the bug where the key came from one variable while the endpoint and
# model were hard-coded to DeepSeek: setting only OPENAI_API_KEY still posted to
# api.deepseek.com as model "deepseek-chat", so the documented fallback returned
# 401 and looked like an auth problem rather than a configuration one.


def test_provider_defaults_are_consistent_per_key():
    """Each provider-specific key implies its own endpoint and model."""
    from jobreach.filters import resolve_llm_settings

    openai = resolve_llm_settings(env={"OPENAI_API_KEY": "sk-test"})
    assert openai.base_url == "https://api.openai.com/v1"
    assert openai.model == "gpt-4o-mini"
    assert openai.key_source == "OPENAI_API_KEY"
    assert openai.endpoint_host == "api.openai.com"

    deepseek = resolve_llm_settings(env={"DEEPSEEK_API_KEY": "sk-test"})
    assert deepseek.base_url == "https://api.deepseek.com/v1"
    assert deepseek.model == "deepseek-chat"
    assert deepseek.key_source == "DEEPSEEK_API_KEY"


def test_the_first_key_in_precedence_order_wins():
    from jobreach.filters import resolve_llm_settings

    settings = resolve_llm_settings(
        env={
            "JOBREACH_LLM_API_KEY": "generic",
            "JOBREACH_LLM_BASE_URL": "https://llm.test/v1",
            "OPENAI_API_KEY": "sk-openai",
        }
    )
    assert settings.api_key == "generic"
    assert settings.base_url == "https://llm.test/v1"


def test_a_generic_key_without_an_endpoint_is_refused():
    """Guessing a host for an unknown key is how you get an unexplained 401.

    Note the deliberate absence of a silent fallthrough: even when another
    usable key is present, an explicitly configured-but-incomplete provider is
    reported rather than quietly ignored.
    """
    from jobreach.filters import resolve_llm_settings

    with pytest.raises(FilterError, match="JOBREACH_LLM_BASE_URL"):
        resolve_llm_settings(
            env={"JOBREACH_LLM_API_KEY": "sk-test", "OPENAI_API_KEY": "sk-openai"}
        )


def test_a_generic_key_works_once_an_endpoint_is_given():
    from jobreach.filters import resolve_llm_settings

    settings = resolve_llm_settings(
        env={
            "JOBREACH_LLM_API_KEY": "sk-test",
            "JOBREACH_LLM_BASE_URL": "https://llm.test/v1/",
        }
    )
    assert settings.base_url == "https://llm.test/v1"  # trailing slash normalised
    assert settings.model == "gpt-4o-mini"  # OpenAI-compatible default


def test_explicit_arguments_beat_the_environment():
    from jobreach.filters import resolve_llm_settings

    settings = resolve_llm_settings(
        api_key="explicit",
        base_url="https://explicit.test/v1",
        model="explicit-model",
        env={"OPENAI_API_KEY": "sk-openai", "JOBREACH_LLM_MODEL": "from-env"},
    )
    assert (settings.api_key, settings.base_url, settings.model) == (
        "explicit",
        "https://explicit.test/v1",
        "explicit-model",
    )


def test_model_can_be_overridden_without_touching_the_endpoint():
    from jobreach.filters import resolve_llm_settings

    settings = resolve_llm_settings(
        env={"OPENAI_API_KEY": "sk-test", "JOBREACH_LLM_MODEL": "gpt-4o"}
    )
    assert settings.base_url == "https://api.openai.com/v1"
    assert settings.model == "gpt-4o"


def test_llm_filter_calls_the_provider_that_matches_the_key(monkeypatch: pytest.MonkeyPatch):
    """The end-to-end assertion: right host, right model, right key."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    seen: dict[str, object] = {}

    def fake_completion(**kwargs):
        seen.update(kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"classifications": [{"idx": 0, "keep": true, '
                        '"score": 0.9, "reason": "ok"}]}'
                    }
                }
            ],
            "usage": {"total_tokens": 7},
        }

    monkeypatch.setattr("jobreach.filters._chat_completion", fake_completion)
    result = filter_jobs([{"title": "UI Designer"}], profile="designer", mode="llm")

    assert seen["endpoint"] == "https://api.openai.com/v1"
    assert seen["model"] == "gpt-4o-mini"
    assert seen["key"] == "sk-openai"
    assert result.stats["model"] == "gpt-4o-mini"
    assert result.stats["endpoint"] == "api.openai.com"
    assert result.stats["key_source"] == "OPENAI_API_KEY"


def test_deepseek_key_still_reaches_deepseek(monkeypatch: pytest.MonkeyPatch):
    """The behaviour that already worked must not regress."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    seen: dict[str, object] = {}

    def fake_completion(**kwargs):
        seen.update(kwargs)
        return {"choices": [{"message": {"content": '{"classifications": []}'}}]}

    monkeypatch.setattr("jobreach.filters._chat_completion", fake_completion)
    filter_jobs([{"title": "UI Designer"}], profile="designer", mode="llm")
    assert seen["endpoint"] == "https://api.deepseek.com/v1"
    assert seen["model"] == "deepseek-chat"


def test_a_rejected_key_degrades_with_the_reason_attached(monkeypatch: pytest.MonkeyPatch):
    """A 401 must name the endpoint that refused, and must not lose the scrape."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-wrong-vendor")

    def unauthorised(**_kwargs):
        raise FilterError(
            "https://api.openai.com/v1 returned HTTP 401 for model 'gpt-4o-mini': bad key"
        )

    monkeypatch.setattr("jobreach.filters._chat_completion", unauthorised)
    result = filter_jobs([{"title": "UI Designer"}], profile="designer", mode="llm")
    assert result.stats["fallbacks"] == 1
    assert result.kept[0]["filter_reason"].startswith("LLM failover:")


def test_builtin_profiles_are_listed():
    assert {"designer", "frontend", "engineering", "product", "any"} <= set(
        available_profiles()
    )

"""Relevance filtering: is this listing actually a job the user wants?

Two strategies, same interface:

``local``
    Regex heuristics over the title. Free, instant, deterministic. Good enough
    to strip "Sales / Marketing / Nurse" noise out of a design search, and it
    is what the pipeline uses by default.

``llm``
    Batches listings through any OpenAI-compatible chat-completions endpoint
    and asks it to classify against a natural-language profile. Understands
    cross-language nuance ("設計" in manufacturing ≠ "design"), at the cost of
    an API key and a few seconds.

The LLM call uses :mod:`urllib.request` rather than ``httpx`` so the core
stays standard-library-only. It falls back to the local filter per batch when
the API call fails, so a flaky network degrades instead of aborting a scrape.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .errors import FilterError
from .logging_setup import get_logger

logger = get_logger("filters")

#: Natural-language definitions of what each profile KEEPS. The value is
#: either interpolated into the LLM prompt or (for ``local``) interpreted by
#: the regex tables below, so a new profile needs both halves.
FILTER_PROFILES: dict[str, str] = {
    "designer": (
        "I am looking for DESIGN and CREATIVE roles in Tokyo, Japan. "
        "KEEP: UI/UX designer, web designer, graphic designer, product designer, "
        "visual designer, brand designer, communication designer, art director, "
        "creative director, design lead, frontend designer. "
        "Also KEEP Japanese equivalents: デザイナー, UI/UXデザイナー, "
        "Webデザイナー, グラフィックデザイナー, プロダクトデザイナー, "
        "アートディレクター, クリエイティブディレクター. "
        "REJECT: game designer (ゲームデザイナー), fashion designer, "
        "industrial designer, CAD operator, architect (建築士), "
        "mechanical designer, video editor, 3D/CG artist, "
        "sales, marketing, HR, accounting, engineer (non-frontend)."
    ),
    "frontend": (
        "I am looking for FRONTEND ENGINEERING roles in Tokyo, Japan. "
        "KEEP: frontend engineer, frontend developer, web developer, "
        "JavaScript/TypeScript developer, React/Vue/Angular developer, "
        "UI engineer, full-stack (frontend-leaning), markup engineer. "
        "Also KEEP Japanese: フロントエンドエンジニア, フロントエンドデベロッパー, "
        "Webエンジニア, マークアップエンジニア. "
        "REJECT: backend-only, infra/SRE, data engineer, ML engineer, "
        "embedded, game programmer, QA-only, mobile (iOS/Android native), "
        "sales engineer, IT support, project manager (non-technical)."
    ),
    "engineering": (
        "I am looking for SOFTWARE ENGINEERING roles in Tokyo, Japan. "
        "KEEP: software engineer, backend engineer, full-stack, "
        "web developer, systems engineer, application engineer, "
        "cloud engineer, platform engineer, developer. "
        "Also KEEP Japanese: ソフトウェアエンジニア, システムエンジニア, "
        "バックエンドエンジニア, アプリケーションエンジニア, プログラマー. "
        "REJECT: IT support, helpdesk, technical support, sales engineer, "
        "project manager, QA-only, data entry, hardware, embedded (unless web-adjacent)."
    ),
    "any": (
        "I am looking for ANY design or tech role in Tokyo, Japan. "
        "KEEP: any design, engineering, product, or tech-adjacent role. "
        "REJECT: sales, marketing, HR, accounting, finance, legal, "
        "medical, construction, manufacturing (non-tech), education, "
        "hospitality, retail, driving, cleaning, security."
    ),
    "product": (
        "I am looking for PRODUCT MANAGEMENT and PRODUCT DESIGN roles in Tokyo, Japan. "
        "KEEP: product manager, product owner, product designer, UX researcher, "
        "service designer, business designer, PM, PdM, プロダクトマネージャー, "
        "プロダクトデザイナー, サービスデザイナー, UXリサーチャー. "
        "REJECT: project manager (non-product), sales, marketing, accounting, "
        "engineering-only roles, QA, support."
    ),
}

#: Titles matching any of these are rejected under every profile.
UNIVERSAL_STOP: tuple[str, ...] = (
    r"\b(sales|marketing|accounting|finance|legal|hr|human.resources?)\b",
    r"\b(medical|nurse|doctor|pharma|clinical)\b",
    r"\b(construction|manufacturing|factory|warehouse)\b",
    r"\b(education|teacher|instructor|professor)\b",
    r"\b(hospitality|hotel|restaurant|chef|cook|waiter|waitress)\b",
    r"\b(retail|store|shop.assistant|cashier)\b",
    r"\b(driving|driver|delivery|clean(ing|er)|security.guard)\b",
    r"\b(recruiter|talent.acquisition|sourcer)\b",
)

#: Per-profile ``(good, bad)`` regex tables used by the local strategy.
_LOCAL_PATTERNS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "designer": (
        (
            r"\b(ui|ux|user.(interface|experience))\b",
            r"\b(web|graphic|visual|brand|product|communication).?(design|デザイン)",
            r"\bdesign(er| lead| director| manager)?\b",
            r"\b(art|creative).(director| lead)\b",
            r"デザイナー",
            r"デザイン",
            r"アートディレクター",
            r"クリエイティブ",
            r"\bfrontend\b",
        ),
        (
            r"ゲーム", r"\bgame\b", r"ファッション", r"アパレル", r"\bfashion\b",
            r"\b(industrial|mechanical|machine|cad)\b", r"建築", r"\barchitect\b",
            r"\b(3d|cg|video|movie|映像|動画)\b", r"エンジニア", r"\bengineer\b",
            r"施工", r"建設", r"\bconstruction\b",
        ),
    ),
    "frontend": (
        (
            r"\bfrontend\b", r"front.end", r"フロントエンド",
            r"\b(web.develop|web.engineer)\b", r"webエンジニア",
            r"\b(react|vue|angular|next\.?js|nuxt\.?js)\b",
            r"\b(typescript|javascript|js\b)",
            r"(\b(ui|markup)[.\s-]?engineer\b|マークアップ)",
            r"\b(fullstack|full.stack)\b",
        ),
        (
            r"\b(backend.only|back.end.only)\b",
            r"\b(infra|sre|devops|platform.engineer)\b",
            r"\b(data.engineer|ml.engineer|machine.learning)\b",
            r"\b(embedded|iot|firmware)\b", r"ゲームプログラマ", r"\bgame.program\b",
            r"テスト", r"\bqa\b", r"\btester\b",
            r"\b(mobile|ios|android|swift|kotlin)\b",
            r"セールス", r"\bsales.engineer\b", r"サポート", r"\b(support|helpdesk)\b",
        ),
    ),
    "engineering": (
        (
            # Japanese compounds are matched literally: a trailing \b would
            # never fire between two kanji/kana word characters.
            r"ソフトウェアエンジニア", r"システムエンジニア", r"バックエンドエンジニア",
            r"フロントエンドエンジニア", r"アプリケーションエンジニア",
            r"インフラエンジニア", r"クラウドエンジニア",
            r"プログラマー", r"デベロッパー", r"開発エンジニア",
            r"\bse\b",
            r"\b(software|backend|back\.end|fullstack|full\.stack|web)\b.{0,16}\b(engineer|developer)\b",
            r"\b(cloud|platform|infrastructure|systems?)\b.{0,16}\bengineer\b",
            r"\b(developer|programmer)\b",
            r"\b(python|golang|go|rust|java|ruby|php|node|typescript|javascript)\b",
            r"\b(api|server).?(side)?\b",
        ),
        (
            r"サポート", r"ヘルプデスク", r"\b(support|helpdesk)\b",
            r"セールスエンジニア", r"\bsales.engineer\b",
            r"テストエンジニア", r"品質", r"\bqa\b", r"\btester\b",
            r"入力", r"オペレーター", r"\bdata.entry\b",
            r"ネットワーク", r"\bnetwork.engineer\b", r"ハードウェア", r"\bhardware\b",
        ),
    ),
    "product": (
        (
            r"プロダクトマネージャー", r"プロダクトデザイナー", r"サービスデザイナー",
            r"UXリサーチャー", r"ユーザーリサーチ",
            r"\bproduct[.\s-]?(manager|owner|designer)\b",
            r"(\b(ux|user)[.\s-]?research\b|UXリサーチ)",
            r"\b(pdm|pm)\b",
            r"\bproduct\b", r"事業企画", r"企画",
        ),
        (
            r"\b(sales|marketing|accounting|hr)\b",
            r"セールス", r"営業", r"経理",
            r"\bproject[.\s-]?manager\b", r"\bpmo\b",
        ),
    ),
}

_SYSTEM_PROMPT = """\
You are a job listing classifier. Given a list of job titles/descriptions and a search profile, return a JSON array classifying each job.

For each job, output:
- "idx": the job's index number
- "keep": true if the job matches the profile, false otherwise
- "score": relevance score 0.0 to 1.0 (1.0 = perfect match)
- "reason": one-line explanation in English

Rules:
- Be GENEROUS — if a job could plausibly match, keep it (score >= 0.4).
- Understand Japanese job titles (many are in Japanese or mixed JA/EN).
- Cross-language: "エンジニア" = engineer, "デザイナー" = designer.
- New-grad / 新卒 / 未経験者歓迎 roles are valid — don't reject for junior level.
- A role with the right keywords in a different context (e.g. "設計" meaning design in manufacturing) should be REJECTED.

Return ONLY valid JSON — no markdown, no explanation outside the JSON."""


@dataclass(slots=True)
class Decision:
    """The verdict for one listing."""

    keep: bool
    reason: str
    score: float


@dataclass(slots=True)
class FilterResult:
    """Outcome of filtering a batch of listings."""

    kept: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    stats: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Local (regex) strategy
# --------------------------------------------------------------------------- #


def local_match(title: str, profile: str) -> Decision:
    """Classify *title* against *profile* using regex heuristics only."""
    text = title.lower()

    for pattern in UNIVERSAL_STOP:
        if re.search(pattern, text):
            return Decision(False, f"universal stop: {pattern}", 0.0)

    if profile == "any" or profile not in _LOCAL_PATTERNS:
        return Decision(True, f"{profile}: no universal stop matched", 0.6)

    good, bad = _LOCAL_PATTERNS[profile]

    # "Frontend Engineer" must survive the designer profile's engineer stop-word.
    if profile == "designer" and re.search(r"\bfrontend\b", text):
        return Decision(True, "designer: frontend exception", 0.9)

    for pattern in bad:
        if re.search(pattern, text):
            return Decision(False, f"{profile} stop word: {pattern}", 0.0)

    for pattern in good:
        if re.search(pattern, text):
            return Decision(True, f"{profile} match: {pattern}", 0.8)

    return Decision(False, f"no {profile} match", 0.0)


# --------------------------------------------------------------------------- #
# LLM strategy
# --------------------------------------------------------------------------- #


def llm_filter(
    jobs: list[dict[str, Any]],
    profile: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str = "deepseek-chat",
    batch_size: int = 10,
    timeout: float = 60.0,
) -> tuple[list[Decision], dict[str, Any]]:
    """Classify *jobs* through an OpenAI-compatible endpoint, in batches.

    The API key is read from ``JOBREACH_LLM_API_KEY``, ``DEEPSEEK_API_KEY``,
    then ``OPENAI_API_KEY``. Any batch that fails falls back to
    :func:`local_match`, so one bad request cannot lose a whole scrape.
    """
    key = api_key or os.environ.get("JOBREACH_LLM_API_KEY") or os.environ.get(
        "DEEPSEEK_API_KEY"
    ) or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise FilterError(
            "llm validation needs an API key: set JOBREACH_LLM_API_KEY, "
            "DEEPSEEK_API_KEY or OPENAI_API_KEY, or use validation='local'"
        )
    endpoint = (base_url or os.environ.get("JOBREACH_LLM_BASE_URL")
                or os.environ.get("DEEPSEEK_BASE_URL")
                or "https://api.deepseek.com/v1").rstrip("/")
    profile_prompt = FILTER_PROFILES.get(profile, profile)

    decisions: list[Decision] = []
    stats = {"api_calls": 0, "fallbacks": 0, "mode": "llm", "model": model}

    for start in range(0, len(jobs), batch_size):
        batch = jobs[start : start + batch_size]
        lines = []
        for offset, job in enumerate(batch):
            parts = [f"Job #{start + offset}: {job.get('title', '')}"]
            if job.get("company"):
                parts.append(f" | Company: {job['company']}")
            if job.get("location"):
                parts.append(f" | Location: {job['location']}")
            if job.get("description"):
                parts.append(f" | Description: {str(job['description'])[:300]}")
            lines.append("".join(parts))

        try:
            payload = _chat_completion(
                endpoint=endpoint,
                key=key,
                model=model,
                timeout=timeout,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Search profile:\n{profile_prompt}\n\n"
                            f"Jobs to classify:\n" + "\n".join(lines)
                        ),
                    },
                ],
            )
            stats["api_calls"] += 1
            stats["tokens_used"] = stats.get("tokens_used", 0) + int(
                (payload.get("usage") or {}).get("total_tokens", 0)
            )
            content = payload["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            classifications = (
                parsed if isinstance(parsed, list) else parsed.get("classifications", [])
            )
            by_index = {
                int(item.get("idx", item.get("index", -1))): item
                for item in classifications
                if isinstance(item, dict)
            }
            for offset, job in enumerate(batch):
                item = by_index.get(start + offset)
                if item is None:
                    decisions.append(local_match(job.get("title", ""), profile))
                    continue
                keep = bool(item.get("keep", False))
                decisions.append(
                    Decision(
                        keep=keep,
                        reason=str(item.get("reason") or "LLM classified"),
                        score=float(item.get("score", 0.5 if keep else 0.0)),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — degrade, never abort a scrape
            logger.warning("LLM batch failed, falling back to local", extra={"error": str(exc)})
            stats["fallbacks"] += 1
            for job in batch:
                fallback = local_match(job.get("title", ""), profile)
                decisions.append(
                    Decision(fallback.keep, f"LLM failover: {fallback.reason}", fallback.score)
                )

    return decisions, stats


def _chat_completion(
    *, endpoint: str, key: str, model: str, messages: list[dict[str, str]], timeout: float
) -> dict[str, Any]:
    """POST a chat completion with ``urllib`` and return the decoded body."""
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise FilterError(f"LLM endpoint returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise FilterError(f"LLM endpoint unreachable: {exc.reason}") from exc


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def filter_jobs(
    jobs: list[dict[str, Any]],
    *,
    profile: str = "designer",
    mode: str = "local",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str = "deepseek-chat",
) -> FilterResult:
    """Split *jobs* into ``kept`` and ``rejected`` under *profile*.

    Each returned record is the input dict plus ``filter_reason`` and
    ``filter_score``, so the agent can explain *why* something was dropped.
    """
    if not jobs:
        return FilterResult(kept=[], rejected=[], stats={"total": 0, "kept": 0, "rejected": 0,
                                                         "mode": mode, "profile": profile})
    if mode == "llm":
        decisions, stats = llm_filter(
            jobs, profile, api_key=api_key, base_url=base_url, model=model
        )
    elif mode == "local":
        decisions = [local_match(job.get("title", ""), profile) for job in jobs]
        stats = {"mode": "local"}
    else:
        raise FilterError(f"unknown validation mode {mode!r}; use 'local' or 'llm'")

    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for job, decision in zip(jobs, decisions, strict=True):
        record = {**job, "filter_reason": decision.reason, "filter_score": decision.score}
        (kept if decision.keep else rejected).append(record)

    stats.update(
        {
            "total": len(jobs),
            "kept": len(kept),
            "rejected": len(rejected),
            "profile": profile,
            "mode": stats.get("mode", mode),
        }
    )
    return FilterResult(kept=kept, rejected=rejected, stats=stats)


def available_profiles() -> list[str]:
    """Profile names the caller may pass as ``validation_profile``."""
    return sorted(FILTER_PROFILES)

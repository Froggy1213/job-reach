"""LLM-based job validation agent.

Takes raw scraped jobs and filters them through an LLM using a semantic
profile.  Goes far beyond the static ``is_target_job()`` keyword filter —
the agent understands context, seniority, and role nuance.

Supports two modes:

1. **Local** (``mode="local"``) — improved regex/keyword heuristics, zero API cost.
2. **LLM** (``mode="llm"``) — batch-evaluate jobs via OpenAI-compatible API.

LLM mode batches jobs (default 10 per call) to minimize API round-trips
and uses structured JSON output for reliable parsing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("job_hunter.agent_filter")

# ---------------------------------------------------------------------------
# Filter profiles — natural-language descriptions of what to KEEP
# ---------------------------------------------------------------------------

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
}


# ---------------------------------------------------------------------------
# Local heuristic filter — improved regex version of is_target_job()
# ---------------------------------------------------------------------------

@dataclass
class LocalFilterResult:
    keep: bool
    reason: str
    score: float  # 0.0–1.0 relevance


def _local_match(title: str, profile: str) -> LocalFilterResult:
    """Improved local filter with regex patterns per profile."""

    t = title.lower()

    # Universal stop words (never relevant for tech/design roles)
    UNIVERSAL_STOP = [
        r"\b(sales|marketing|accounting|finance|legal|hr|human.resources?)\b",
        r"\b(medical|nurse|doctor|pharma|clinical)\b",
        r"\b(construction|manufacturing|factory|warehouse)\b",
        r"\b(education|teacher|instructor|professor)\b",
        r"\b(hospitality|hotel|restaurant|chef|cook|waiter|waitress)\b",
        r"\b(retail|store|shop.assistant|cashier)\b",
        r"\b(driving|driver|delivery|clean(ing|er)|security.guard)\b",
        r"\b(recruiter|talent.acquisition|sourcer)\b",
    ]
    for pattern in UNIVERSAL_STOP:
        if re.search(pattern, t):
            return LocalFilterResult(False, f"universal stop: {pattern}", 0.0)

    # Profile-specific patterns
    if profile == "designer":
        good = [
            r"\b(ui|ux|user.(interface|experience))\b",
            r"\b(web|graphic|visual|brand|product|communication).?(design|デザイン)",
            r"\bdesign(er| lead| director| manager)?\b",
            r"\b(art|creative).(director| lead)\b",
            r"デザイナー",
            r"デザイン",
            r"アートディレクター",
            r"クリエイティブ",
            r"\bfrontend\b",
        ]
        bad = [
            r"ゲーム",
            r"\bgame\b",
            r"ファッション",
            r"アパレル",
            r"\b(fashion)\b",
            r"\b(industrial|mechanical|machine|cad)\b",
            r"建築",
            r"\barchitect\b",
            r"\b(3d|cg|video|movie|映像|動画)\b",
            r"エンジニア",
            r"\bengineer\b",
            r"施工",
            r"建設",
            r"\bconstruction\b",
        ]
        # "Frontend Engineer" should pass despite having "engineer"
        if re.search(r"\bfrontend\b", t):
            score = 0.9
        elif any(re.search(p, t) for p in bad):
            return LocalFilterResult(False, "designer stop word", 0.0)
        elif any(re.search(p, t) for p in good):
            score = 0.8
        else:
            return LocalFilterResult(False, "no designer match", 0.0)
        return LocalFilterResult(True, "designer match", score)

    elif profile == "frontend":
        good = [
            r"\bfrontend\b",
            r"front.end",
            r"フロントエンド",
            r"\b(web.develop|web.engineer|webエンジニア)\b",
            r"\b(react|vue|angular|next\.?js|nuxt\.?js)\b",
            r"\b(typescript|javascript|js\b)",
            r"\b(ui.engineer|markup.engineer|マークアップ)\b",
            r"\b(fullstack|full.stack)\b",
        ]
        bad = [
            r"\b(backend.only|back.end.only)\b",
            r"\b(infra|sre|devops|platform.engineer)\b",
            r"\b(data.engineer|ml.engineer|machine.learning)\b",
            r"\b(embedded|iot|firmware)\b",
            r"ゲームプログラマ",
            r"\bgame.program\b",
            r"テスト",
            r"\bqa\b",
            r"\btester\b",
            r"\b(mobile|ios|android|swift|kotlin)\b",
            r"セールス",
            r"\bsales.engineer\b",
            r"サポート",
            r"\b(support|helpdesk)\b",
        ]
        if any(re.search(p, t) for p in bad):
            return LocalFilterResult(False, "frontend stop word", 0.0)
        elif any(re.search(p, t) for p in good):
            return LocalFilterResult(True, "frontend match", 0.8)
        else:
            return LocalFilterResult(False, "no frontend match", 0.0)

    elif profile == "engineering":
        good = [
            r"\b(software|ソフトウェア).?(engineer|エンジニア|developer|デベロッパー)?\b",
            r"システムエンジニア",
            r"\bse\b",
            r"\b(backend|back.end|バックエンド)\b",
            r"\b(fullstack|full.stack)\b",
            r"\b(web.develop|web.engineer)\b",
            r"\b(cloud|platform|infrastructure).?(engineer|エンジニア)?\b",
            r"アプリケーションエンジニア",
            r"\b(developer|デベロッパー|プログラマー|programmer)\b",
            r"\b(python|go|rust|java|ruby|php|node)\b",
            r"\b(backend|api|server).?(side|サイド)?\b",
        ]
        bad = [
            r"サポート",
            r"ヘルプデスク",
            r"\b(support|helpdesk)\b",
            r"セールスエンジニア",
            r"\bsales.engineer\b",
            r"テストエンジニア",
            r"品質",
            r"\bqa\b",
            r"\btester\b",
            r"入力",
            r"オペレーター",
            r"\bdata.entry\b",
            r"ネットワーク",
            r"\bnetwork.engineer\b",
            r"ハードウェア",
            r"\bhardware\b",
        ]
        if any(re.search(p, t) for p in bad):
            return LocalFilterResult(False, "engineering stop word", 0.0)
        elif any(re.search(p, t) for p in good):
            return LocalFilterResult(True, "engineering match", 0.8)
        else:
            return LocalFilterResult(False, "no engineering match", 0.0)

    elif profile == "any":
        # Only reject universal stops
        score = 0.6
        return LocalFilterResult(True, "any — no universal stops", score)

    return LocalFilterResult(False, "unknown profile", 0.0)


# ---------------------------------------------------------------------------
# LLM batch filter
# ---------------------------------------------------------------------------

# System prompt for the LLM classifier
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


@dataclass
class BatchResult:
    results: list[LocalFilterResult]
    api_calls: int = 0
    tokens_used: int = 0


async def llm_filter(
    jobs: list[dict[str, str]],
    profile: str,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str = "deepseek-chat",
    batch_size: int = 10,
) -> BatchResult:
    """Filter jobs through an LLM in batches.

    Args:
        jobs: List of dicts with ``title``, ``company``, ``location``, ``description`` (optional).
        profile: Filter profile key (see ``FILTER_PROFILES``) or custom prompt.
        api_key: API key.  Reads ``OPENAI_API_KEY`` or ``DEEPSEEK_API_KEY`` if ``None``.
        base_url: API base URL.  Defaults to DeepSeek.
        model: Model name.
        batch_size: Jobs per LLM call.

    Returns:
        ``BatchResult`` with per-job decisions.
    """
    import httpx

    if api_key is None:
        api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "No API key found. Set DEEPSEEK_API_KEY or OPENAI_API_KEY, "
            "or pass api_key=..."
        )
    if base_url is None:
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

    profile_prompt = FILTER_PROFILES.get(profile, profile)
    batch_results: list[LocalFilterResult] = []
    total_tokens = 0
    api_calls = 0

    async with httpx.AsyncClient(timeout=60.0) as client:
        for batch_start in range(0, len(jobs), batch_size):
            batch = jobs[batch_start : batch_start + batch_size]

            # Build user message — one job per line with index
            job_lines = []
            for i, job in enumerate(batch):
                idx = batch_start + i
                parts = [f"Job #{idx}: {job.get('title', '')}"]
                if job.get("company"):
                    parts.append(f" | Company: {job['company']}")
                if job.get("location"):
                    parts.append(f" | Location: {job['location']}")
                if job.get("description"):
                    desc = job["description"][:300]
                    parts.append(f" | Description: {desc}")
                job_lines.append("".join(parts))

            user_msg = (
                f"Search profile:\n{profile_prompt}\n\n"
                f"Jobs to classify:\n" + "\n".join(job_lines)
            )

            try:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": user_msg},
                        ],
                        "temperature": 0.0,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                api_calls += 1
                usage = data.get("usage", {})
                total_tokens += usage.get("total_tokens", 0)

                content = data["choices"][0]["message"]["content"]
                parsed = json.loads(content)

                # Accept {"classifications": [...]} or top-level [...]
                classifications = parsed if isinstance(parsed, list) else parsed.get("classifications", [])

                for item in classifications:
                    idx = item.get("idx", item.get("index", -1))
                    keep = item.get("keep", False)
                    score = float(item.get("score", 0.5 if keep else 0.0))
                    reason = item.get("reason", "LLM classified")
                    batch_results.append(LocalFilterResult(keep=keep, reason=reason, score=score))

                logger.info(
                    "LLM batch done",
                    extra={
                        "batch_start": batch_start,
                        "batch_size": len(batch),
                        "classifications": len(classifications),
                        "kept": sum(1 for c in classifications if c.get("keep")),
                    },
                )

            except Exception as exc:
                logger.error("LLM batch failed, falling back to local", extra={"error": str(exc)})
                # Fall back to local filter for this batch
                for i, job in enumerate(batch):
                    idx = batch_start + i
                    local = _local_match(job.get("title", ""), profile)
                    local.reason = f"LLM failover: {local.reason}"
                    batch_results.append(local)

    return BatchResult(results=batch_results, api_calls=api_calls, tokens_used=total_tokens)


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------


@dataclass
class FilterResult:
    kept: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    stats: dict[str, Any] = field(default_factory=dict)


async def filter_jobs(
    jobs: list[dict[str, Any]],
    profile: str = "designer",
    mode: str = "local",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str = "deepseek-chat",
) -> FilterResult:
    """Filter a list of job dicts, returning kept and rejected.

    Args:
        jobs: List of dicts with at least ``title``.  ``company``, ``location``,
              ``description`` are optional but improve LLM accuracy.
        profile: ``"designer"``, ``"frontend"``, ``"engineering"``, ``"any"``,
                 or a custom natural-language description.
        mode: ``"local"`` (regex heuristics, free) or ``"llm"`` (LLM batch, needs API key).

    Returns:
        ``FilterResult`` with ``kept``, ``rejected``, and ``stats``.
    """
    if mode == "llm":
        batch = await llm_filter(
            jobs=jobs,
            profile=profile,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        results = batch.results
        stats = {"api_calls": batch.api_calls, "tokens_used": batch.tokens_used, "mode": "llm"}
    else:
        results = [_local_match(j.get("title", ""), profile) for j in jobs]
        stats = {"mode": "local"}

    kept = []
    rejected = []
    for job, result in zip(jobs, results):
        entry = {**job, "filter_reason": result.reason, "filter_score": result.score}
        if result.keep:
            kept.append(entry)
        else:
            rejected.append(entry)

    stats.update({
        "total": len(jobs),
        "kept": len(kept),
        "rejected": len(rejected),
        "profile": profile,
    })

    return FilterResult(kept=kept, rejected=rejected, stats=stats)

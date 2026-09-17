"""Obsidian note rendering and path derivation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobreach.errors import ConfigError
from jobreach.notes import note_path, render_note, slugify, write_note

MOMENT = datetime(2026, 9, 17, 9, 30, tzinfo=UTC)


def envelope() -> dict:
    return {
        "mode": "search",
        "query": {"keyword": "frontend engineer", "location": "tokyo",
                  "sources": ["wantedly", "indeed"]},
        "summary": {
            "total": 2,
            "new": 1,
            "saved": 1,
            "shown": 2,
            "by_platform": {"wantedly": {"total": 1, "new": 1},
                            "indeed": {"total": 1, "new": 0}},
            "errors": {"linkedin": "Chrome is not running"},
        },
        "jobs": [
            {"title": "Frontend Engineer", "company": "Acme", "url": "https://w.test/1",
             "location": "Tokyo, Shibuya", "source_platform": "wantedly",
             "source_label": "Wantedly", "salary": None, "is_new": True},
            {"title": "Backend | Platform", "company": "Foo", "url": "https://i.test/2",
             "location": "Tokyo", "source_platform": "indeed",
             "source_label": "Indeed Japan", "salary": None, "is_new": False},
        ],
    }


def test_slugify_is_filesystem_safe():
    assert slugify("Frontend Engineer / Tokyo") == "frontend-engineer-tokyo"
    assert slugify("  ") == "search"
    assert len(slugify("x" * 200)) == 60


def test_note_path_layout(tmp_path: Path):
    path = note_path(
        keyword="UX Researcher", location="tokyo", vault=tmp_path, when=MOMENT
    )
    assert path.parent == tmp_path / "job-searches"
    assert path.name == "2026-09-17 - ux-researcher - tokyo.md"


def test_note_path_requires_a_vault(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    monkeypatch.setattr("jobreach.config.Path.home", lambda: Path("/nonexistent-home"))
    with pytest.raises(ConfigError, match="vault"):
        note_path(keyword="x", location="tokyo")


def test_frontmatter_and_groups():
    text = render_note(envelope(), generated_at=MOMENT)
    assert text.startswith("---\n")
    assert 'query: "frontend engineer"' in text
    assert "sources: [wantedly, indeed]" in text
    assert "total: 2" in text
    assert "## Wantedly (1 jobs)" in text
    assert "## Indeed Japan (1 jobs)" in text


def test_new_listings_get_a_badge():
    text = render_note(envelope(), generated_at=MOMENT)
    assert "🏷️ NEW [Frontend Engineer](https://w.test/1)" in text
    assert "| 1 | [Backend \\| Platform](https://i.test/2)" in text


def test_errors_are_surfaced():
    text = render_note(envelope(), generated_at=MOMENT)
    assert "## Warnings" in text
    assert "linkedin" in text and "Chrome is not running" in text


def test_empty_result_set_renders_cleanly():
    text = render_note(
        {"query": {}, "summary": {}, "jobs": []}, generated_at=MOMENT
    )
    assert "_No listings matched this run._" in text


def test_render_is_byte_stable():
    """Two renders of the same envelope differ only by the caller's timestamp."""
    assert render_note(envelope(), generated_at=MOMENT) == render_note(
        envelope(), generated_at=MOMENT
    )


def test_write_note_creates_directories(tmp_path: Path):
    path = write_note(envelope(), vault=tmp_path)
    assert path.exists()
    assert path.parent.name == job_searches_subfolder()
    assert "Frontend Engineer" in path.read_text(encoding="utf-8")


def job_searches_subfolder() -> str:
    from jobreach.config import NOTE_SUBFOLDER

    return NOTE_SUBFOLDER

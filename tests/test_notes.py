"""Obsidian note rendering and path derivation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

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


def test_every_board_renders_with_its_display_label():
    """Green / Daijob / Japan Dev must not fall back to the raw board id."""
    text = render_note(
        {
            "query": {"keyword": "designer", "location": "tokyo"},
            "summary": {
                "total": 3,
                "new": 0,
                "by_platform": {"green": {"total": 1, "new": 0},
                                "daijob": {"total": 1, "new": 0},
                                "japan_dev": {"total": 1, "new": 0}},
            },
            "jobs": [
                {"title": "Web Designer", "company": "A", "url": "https://g.test/1",
                 "location": "東京都", "source_platform": "green", "is_new": False},
                {"title": "Graphic Designer", "company": "B", "url": "https://d.test/2",
                 "location": "東京都", "source_platform": "daijob", "is_new": False},
                {"title": "Product Designer", "company": "C", "url": "https://j.test/3",
                 "location": "Tokyo", "source_platform": "japan_dev", "is_new": False},
            ],
        },
        generated_at=MOMENT,
    )
    for heading in ("## Green (1 jobs)", "## Daijob (1 jobs)", "## Japan Dev (1 jobs)"):
        assert heading in text
    assert "## green" not in text


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


def test_frontmatter_escapes_quotes_and_backslashes():
    keyword = 'say "hi" \\ now'
    location = 'tokyo "metro" \\ central'
    note = render_note(
        {
            "query": {"keyword": keyword, "location": location},
            "summary": {"total": 0, "new": 0},
            "jobs": [],
        },
        generated_at=MOMENT,
    )
    parts = note.split("---")
    assert len(parts) >= 3
    frontmatter = yaml.safe_load(parts[1])
    assert frontmatter["query"] == keyword
    assert frontmatter["location"] == location


def test_listing_title_with_newlines_and_pipes_renders_single_row():
    title = "Senior Frontend\nDeveloper | Remote"
    note = render_note(
        {
            "query": {"keyword": "frontend", "location": "tokyo"},
            "summary": {"total": 1, "new": 0},
            "jobs": [
                {
                    "title": title,
                    "company": "Acme\nCorp",
                    "url": "https://example.com/job/1",
                    "location": "Tokyo\nRemote",
                    "source_platform": "wantedly",
                    "is_new": False,
                }
            ],
        },
        generated_at=MOMENT,
    )
    # The listing must produce exactly one table row line with escaped pipe and no inner newline
    row_lines = [line for line in note.splitlines() if "[Senior Frontend Developer" in line]
    assert len(row_lines) == 1
    row = row_lines[0]
    assert row.startswith("| 1 |")
    assert "Senior Frontend Developer \\| Remote" in row
    assert "Acme Corp" in row
    assert "Tokyo Remote" in row

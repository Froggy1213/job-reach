"""Tests for URL normalization — the deduplication key."""

from database.repository import normalize_url


def test_strips_trailing_slash_and_fragment():
    assert normalize_url("https://example.com/jobs/1/#top") == "https://example.com/jobs/1"


def test_lowercases_scheme_and_host_only():
    # Host is lowercased; the path's case is preserved.
    assert (
        normalize_url("HTTPS://WWW.Wantedly.com/projects/123")
        == "https://www.wantedly.com/projects/123"
    )


def test_queryless_urls_unchanged():
    # Wantedly/Mynavi listings have no query — normalization is a no-op.
    url = "https://www.wantedly.com/projects/123"
    assert normalize_url(url) == url


def test_query_preserved_for_indeed_style_ids():
    # Indeed's job id lives in ?jk=; two different jk must NOT collapse.
    a = normalize_url("https://jp.indeed.com/viewjob?jk=aaa111")
    b = normalize_url("https://jp.indeed.com/viewjob?jk=bbb222")
    assert a == "https://jp.indeed.com/viewjob?jk=aaa111"
    assert a != b

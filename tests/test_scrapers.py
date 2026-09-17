"""Scraper strategies: URL construction, relevance matching, CLI plumbing.

Nothing here launches a browser — the parsers are fed synthetic card payloads,
which is precisely why they were written as separate methods.
"""

from __future__ import annotations

import pytest

from jobreach.domain import SourcePlatform
from jobreach.errors import ScraperError
from jobreach.scrapers import (
    SCRAPERS,
    available_sources,
    build_scrapers,
    check_source_requirements,
    is_cli_scraper,
)
from jobreach.scrapers.cli_base import _records_from
from jobreach.scrapers.linkedin import LinkedInScraper
from jobreach.scrapers.mynavi2027 import Mynavi2027Scraper
from jobreach.scrapers.wantedly import WantedlyScraper

# --- registry --------------------------------------------------------------


def test_registry_lists_scrapers_and_ingest_only_boards():
    assert set(available_sources()) == {"wantedly", "mynavi2027", "linkedin", "indeed"}
    assert "indeed" not in SCRAPERS


def test_build_scrapers_rejects_indeed_with_guidance():
    with pytest.raises(ValueError, match="job_ingest"):
        build_scrapers(["indeed"])


def test_build_scrapers_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown source"):
        build_scrapers(["monster"])


def test_build_scrapers_passes_the_query_through():
    (scraper,) = build_scrapers(["wantedly"], keyword=" designer ", location="osaka")
    assert scraper.keyword == "designer"
    assert scraper.location == "osaka"


def test_linkedin_is_the_only_cli_board():
    assert is_cli_scraper("linkedin")
    assert not is_cli_scraper("wantedly")


def test_requirements_report_missing_playwright(monkeypatch: pytest.MonkeyPatch):
    """Without the scraping extra, browser boards report a problem — not a crash."""
    monkeypatch.setattr(
        "jobreach.scrapers.base.require_playwright",
        lambda: (_ for _ in ()).throw(ImportError("nope")),
    )
    problems = check_source_requirements(["wantedly", "mynavi2027", "linkedin"])
    assert set(problems) == {"wantedly", "mynavi2027"}


# --- relevance matching ----------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Web Designer", True),
        ("UI/UX Designer", True),
        ("Game Designer", False),
        ("Fashion Designer", False),
        ("Mechanical CAD operator", False),
        ("セールス担当", False),
    ],
)
def test_default_design_filter(title: str, expected: bool):
    assert WantedlyScraper().matches(title) is expected


def test_server_side_keyword_boards_trust_the_query():
    """A cross-language query returns titles a substring match would reject."""
    scraper = WantedlyScraper(keyword="engineer")
    assert scraper.matches("バックエンドエンジニア募集") is True


def test_client_side_keyword_boards_filter_on_tokens():
    """Mynavi has no server-side search, so its filter is deliberately literal."""
    scraper = Mynavi2027Scraper(keyword="design")
    assert scraper.matches("Acme | Web Design職") is True
    assert scraper.matches("Acme | 営業職") is False


def test_a_japanese_keyword_matches_japanese_cards():
    assert Mynavi2027Scraper(keyword="デザイン").matches("Acme | Webデザイン職") is True


def test_an_english_keyword_cannot_match_a_purely_japanese_card():
    """Documents the known limitation that makes Wantedly the general board."""
    assert Mynavi2027Scraper(keyword="design").matches("Acme | Webデザイン職") is False


# --- Wantedly --------------------------------------------------------------


def test_wantedly_url_encodes_keyword_and_location():
    url = WantedlyScraper(keyword="frontend engineer", location="osaka").build_page_url(2)
    assert url.startswith("https://www.wantedly.com/projects?")
    assert "q=frontend+engineer" in url
    assert "locations=osaka" in url
    assert "page=2" in url


def test_wantedly_default_url_uses_design_occupations():
    url = WantedlyScraper().build_page_url(1)
    assert "occupations=" in url
    assert "locations=tokyo" in url


@pytest.mark.parametrize("sentinel", ["any", "all"])
def test_wantedly_location_sentinel_disables_the_filter(sentinel: str):
    assert "locations=" not in WantedlyScraper(location=sentinel).build_page_url(1)


def test_wantedly_parses_cards_and_drops_malformed_ones():
    scraper = WantedlyScraper()
    jobs = scraper._parse_cards(
        [
            {"title": "UI Designer", "company": "Acme", "url": "https://w.test/1",
             "location": "Tokyo, 渋谷"},
            {"title": "Sales", "company": "Nope", "url": "https://w.test/2",
             "location": "Tokyo"},
            {"title": "Broken"},  # no url
        ]
    )
    assert [job.url for job in jobs] == ["https://w.test/1"]
    assert jobs[0].source_platform is SourcePlatform.WANTEDLY


def test_wantedly_platform_property():
    assert WantedlyScraper().platform is SourcePlatform.WANTEDLY


# --- Mynavi ----------------------------------------------------------------


def test_mynavi_parses_and_deduplicates_cards():
    scraper = Mynavi2027Scraper()
    seen: set[str] = set()
    cards = [
        {"company": "Acme", "matchedText": "Acme | Webデザイン", "location": "Tokyo",
         "url": "https://job.mynavi.jp/corp1"},
        {"company": "Acme", "matchedText": "Acme | Webデザイン", "location": "Tokyo",
         "url": "https://job.mynavi.jp/corp1"},
    ]
    jobs = scraper._parse_cards(cards, "WEBデザイナー", seen)
    assert len(jobs) == 1
    assert jobs[0].title == "Acme (WEBデザイナー)"


def test_mynavi_filters_non_design_cards():
    jobs = Mynavi2027Scraper()._parse_cards(
        [{"company": "Acme", "matchedText": "Acme | 営業", "location": "Tokyo",
          "url": "https://job.mynavi.jp/corp9"}],
        "WEBデザイナー",
        set(),
    )
    assert jobs == []


# --- LinkedIn --------------------------------------------------------------


def test_linkedin_builds_the_expected_cli_argv():
    args = LinkedInScraper(keyword="designer", location="Tokyo").build_cli_args()
    assert args[:3] == ["opencli", "linkedin", "search"]
    assert "designer" in args
    assert "-f" in args and args[args.index("-f") + 1] == "json"


def test_linkedin_defaults_keyword_and_location():
    args = LinkedInScraper().build_cli_args()
    assert "designer" in args
    assert "Tokyo" in args


def test_linkedin_parses_records_and_skips_urlless_ones():
    jobs = LinkedInScraper().parse_cli_output(
        [
            {"title": "Product Designer", "company": "Acme",
             "url": "https://linkedin.com/jobs/1", "location": "Tokyo"},
            {"title": "No link", "company": "Nope"},
        ]
    )
    assert [job.url for job in jobs] == ["https://linkedin.com/jobs/1"]
    assert jobs[0].source_platform is SourcePlatform.LINKEDIN


def test_linkedin_keeps_a_placeholder_title():
    (job,) = LinkedInScraper().parse_cli_output([{"url": "https://linkedin.com/jobs/2"}])
    assert job.title == "Untitled role"


@pytest.mark.parametrize(
    ("raw", "expected_count"),
    [
        ([{"a": 1}], 1),
        ({"jobs": [{"a": 1}]}, 1),
        ({"data": [{"a": 1}]}, 1),
        ({"results": [{"a": 1}]}, 1),
        ({"jobs": []}, 0),
        ([{"a": 1}, "junk"], 1),
    ],
)
def test_cli_record_unwrapping(raw: object, expected_count: int):
    assert len(_records_from(raw)) == expected_count


def test_cli_record_unwrapping_rejects_garbage():
    with pytest.raises(ScraperError):
        _records_from("nope")

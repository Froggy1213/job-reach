"""Scraper strategies: URL construction, relevance matching, parsing, plumbing.

Nothing here launches a browser or hits the network — pages and API payloads are
fed in as data, which is precisely why the parsers were written as separate
methods. The fetch layer itself is covered by ``test_fetchers.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jobreach.domain import SourcePlatform
from jobreach.errors import MissingDependencyError, ScraperError
from jobreach.fetchers import FetchResult
from jobreach.htmlextract import next_data
from jobreach.scrapers import (
    SCRAPERS,
    available_sources,
    build_scrapers,
    check_source_requirements,
    is_cli_scraper,
    needs_browser,
    scraper_for,
)
from jobreach.scrapers.cli_base import _records_from
from jobreach.scrapers.daijob import DaijobScraper
from jobreach.scrapers.green import GreenScraper
from jobreach.scrapers.indeed import LOCATION_SLUGS, IndeedScraper
from jobreach.scrapers.japandev import JapanDevScraper
from jobreach.scrapers.linkedin import LinkedInScraper
from jobreach.scrapers.mynavi2027 import Mynavi2027Scraper
from jobreach.scrapers.wantedly import WantedlyScraper

# --- registry --------------------------------------------------------------


def test_every_board_has_a_scraper():
    """Indeed used to be ingest-only; the stealth browser graduated it."""
    assert set(available_sources()) == {
        "wantedly", "indeed", "green", "daijob", "japandev", "mynavi2027", "linkedin",
    }
    assert set(SCRAPERS) == set(available_sources())


def test_build_scrapers_passes_the_query_through():
    (scraper,) = build_scrapers(["wantedly"], keyword=" designer ", location="osaka")
    assert scraper.keyword == "designer"
    assert scraper.location == "osaka"


def test_build_scrapers_instantiates_indeed():
    (scraper,) = build_scrapers(["indeed"], keyword="デザイナー")
    assert isinstance(scraper, IndeedScraper)
    assert scraper.platform is SourcePlatform.INDEED


def test_build_scrapers_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown source"):
        build_scrapers(["monster"])


def test_scraper_for_returns_none_for_unknown_boards():
    assert scraper_for("monster") is None
    assert scraper_for("indeed") is IndeedScraper


def test_linkedin_is_the_only_cli_board():
    assert is_cli_scraper("linkedin")
    assert not is_cli_scraper("wantedly")


# --- browser requirements --------------------------------------------------


def test_only_browser_boards_are_probed(monkeypatch: pytest.MonkeyPatch):
    """Wantedly is HTTP and LinkedIn is a CLI: neither may need a browser."""

    def _explode():  # pragma: no cover - would fail the test if called
        raise AssertionError("a board without a browser must not trigger a probe")

    monkeypatch.setattr("jobreach.scrapers.base.probe_browser_stack", _explode)
    assert check_source_requirements(["wantedly", "linkedin"]) == {}


def test_requirements_report_a_missing_browser_for_every_browser_board(
    monkeypatch: pytest.MonkeyPatch,
):
    """Without any backend, browser boards report the fix — not a crash.

    The probe is patched at its **definition site** (``scrapers.base``), which is
    the only place it is looked up: ``check_source_requirements`` calls it
    through the module precisely so that seam exists. Patching a name imported
    into ``scrapers/__init__`` would silently do nothing, and this test would
    then only pass on machines that happen to lack a browser.
    """

    def _missing():
        raise MissingDependencyError("no browser backend here", hint="run setup")

    monkeypatch.setattr("jobreach.scrapers.base.probe_browser_stack", _missing)
    problems = check_source_requirements(list(SCRAPERS))
    assert set(problems) == {"mynavi2027", "indeed"}
    assert "run setup" in problems["indeed"]


def test_requirements_are_clean_when_a_backend_exists(monkeypatch: pytest.MonkeyPatch):
    """The mirror case, so the test above cannot pass for the wrong reason."""
    monkeypatch.setattr(
        "jobreach.scrapers.base.probe_browser_stack", lambda: ("scrapling", "Scrapling 0.4")
    )
    assert check_source_requirements(list(SCRAPERS)) == {}


def test_needs_browser_matches_the_implementation():
    assert needs_browser("indeed") is True
    assert needs_browser("mynavi2027") is True
    assert needs_browser("wantedly") is False
    assert needs_browser("green") is False  # JSON embedded in the page
    assert needs_browser("daijob") is False  # server-rendered HTML
    assert needs_browser("japandev") is False  # server-rendered HTML
    assert needs_browser("linkedin") is False  # CLI-driven, not a browser
    assert needs_browser("monster") is False


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
    assert WantedlyScraper(keyword="engineer").matches("バックエンドエンジニア募集") is True
    assert IndeedScraper(keyword="designer").matches("【アートディレクター】") is True


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


# --- Wantedly (JSON API) ---------------------------------------------------


def project(project_id: int = 2436572, **overrides) -> dict:
    record = {
        "id": project_id,
        "title": "UI Designer 募集",
        "company": {"id": 1, "name": "株式会社テスト"},
        "location": "東京都渋谷区道玄坂１丁目",
        "location_suffix": "渋谷ビル 5階",
        "description": "■仕事内容\nプロダクトのUI設計",
        "published_at": "2026-03-11T17:42:31.063+09:00",
    }
    record.update(overrides)
    return record


def test_wantedly_api_url_carries_keyword_area_and_page():
    url = WantedlyScraper(keyword="frontend engineer", location="osaka").build_api_url(2)
    assert url.startswith("https://www.wantedly.com/api/v1/projects?")
    assert "q=frontend+engineer" in url
    assert "areas=osaka" in url
    assert "page=2" in url


@pytest.mark.parametrize("sentinel", ["any", "all", "Japan"])
def test_wantedly_location_sentinel_disables_the_area_filter(sentinel: str):
    assert "areas=" not in WantedlyScraper(location=sentinel).build_api_url(1)


def test_wantedly_needs_no_browser_at_all():
    """The point of the API rewrite: no Chromium, no venv, no Playwright."""
    scraper = WantedlyScraper()
    assert scraper.needs_browser is False
    assert scraper.fetch_modes  # inherited, but never reached for this board


def test_wantedly_parses_projects_into_postings():
    (job,) = WantedlyScraper()._parse_projects([project()])
    assert job.url == "https://www.wantedly.com/projects/2436572"
    assert job.company == "株式会社テスト"
    assert job.source_platform is SourcePlatform.WANTEDLY
    assert job.location.startswith("東京都渋谷区")
    assert job.posted_at is not None and job.posted_at.year == 2026
    assert job.description.startswith("■仕事内容")


def test_wantedly_default_feed_filters_by_title():
    """With no keyword the client-side design filter is the only filter."""
    records = [project(1, title="UI Designer"), project(2, title="営業マネージャー")]
    assert [job.title for job in WantedlyScraper()._parse_projects(records)] == ["UI Designer"]


def test_wantedly_keyword_search_trusts_the_server():
    records = [project(1, title="バックエンドエンジニア募集")]
    jobs = WantedlyScraper(keyword="engineer")._parse_projects(records)
    assert len(jobs) == 1


def test_wantedly_drops_malformed_projects():
    jobs = WantedlyScraper()._parse_projects(
        [
            {"id": 1, "title": "UI Designer"},
            {"id": 2},  # no title
            "nonsense",
            project(3, company=None),  # company missing → Unknown, still kept
        ]
    )
    assert [job.url.rsplit("/", 1)[-1] for job in jobs] == ["1", "3"]
    assert jobs[1].company == "Unknown"


def test_wantedly_marks_remote_roles():
    (job,) = WantedlyScraper()._parse_projects(
        [project(location="オンライン", location_suffix="")]
    )
    assert job.location.startswith("Remote")


def test_wantedly_pagination_stops_at_the_api_limit(monkeypatch: pytest.MonkeyPatch):
    """Two API pages, then stop — the page count the API reports is respected."""
    pages = {
        1: {"data": [project(i) for i in (1, 2)], "_metadata": {"total_pages": 2}},
        2: {"data": [project(i) for i in (3, 4)], "_metadata": {"total_pages": 2}},
    }
    calls: list[int] = []

    def fake_fetch(self, page_number: int) -> dict:
        calls.append(page_number)
        return pages[page_number]

    monkeypatch.setattr(WantedlyScraper, "_fetch_page", fake_fetch)
    jobs = asyncio.run(WantedlyScraper(keyword="designer").fetch_jobs())
    assert calls == [1, 2]
    assert len(jobs) == 4


def test_wantedly_reports_an_api_failure_instead_of_empty_results(
    monkeypatch: pytest.MonkeyPatch,
):
    from jobreach.webclient import HttpError

    def boom(self, page_number: int) -> dict:
        raise HttpError("HTTP 403 Forbidden", status=403, url="https://api.test")

    monkeypatch.setattr(WantedlyScraper, "_fetch_page", boom)
    with pytest.raises(ScraperError, match="HTTP 403"):
        asyncio.run(WantedlyScraper(keyword="designer").fetch_jobs())


# --- Indeed ----------------------------------------------------------------


def test_indeed_search_url_uses_the_japanese_market():
    url = IndeedScraper(keyword="web designer", location="tokyo").build_search_url(1)
    assert url.startswith("https://jp.indeed.com/jobs?")
    assert "q=web+designer" in url
    assert "hl=ja" in url
    assert "l=%E6%9D%B1%E4%BA%AC" in url  # 東京
    assert "start=" not in url


def test_indeed_default_query_is_the_design_feed():
    scraper = IndeedScraper(location="tokyo")
    assert scraper.query == "デザイナー"
    assert IndeedScraper(location="tokyo").build_search_url(1)


def test_indeed_pages_with_a_zero_based_start():
    url = IndeedScraper(keyword="engineer", location="tokyo").build_search_url(2)
    assert "start=15" in url


def test_indeed_location_accepts_slugs_and_place_names():
    assert IndeedScraper(location="osaka").place == LOCATION_SLUGS["osaka"]
    assert IndeedScraper(location="大阪").place == "大阪"
    assert IndeedScraper(location="any").place == ""
    assert "&l=" not in IndeedScraper(location="any").build_search_url(1)


def indeed_card(**overrides) -> dict:
    card = {
        "jk": "bcf91e657e236bc7",
        "title": "【アートディレクター】リモート可",
        "company": "株式会社テスト",
        "location": "東京都 23区",
        "salary": "月給 27.6万円 ~ 40.1万円",
        "snippet": "カード全体のテキスト",
        "url": "https://jp.indeed.com/viewjob?jk=bcf91e657e236bc7",
    }
    card.update(overrides)
    return card


def test_indeed_parses_cards():
    (job,) = IndeedScraper(keyword="designer")._parse_cards([indeed_card()])
    assert job.url == "https://jp.indeed.com/viewjob?jk=bcf91e657e236bc7"
    assert job.company == "株式会社テスト"
    assert job.salary == "月給 27.6万円 ~ 40.1万円"
    assert job.source_platform is SourcePlatform.INDEED


def test_indeed_drops_foreign_and_malformed_cards():
    jobs = IndeedScraper(keyword="designer")._parse_cards(
        [
            indeed_card(),
            indeed_card(url="https://www.indeed.com/viewjob?jk=us1"),  # US market
            indeed_card(title="", url="https://jp.indeed.com/viewjob?jk=x"),
            {"url": "https://jp.indeed.com/viewjob?jk=no-title"},  # no title
        ]
    )
    assert [job.url for job in jobs] == ["https://jp.indeed.com/viewjob?jk=bcf91e657e236bc7"]


def test_indeed_ignores_non_card_payloads():
    assert IndeedScraper(keyword="designer")._parse_cards([]) == []


def test_indeed_asks_for_the_stealth_browser_first():
    """Cloudflare is the reason this board was unsupported for so long."""
    assert IndeedScraper.fetch_modes[0] == "stealthy"
    assert "dynamic" in IndeedScraper.fetch_modes


# --- Mynavi ----------------------------------------------------------------


def mynavi_card(**overrides) -> dict:
    card = {
        "company": "Acme",
        "matchedText": "Acme | Webデザイン",
        "location": "Tokyo",
        "url": "https://job.mynavi.jp/corp1",
    }
    card.update(overrides)
    return card


def test_mynavi_parses_and_deduplicates_cards():
    seen: set[str] = set()
    jobs = Mynavi2027Scraper()._parse_cards(
        [mynavi_card(), mynavi_card()], "WEBデザイナー", seen
    )
    assert len(jobs) == 1
    assert jobs[0].title == "Acme (WEBデザイナー)"


def test_mynavi_declares_its_title_synthetic():
    """The title is built from the occupation code, so it must not be evidence.

    Every card under 415 is titled "…（WEBデザイナー）" whatever the company
    actually recruits for; a relevance filter reading that title would find its
    own search term in it and keep the entire board.
    """
    jobs = Mynavi2027Scraper()._parse_cards(
        [mynavi_card(cardText="Webデザイン、バナー制作、コーディング")],
        "WEBデザイナー",
        set(),
    )
    assert jobs[0].title_is_synthetic is True
    assert jobs[0].description == "Webデザイン、バナー制作、コーディング"


def test_mynavi_filters_non_design_cards():
    jobs = Mynavi2027Scraper()._parse_cards(
        [mynavi_card(matchedText="Acme | 営業", url="https://job.mynavi.jp/corp9")],
        "WEBデザイナー",
        set(),
    )
    assert jobs == []


def test_mynavi_pagination_is_an_optional_click(monkeypatch: pytest.MonkeyPatch):
    """A pager that does not exist is the last page — not a failure.

    The step list is what a browser backend executes, so this asserts the
    contract between the scraper and the fetch layer without launching one.
    """
    captured: dict[str, object] = {}

    async def fake_fetch(self, url, *, steps=(), **kwargs):
        captured["url"] = url
        captured["steps"] = list(steps)
        return FetchResult(status=200, url=url, title="", results={})

    monkeypatch.setattr("jobreach.scrapers.base.BaseScraper.fetch", fake_fetch)
    asyncio.run(Mynavi2027Scraper()._scrape_occupation("415", "WEBデザイナー", set()))

    steps = captured["steps"]
    assert isinstance(steps, list)
    assert captured["url"] == "https://job.mynavi.jp/27/pc/search/occ415.html"
    clicks = [step for step in steps if "click" in step]
    assert clicks, "the occupation must follow its pager"
    assert all(step.get("optional") for step in clicks), "a missing pager is not an error"
    evaluates = [step for step in steps if "evaluate" in step]
    assert [step["key"] for step in evaluates] == ["page1", "page2"]


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


# --- Green (JSON embedded in the page) -------------------------------------

#: Real markup captured from the live sites, trimmed to a couple of cards.
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def green_jobs(keyword: str | None = "デザイナー"):
    html = fixture("green_search.html")
    offers = GreenScraper.offers_in(next_data(html) or {})
    return GreenScraper(keyword=keyword)._parse_offers(offers)


def test_green_reads_its_embedded_payload():
    jobs = green_jobs()
    assert len(jobs) == 2
    first = jobs[0]
    assert first.source_platform is SourcePlatform.GREEN
    assert first.url.startswith("https://www.green-japan.com/company/")
    assert first.company and first.location and first.salary
    assert first.posted_at is not None


def test_green_maps_slugs_and_place_names_to_area_ids():
    assert GreenScraper(location="tokyo").area_id == 13
    assert GreenScraper(location="大阪").area_id == 27
    assert GreenScraper(location="any").area_id is None
    assert GreenScraper(location="99").area_id == 99  # a raw id passes through


def test_green_url_encodes_keyword_area_and_page():
    url = GreenScraper(keyword="デザイナー", location="tokyo").build_search_url(2)
    assert url.startswith("https://www.green-japan.com/search?")
    assert "area_ids=13" in url and "page=2" in url
    assert "keyword=%E3%83%87%E3%82%B6%E3%82%A4%E3%83%8A%E3%83%BC" in url


def test_green_skips_offers_without_a_title_or_url():
    jobs = GreenScraper()._parse_offers(
        [
            {"name": "UI Designer", "jobOfferUrl": "/company/1/job/2"},
            {"name": "No URL"},
            {"jobOfferUrl": "/company/1/job/3"},
            "junk",
        ]
    )
    assert [job.title for job in jobs] == ["UI Designer"]


def test_green_reports_a_missing_payload_path():
    with pytest.raises(ScraperError, match="job offers"):
        GreenScraper.offers_in({"props": {"pageProps": {}}})


# --- Daijob (server-rendered HTML) -----------------------------------------


def daijob_jobs(keyword: str | None = "designer"):
    scraper = DaijobScraper(keyword=keyword)
    return scraper._parse_cards(scraper.card_chunks(fixture("daijob_search.html")))


def test_daijob_reads_cards_from_real_markup():
    jobs = daijob_jobs()
    assert len(jobs) == 2
    first = jobs[0]
    assert first.source_platform is SourcePlatform.DAIJOB
    assert first.url.startswith("https://www.daijob.com/jobs/detail/")
    assert first.company != "Unknown"
    assert first.salary
    assert first.description


def test_daijob_compacts_the_location_cell():
    """The cell reads "アジア 日本 東京都 新宿区 / …" — the prefix is noise."""
    location = daijob_jobs()[0].location
    assert "アジア" not in location and "日本" not in location
    assert location.startswith("東京都")
    assert " / " in location  # several offices are kept, deduplicated


def test_daijob_url_scopes_to_japan_and_the_prefecture():
    url = DaijobScraper(keyword="designer", location="tokyo").build_search_url(2)
    assert "la=102" in url and "ac=118" in url and "page=2" in url
    assert "keyword=designer" in url


def test_daijob_location_sentinel_drops_the_prefecture_filter():
    url = DaijobScraper(location="any").build_search_url(1)
    assert "ac=" not in url and "la=" not in url


# --- Japan Dev (server-rendered HTML, client-side search) ------------------


def japandev_jobs(keyword: str | None = None):
    scraper = JapanDevScraper(keyword=keyword)
    return scraper._parse_cards(scraper.card_chunks(fixture("japandev_jobs.html")))


def test_japandev_reads_cards_from_real_markup():
    jobs = japandev_jobs(keyword="engineer")
    assert jobs, "the fixture cards are engineering roles"
    first = jobs[0]
    assert first.source_platform is SourcePlatform.JAPAN_DEV
    assert first.url.startswith("https://japan-dev.com/jobs/")
    assert first.company != "Unknown"
    assert first.location in {"Tokyo", "Remote", "Japan"} or first.location


def test_japandev_filters_client_side():
    """The site ignores ?query= (verified), so relevance filtering is ours."""
    assert JapanDevScraper(keyword="designer")._parse_cards(
        JapanDevScraper.card_chunks(fixture("japandev_jobs.html"))
    ) == []
    assert JapanDevScraper().matches("Designer (Sales AI Agent Business)") is True
    assert JapanDevScraper().matches("Production Engineer, Trading") is False


def test_japandev_keeps_the_visa_and_language_flags():
    jobs = japandev_jobs(keyword="engineer")
    assert any(job.description for job in jobs), "the flags are the point of this board"
    assert not any("Japanese required · No Japanese required" in (job.description or "") for job in jobs)


def test_japandev_sends_no_parameters_because_the_site_ignores_them():
    assert JapanDevScraper(keyword="designer", location="osaka").build_list_url() == "https://japan-dev.com/jobs"

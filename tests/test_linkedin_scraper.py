"""Tests for LinkedIn CLI scraper — parsing logic (no network)."""

import pytest
from models.enums import SourcePlatform
from scrapers.cli.linkedin import LinkedInScraper


class TestLinkedInScraper:
    """Unit tests for LinkedInScraper._parse_cli_output."""

    def test_platform_is_linkedin(self):
        scraper = LinkedInScraper(headless=True)
        assert scraper.platform == SourcePlatform.LINKEDIN

    def test_parse_valid_records(self):
        scraper = LinkedInScraper(headless=True)
        raw = [
            {
                "title": "Senior Designer",
                "company": "Acme Corp",
                "url": "https://www.linkedin.com/jobs/view/12345",
                "location": "Tokyo, Japan",
                "salary": "¥8M-12M",
            },
            {
                "title": "Frontend Engineer",
                "company": "TechStart KK",
                "url": "https://www.linkedin.com/jobs/view/67890",
                "location": "Tokyo",
            },
        ]
        jobs = scraper._parse_cli_output(raw)
        assert len(jobs) == 2
        assert jobs[0].title == "Senior Designer"
        assert jobs[0].company == "Acme Corp"
        assert str(jobs[0].url) == "https://www.linkedin.com/jobs/view/12345"
        assert jobs[0].location == "Tokyo, Japan"
        assert jobs[0].salary == "¥8M-12M"
        assert jobs[0].source_platform == SourcePlatform.LINKEDIN
        assert jobs[1].salary is None

    def test_skip_records_without_url(self):
        scraper = LinkedInScraper(headless=True)
        raw = [
            {"title": "No URL", "company": "Ghost", "url": ""},
            {"title": "Has URL", "company": "Real", "url": "https://www.linkedin.com/jobs/view/1"},
        ]
        jobs = scraper._parse_cli_output(raw)
        assert len(jobs) == 1
        assert jobs[0].title == "Has URL"

    def test_skip_invalid_urls(self):
        scraper = LinkedInScraper(headless=True)
        raw = [
            {"title": "Bad URL", "company": "X", "url": "not-a-url"},
            {"title": "Good", "company": "Y", "url": "https://linkedin.com/jobs/view/2"},
        ]
        jobs = scraper._parse_cli_output(raw)
        assert len(jobs) == 1
        assert jobs[0].title == "Good"

    def test_empty_input(self):
        scraper = LinkedInScraper(headless=True)
        assert scraper._parse_cli_output([]) == []

    def test_unknown_company_defaults(self):
        scraper = LinkedInScraper(headless=True)
        raw = [
            {"title": "Designer", "url": "https://linkedin.com/jobs/view/3"},
        ]
        jobs = scraper._parse_cli_output(raw)
        assert jobs[0].company == "Unknown"

    def test_strip_whitespace(self):
        scraper = LinkedInScraper(headless=True)
        raw = [
            {
                "title": "  Product Designer  ",
                "company": "  CoolCo  ",
                "url": "https://linkedin.com/jobs/view/4",
                "location": "  Tokyo  ",
                "salary": "  ¥5M  ",
            },
        ]
        jobs = scraper._parse_cli_output(raw)
        assert jobs[0].title == "Product Designer"
        assert jobs[0].company == "CoolCo"
        assert jobs[0].location == "Tokyo"
        assert jobs[0].salary == "¥5M"

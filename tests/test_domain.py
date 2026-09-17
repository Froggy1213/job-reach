"""Domain model: validation, normalisation and wire format."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from jobreach.domain import (
    JobPosting,
    SourcePlatform,
    ValidationError,
    resolve_platform,
)


def make(**overrides: object) -> JobPosting:
    payload = {
        "title": "UI Designer",
        "company": "Acme",
        "url": "https://example.com/jobs/1",
        "location": "Tokyo",
        "source_platform": "wantedly",
    }
    payload.update(overrides)
    return JobPosting(**payload)  # type: ignore[arg-type]


def test_platform_string_is_accepted():
    assert make(source_platform="wantedly").source_platform is SourcePlatform.WANTEDLY


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("wantedly", SourcePlatform.WANTEDLY),
        ("mynavi", SourcePlatform.MYNAVI_2027),
        ("mynavi2027", SourcePlatform.MYNAVI_2027),
        ("mynavi_2027", SourcePlatform.MYNAVI_2027),
        ("LinkedIn", SourcePlatform.LINKEDIN),
        ("indeed", SourcePlatform.INDEED),
    ],
)
def test_platform_aliases(alias: str, expected: SourcePlatform):
    assert resolve_platform(alias) is expected


def test_unknown_platform_lists_valid_options():
    with pytest.raises(ValueError, match="unknown source"):
        resolve_platform("linkedln")


def test_fields_are_trimmed_and_truncated():
    job = make(title="  Designer  ", company="C" * 400)
    assert job.title == "Designer"
    assert len(job.company) == 256


def test_empty_title_is_rejected():
    with pytest.raises(ValidationError, match="title"):
        make(title="   ")


@pytest.mark.parametrize("bad_url", ["", "not-a-url", "ftp://example.com/x", "/relative"])
def test_invalid_urls_are_rejected(bad_url: str):
    with pytest.raises(ValidationError, match="url"):
        make(url=bad_url)


def test_url_fragment_is_dropped():
    assert make(url="https://example.com/j/1#apply").url == "https://example.com/j/1"


def test_posting_is_immutable():
    job = make()
    with pytest.raises(FrozenInstanceError):
        job.title = "changed"  # type: ignore[misc]


def test_with_new_flag_returns_a_copy():
    job = make()
    flagged = job.with_new_flag(True)
    assert flagged.is_new is True
    assert job.is_new is False


def test_to_dict_is_json_ready():
    payload = make(salary="600万").to_dict()
    assert payload["source_label"] == "Wantedly"
    assert payload["source_platform"] == "wantedly"
    assert payload["is_new"] is False
    assert isinstance(payload["scraped_at"], str)


def test_from_dict_defaults_optional_fields():
    job = JobPosting.from_dict({"title": "Engineer", "url": "https://x.test/1"})
    assert job.company == "Unknown"
    assert job.location == "Japan"
    assert job.source_platform is SourcePlatform.INDEED


@pytest.mark.parametrize(
    "record",
    [
        {"url": "https://x.test/1"},
        {"title": "Engineer"},
        "not-a-dict",
    ],
)
def test_from_dict_rejects_incomplete_records(record: object):
    with pytest.raises(ValidationError):
        JobPosting.from_dict(record)  # type: ignore[arg-type]

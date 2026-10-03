"""Tests for the Indeed block probe and cooldown.

All network calls are prevented: urllib and module seams are patched so nothing
leaves the machine.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.error
from pathlib import Path

import pytest

from jobreach.errors import ScraperError
from jobreach.scrapers.indeed import (
    BLOCK_COOLDOWN_SECONDS,
    IndeedScraper,
    clear_block,
    record_block,
)
from jobreach.scrapers.indeed import active_block as real_active_block
from jobreach.scrapers.indeed import block_evidence as real_block_evidence
from jobreach.webclient import ProbeResult, probe


class FakeHttpResponse(io.BytesIO):
    """Stub for urllib.request.urlopen responses."""

    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def get_content_charset(self) -> str:
        return "utf-8"

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def make_http_error(
    code: int,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> urllib.error.HTTPError:
    fp = io.BytesIO(body)
    hdrs = headers or {}
    return urllib.error.HTTPError("https://jp.indeed.com/jobs", code, "error", hdrs, fp)  # type: ignore[arg-type]


# --- webclient.probe --------------------------------------------------------


def test_probe_turns_403_into_data(monkeypatch: pytest.MonkeyPatch):
    """A 403 returns status 403, keeps headers and body, and does not raise."""
    def fake_urlopen(request, timeout):
        raise make_http_error(
            403,
            body=b"<html>Just a moment...</html>",
            headers={"CF-Mitigated": "challenge", "Content-Type": "text/html"},
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    res = probe("https://jp.indeed.com/jobs")
    assert res.status == 403
    assert res.headers.get("cf-mitigated") == "challenge"
    assert "Just a moment" in res.text
    assert res.error == ""


def test_probe_turns_connection_error_into_none_status(monkeypatch: pytest.MonkeyPatch):
    """A transport failure returns status None with error text."""
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    res = probe("https://jp.indeed.com/jobs")
    assert res.status is None
    assert "URLError" in res.error
    assert res.headers == {}
    assert res.text == ""


def test_probe_lowercases_headers(monkeypatch: pytest.MonkeyPatch):
    """Response header keys are normalized to lowercase."""
    def fake_urlopen(request, timeout):
        return FakeHttpResponse(
            body=b"OK",
            status=200,
            headers={"Content-Type": "text/html", "X-Varnish-ID": "1234"},
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    res = probe("https://jp.indeed.com/jobs")
    assert res.status == 200
    assert "content-type" in res.headers
    assert "x-varnish-id" in res.headers
    assert "Content-Type" not in res.headers
    assert res.text == "OK"
    assert res.error == ""


# --- block_evidence ---------------------------------------------------------


def test_block_evidence_http_403(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "jobreach.scrapers.indeed.probe",
        lambda url, timeout=10.0: ProbeResult(status=403, headers={}, text="", error=""),
    )
    evidence = real_block_evidence("https://jp.indeed.com/jobs")
    assert evidence == "HTTP 403 on a plain GET"


def test_block_evidence_cf_mitigated_on_200(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "jobreach.scrapers.indeed.probe",
        lambda url, timeout=10.0: ProbeResult(
            status=200,
            headers={"cf-mitigated": "challenge"},
            text="<html>challenge content</html>",
            error="",
        ),
    )
    evidence = real_block_evidence("https://jp.indeed.com/jobs")
    assert evidence == "cf-mitigated: challenge"


def test_block_evidence_body_marker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "jobreach.scrapers.indeed.probe",
        lambda url, timeout=10.0: ProbeResult(
            status=200,
            headers={},
            text="<html><title>Just a moment...</title><body>Checking your browser</body></html>",
            error="",
        ),
    )
    evidence = real_block_evidence("https://jp.indeed.com/jobs")
    assert evidence == "challenge page marker 'just a moment'"


def test_block_evidence_clean_200_is_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "jobreach.scrapers.indeed.probe",
        lambda url, timeout=10.0: ProbeResult(
            status=200,
            headers={},
            text="<html><body><div>Job Listings</div></body></html>",
            error="",
        ),
    )
    assert real_block_evidence("https://jp.indeed.com/jobs") is None


def test_block_evidence_unreachable_host_is_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "jobreach.scrapers.indeed.probe",
        lambda url, timeout=10.0: ProbeResult(
            status=None,
            headers={},
            text="",
            error="URLError: [Errno 8] nodename nor servname provided",
        ),
    )
    assert real_block_evidence("https://jp.indeed.com/jobs") is None


def test_block_evidence_404_is_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "jobreach.scrapers.indeed.probe",
        lambda url, timeout=10.0: ProbeResult(
            status=404,
            headers={},
            text="Not Found",
            error="",
        ),
    )
    assert real_block_evidence("https://jp.indeed.com/jobs") is None


# --- cooldown ---------------------------------------------------------------


def test_cooldown_recorded_block_is_truthy(data_home: Path):
    record_block("HTTP 403 on a plain GET")
    active = real_active_block()
    assert active is not None
    assert active[1] == "HTTP 403 on a plain GET"
    assert active[0] > time.time()


def test_cooldown_expired_is_ignored(data_home: Path):
    data_home.mkdir(parents=True, exist_ok=True)
    block_file = data_home / "indeed-block.json"
    block_file.write_text(
        json.dumps({
            "blocked_until": time.time() - 60,
            "evidence": "old block",
            "at": "2026-01-01T00:00:00Z",
        }),
        encoding="utf-8",
    )
    assert real_active_block() is None


def test_cooldown_disabled_by_env_var(monkeypatch: pytest.MonkeyPatch, data_home: Path):
    record_block("HTTP 403 on a plain GET")
    assert real_active_block() is not None
    monkeypatch.setenv("JOBREACH_INDEED_COOLDOWN", "0")
    assert real_active_block() is None


def test_cooldown_garbage_env_var_uses_default(monkeypatch: pytest.MonkeyPatch, data_home: Path):
    monkeypatch.setenv("JOBREACH_INDEED_COOLDOWN", "not-a-number")
    before = time.time()
    record_block("HTTP 403 on a plain GET")
    active = real_active_block()
    assert active is not None
    assert active[0] >= before + BLOCK_COOLDOWN_SECONDS - 5


def test_cooldown_corrupt_file_is_ignored(data_home: Path):
    data_home.mkdir(parents=True, exist_ok=True)
    block_file = data_home / "indeed-block.json"

    block_file.write_text("not json {{{", encoding="utf-8")
    assert real_active_block() is None

    block_file.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    assert real_active_block() is None

    block_file.write_text(json.dumps({"blocked_until": "bad-float"}), encoding="utf-8")
    assert real_active_block() is None

    block_file.write_text(json.dumps({"blocked_until": time.time() + 1000}), encoding="utf-8")
    assert real_active_block() is None


def test_clear_block_removes_state(data_home: Path):
    record_block("HTTP 403 on a plain GET")
    assert real_active_block() is not None
    clear_block()
    assert real_active_block() is None
    clear_block()


# --- fetch_jobs integration -------------------------------------------------


def test_fetch_jobs_with_blocking_probe_raises_scraper_error(
    monkeypatch: pytest.MonkeyPatch, data_home: Path
):
    dispatched = False

    async def fake_dispatch(self, spec: dict[str, object]) -> dict[str, object]:
        nonlocal dispatched
        dispatched = True
        return {"ok": True}

    monkeypatch.setattr(IndeedScraper, "_dispatch", fake_dispatch)
    monkeypatch.setattr("jobreach.scrapers.indeed.active_block", real_active_block)
    monkeypatch.setattr(
        "jobreach.scrapers.indeed.block_evidence",
        lambda url: "HTTP 403 on a plain GET",
    )

    scraper = IndeedScraper(keyword="designer")
    with pytest.raises(ScraperError, match="bot challenge") as excinfo:
        asyncio.run(scraper.fetch_jobs())

    err = str(excinfo.value)
    assert "cheap GET probe: HTTP 403 on a plain GET" in err
    assert not dispatched, "_dispatch must not be called when probe detects block"
    active = real_active_block()
    assert active is not None
    assert active[1] == "HTTP 403 on a plain GET"


def test_fetch_jobs_inside_active_cooldown_raises_and_never_probes(
    monkeypatch: pytest.MonkeyPatch, data_home: Path
):
    probed = False

    def fake_probe(url: str) -> str | None:
        nonlocal probed
        probed = True
        return "HTTP 403"

    dispatched = False

    async def fake_dispatch(self, spec: dict[str, object]) -> dict[str, object]:
        nonlocal dispatched
        dispatched = True
        return {"ok": True}

    monkeypatch.setattr(IndeedScraper, "_dispatch", fake_dispatch)
    monkeypatch.setattr("jobreach.scrapers.indeed.block_evidence", fake_probe)
    monkeypatch.setattr("jobreach.scrapers.indeed.active_block", real_active_block)

    record_block("HTTP 403 on a plain GET")
    scraper = IndeedScraper(keyword="designer")

    with pytest.raises(ScraperError, match="bot challenge") as excinfo:
        asyncio.run(scraper.fetch_jobs())

    err = str(excinfo.value)
    assert "skipped: HTTP 403 on a plain GET" in err
    assert "set JOBREACH_INDEED_COOLDOWN=0 to retry now" in err
    assert not probed, "block_evidence probe seam must not be called when under active cooldown"
    assert not dispatched, "_dispatch must not be called when under active cooldown"

"""The HTTP client: URL building, retries, and the failure taxonomy.

``urllib`` is stubbed at the single call site, so these tests describe the
behaviour a scraper depends on: which failures are retried, which are fatal, and
what the error message names (a scraper failure ends up in ``summary.errors``
for the agent to read).
"""

from __future__ import annotations

import gzip
import io
import json
import urllib.error

import pytest

from jobreach.webclient import (
    HttpError,
    build_url,
    get_json,
    request_text,
)


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object ``urlopen`` yields."""

    def __init__(self, body: bytes, *, gzipped: bool = False) -> None:
        super().__init__(body)
        self.headers = self
        self.gzipped = gzipped

    def get(self, key: str, default: str = "") -> str:
        if key.lower() == "content-encoding":
            return "gzip" if self.gzipped else ""
        return default

    def get_content_charset(self) -> str:
        return "utf-8"

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def gzipped(text: str) -> FakeResponse:
    return FakeResponse(gzip.compress(text.encode()), gzipped=True)


def plain(text: str) -> FakeResponse:
    return FakeResponse(text.encode())


# --- URL building ----------------------------------------------------------


def test_build_url_appends_parameters():
    url = build_url("https://a.test/api", {"q": "designer", "page": 2})
    assert url == "https://a.test/api?q=designer&page=2"


def test_build_url_skips_none_and_keeps_existing_query():
    url = build_url("https://a.test/api?x=1", {"q": None, "page": None})
    assert url == "https://a.test/api?x=1"


def test_build_url_encodes_japanese():
    assert "q=%E3%83%87%E3%82%B6%E3%82%A4%E3%83%8A%E3%83%BC" in build_url(
        "https://a.test", {"q": "デザイナー"}
    )


# --- successful requests ---------------------------------------------------


def test_get_json_decodes_a_gzipped_body(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout: gzipped(json.dumps({"data": [1]}))
    )
    assert get_json("https://a.test/api") == {"data": [1]}


def test_request_text_sends_a_browser_identity(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, str] = {}

    def fake_urlopen(request, timeout):
        seen.update({key.lower(): value for key, value in request.header_items()})
        return plain("ok")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert request_text("https://a.test") == "ok"
    assert seen["user-agent"].startswith("Mozilla/5.0")
    assert "ja" in seen["accept-language"]


# --- failure taxonomy ------------------------------------------------------

def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://a.test", code, "boom", {}, None)  # type: ignore[arg-type]


def test_a_client_error_is_fatal_and_not_retried(monkeypatch: pytest.MonkeyPatch):
    attempts: list[int] = []

    def fake_urlopen(request, timeout):
        attempts.append(1)
        raise http_error(404)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(HttpError) as excinfo:
        request_text("https://a.test", max_attempts=3)
    assert len(attempts) == 1, "a 404 will not improve on the second try"
    assert excinfo.value.status == 404


def test_a_server_error_is_retried_then_reported(monkeypatch: pytest.MonkeyPatch):
    attempts: list[int] = []

    def fake_urlopen(request, timeout):
        attempts.append(1)
        raise http_error(503)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("jobreach.webclient.time.sleep", lambda _seconds: None)

    with pytest.raises(HttpError, match="after 3 attempt"):
        request_text("https://a.test", max_attempts=3)
    assert len(attempts) == 3


def test_a_transient_error_can_still_succeed(monkeypatch: pytest.MonkeyPatch):
    attempts: list[int] = []

    def fake_urlopen(request, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise http_error(429)
        return plain('{"data": []}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("jobreach.webclient.time.sleep", lambda _seconds: None)
    assert get_json("https://a.test") == {"data": []}


def test_a_network_error_is_reported_with_its_type(monkeypatch: pytest.MonkeyPatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("nodename nor servname provided")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("jobreach.webclient.time.sleep", lambda _seconds: None)
    with pytest.raises(HttpError, match="URLError"):
        request_text("https://a.test", max_attempts=2)


def test_a_non_json_body_names_the_endpoint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: plain("<html>"))
    with pytest.raises(HttpError, match="did not return JSON"):
        get_json("https://a.test/api")

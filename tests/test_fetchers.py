"""The fetch layer: step vocabulary, backend selection, driver bridge.

Everything here is pure data or a stubbed subprocess — no browser is launched.
The pieces worth pinning are the ones that fail silently: a step the driver
would not understand, a backend chosen for the wrong reason, and the marked-JSON
contract with a driver that prints logs on the same stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jobreach import scrapling as bridge
from jobreach.domain import JobPosting, SourcePlatform
from jobreach.errors import ConfigError, MissingDependencyError, ScraperError
from jobreach.fetchers import (
    FetchResult,
    backend_preference,
    build_spec,
    click,
    evaluate,
    goto,
    scroll,
    select_backend,
    wait,
    wait_load,
    wait_selector,
)
from jobreach.scrapers.base import BaseScraper

# --- step vocabulary -------------------------------------------------------


def test_steps_serialise_to_plain_data():
    """The steps cross a process boundary as JSON, so they must be JSON."""
    steps = [
        wait(1_500),
        scroll(800, times=2, settle_ms=1_000),
        wait_selector("a[data-jk]", state="attached", timeout_ms=10_000),
        click('ul.pagingLink a:has-text("2")', optional=True),
        goto("https://example.test/jobs?start=10", wait_until="domcontentloaded"),
        wait_load("domcontentloaded"),
        evaluate("() => 1", "cards"),
    ]
    encoded = json.dumps(steps)
    assert json.loads(encoded) == steps
    assert goto("https://example.test") == {
        "goto": "https://example.test",
        "wait_until": "domcontentloaded",
    }


def test_scroll_keeps_at_least_one_pass():
    assert scroll(500, times=0)["times"] == 1


def test_build_spec_carries_the_page_contract():
    spec = build_spec(
        "https://example.test/jobs",
        [evaluate("() => 1", "cards")],
        mode="dynamic",
        wait_selector="a",
        headless=False,
        timeout_ms=12_000,
    )
    assert spec["url"] == "https://example.test/jobs"
    assert spec["mode"] == "dynamic"
    assert spec["headless"] is False
    assert spec["timeout_ms"] == 12_000
    assert spec["wait_selector"] == "a"
    assert spec["locale"] == "ja-JP" and spec["timezone_id"] == "Asia/Tokyo"
    assert spec["steps"][0]["key"] == "cards"


def test_build_spec_rejects_an_unknown_mode():
    with pytest.raises(ConfigError, match="unknown fetch mode"):
        build_spec("https://example.test", mode="teleport")


def test_build_spec_omits_wait_selector_when_not_asked():
    assert "wait_selector" not in build_spec("https://example.test")


# --- backend selection -----------------------------------------------------


def test_backend_preference_defaults_to_auto(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JOBREACH_BACKEND", raising=False)
    assert backend_preference() == "auto"
    monkeypatch.setenv("JOBREACH_BACKEND", "playwright")
    assert backend_preference() == "playwright"


def test_backend_preference_rejects_typos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBREACH_BACKEND", "selenium")
    with pytest.raises(ConfigError, match="not a backend"):
        backend_preference()


def _fake_probe(python: str, *, use_cache: bool = True):
    return bridge.Probe(True, "Scrapling 9.9.9", version="9.9.9")


def test_auto_prefers_scrapling(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JOBREACH_BACKEND", raising=False)
    monkeypatch.setattr("jobreach.scrapling.scrapling_python", lambda: ("/opt/py", "test"))
    monkeypatch.setattr("jobreach.scrapling.probe", _fake_probe)

    backend = select_backend()
    assert backend.name == "scrapling"
    assert backend.python == "/opt/py"
    assert backend.version == "9.9.9"


def test_auto_falls_back_to_playwright(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JOBREACH_BACKEND", raising=False)
    monkeypatch.setattr("jobreach.scrapling.scrapling_python", lambda: (None, ""))
    monkeypatch.setattr("jobreach.scrapers.base.probe_playwright", lambda: (True, "ok"))

    assert select_backend().name == "playwright"


def test_no_backend_reports_both_fixes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JOBREACH_BACKEND", raising=False)
    monkeypatch.setattr("jobreach.scrapling.scrapling_python", lambda: (None, ""))
    monkeypatch.setattr(
        "jobreach.scrapers.base.probe_playwright", lambda: (False, "no playwright")
    )

    with pytest.raises(MissingDependencyError) as excinfo:
        select_backend()
    hint = excinfo.value.hint
    assert "job-reach setup" in hint
    assert "JOBREACH_SCRAPLING_PYTHON" in hint


def test_a_pinned_backend_is_never_silently_swapped(monkeypatch: pytest.MonkeyPatch):
    """``JOBREACH_BACKEND=scrapling`` must fail loudly, not fall back."""
    monkeypatch.setenv("JOBREACH_BACKEND", "scrapling")
    monkeypatch.setattr("jobreach.scrapling.scrapling_python", lambda: (None, ""))
    with pytest.raises(MissingDependencyError, match="Scrapling"):
        select_backend()


def test_fetch_result_from_payload_keeps_results_and_blocked():
    result = FetchResult.from_payload(
        {"status": 200, "url": "https://x.test", "title": "t", "results": {"cards": [1]}}
    )
    assert result.get("cards") == [1]
    assert result.blocked is False
    assert FetchResult.from_payload({"blocked": True}).blocked is True


# --- the driver bridge -----------------------------------------------------


def test_result_line_is_read_past_log_noise():
    stdout = "\n".join(
        [
            "[2026-01-01] ERROR: No Cloudflare challenge found.",
            'some browser chatter {"json": true}',
            bridge.RESULT_MARKER + json.dumps({"ok": True, "results": {"cards": []}}),
        ]
    )
    payload = bridge._extract_payload(stdout)
    assert payload is not None and payload["ok"] is True


def test_missing_result_line_is_reported_as_none():
    assert bridge._extract_payload("nothing to see here") is None


def test_result_line_must_be_valid_json():
    assert bridge._extract_payload(bridge.RESULT_MARKER + "{oops") is None


def test_the_last_result_line_wins():
    stdout = "\n".join(
        [
            bridge.RESULT_MARKER + json.dumps({"ok": False, "error": "first"}),
            bridge.RESULT_MARKER + json.dumps({"ok": True, "results": {}}),
        ]
    )
    payload = bridge._extract_payload(stdout)
    assert payload is not None and payload["ok"] is True


def test_driver_env_drops_pythonpath(monkeypatch: pytest.MonkeyPatch):
    """A foreign interpreter must not inherit Hermes' PYTHONPATH."""
    monkeypatch.setenv("PYTHONPATH", "/hermes/stuff")
    monkeypatch.setenv("VIRTUAL_ENV", "/hermes/venv")
    env = bridge.driver_env()
    assert "PYTHONPATH" not in env
    assert "VIRTUAL_ENV" not in env
    assert env["JOBREACH_PLUGIN_DIR"]


def test_run_driver_needs_an_interpreter(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("jobreach.scrapling.scrapling_python", lambda: (None, ""))
    with pytest.raises(MissingDependencyError):
        bridge.run_driver({"url": "https://x.test"})


def test_run_driver_returns_the_payload(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("jobreach.scrapling.scrapling_python", lambda: (sys.executable, "test"))

    def fake_run(argv, **kwargs):
        assert argv[0] == sys.executable
        assert json.loads(kwargs["stdin"])["url"] == "https://x.test"
        return subprocess.CompletedProcess(
            argv, 0, bridge.RESULT_MARKER + json.dumps({"ok": True, "results": {}}), ""
        )

    monkeypatch.setattr("jobreach.scrapling.run_captured", fake_run)
    assert bridge.run_driver({"url": "https://x.test"})["ok"] is True


def test_run_driver_turns_a_failed_fetch_into_a_scraper_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("jobreach.scrapling.scrapling_python", lambda: (sys.executable, "test"))
    monkeypatch.setattr(
        "jobreach.scrapling.run_captured",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, bridge.RESULT_MARKER + json.dumps({"ok": False, "error": "blocked"}), ""
        ),
    )
    with pytest.raises(ScraperError, match="blocked"):
        bridge.run_driver({"url": "https://x.test"})


def test_run_driver_timeout_is_a_scraper_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("jobreach.scrapling.scrapling_python", lambda: (sys.executable, "test"))

    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr("jobreach.scrapling.run_captured", boom)
    with pytest.raises(ScraperError, match="did not finish"):
        bridge.run_driver({"url": "https://x.test"}, timeout=5)


def test_probe_is_cached_per_interpreter(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "0.4.15\n", "")

    monkeypatch.setattr("jobreach.scrapling.run_captured", fake_run)
    monkeypatch.setattr(bridge, "_PROBE_CACHE", {})

    first = bridge.probe(sys.executable)
    second = bridge.probe(sys.executable)
    assert first.ok and second.version == "0.4.15"
    assert len(calls) == 1, "a second probe of the same interpreter must be free"


def test_probe_reports_a_broken_install(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "jobreach.scrapling.run_captured",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, "", "ModuleNotFoundError: No module named 'scrapling'"
        ),
    )
    monkeypatch.setattr(bridge, "_PROBE_CACHE", {})
    outcome = bridge.probe(sys.executable)
    assert outcome.ok is False
    assert "scrapling" in outcome.detail


def test_env_override_is_the_first_candidate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    override = tmp_path / "python"
    override.write_text("#!/bin/sh\n")
    monkeypatch.setenv(bridge.SCRAPLING_PYTHON_ENV, str(override))
    first, source = bridge._candidate_pythons()[0]
    assert first == str(override)
    assert source == bridge.SCRAPLING_PYTHON_ENV


# --- the driver itself (step execution, no browser) ------------------------


class FakePage:
    """Records what the driver asked a page to do."""

    def __init__(self, *, present: set[str] | None = None) -> None:
        self.actions: list[tuple[str, object]] = []
        self._present = present if present is not None else set()
        self._content = "<html><title>t</title></html>"

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.actions.append(("goto", url))

    def wait_for_timeout(self, ms: int) -> None:
        self.actions.append(("wait", ms))

    def evaluate(self, js: str):
        self.actions.append(("evaluate", js))
        return ["card"] if js == "cards" else js

    def wait_for_load_state(self, state: str) -> None:
        self.actions.append(("load", state))

    def wait_for_selector(self, selector: str, state: str = "attached", timeout=None) -> None:
        self.actions.append(("selector", selector))

    def query_selector(self, selector: str):
        return object() if selector in self._present else None

    def click(self, selector: str) -> None:
        self.actions.append(("click", selector))

    def content(self) -> str:
        return self._content


def driver_module():
    from jobreach.drivers import scrapling_driver

    return scrapling_driver


def test_driver_executes_the_documented_steps():
    module = driver_module()
    page = FakePage(present={'ul.pagingLink a:has-text("2")'})
    results: dict = {}
    steps = [
        {"wait_ms": 100},
        {"scroll": 500, "times": 2, "wait_ms": 50},
        {"evaluate": "cards", "key": "page1"},
        {"click": 'ul.pagingLink a:has-text("2")', "optional": True},
        {"wait_load": "domcontentloaded"},
        {"evaluate": "cards", "key": "page2"},
        {"capture": "html"},
    ]
    module.run_steps(page, steps, results)

    assert results["page1"] == ["card"] and results["page2"] == ["card"]
    assert results["html"].startswith("<html>")
    assert page.actions.count(("wait", 50)) == 2  # two scroll passes
    assert ("click", 'ul.pagingLink a:has-text("2")') in page.actions


def test_driver_skips_an_absent_optional_click():
    module = driver_module()
    page = FakePage(present=set())
    results: dict = {}
    module.run_steps(page, [{"click": "pager", "optional": True}], results)
    assert ("click", "pager") not in page.actions


def test_driver_fails_loudly_on_a_required_step():
    module = driver_module()

    class Broken(FakePage):
        def evaluate(self, js: str):
            raise RuntimeError("detached from target")

    with pytest.raises(RuntimeError, match="step 0"):
        module.run_steps(Broken(), [{"evaluate": "cards", "key": "cards"}], {})


def test_driver_goto_ordering():
    """Driver executes goto before subsequent wait_load, wait_selector, and evaluate."""
    module = driver_module()
    page = FakePage()
    results: dict = {}
    steps = [
        {"goto": "https://example.test/page2", "wait_until": "domcontentloaded"},
        {"wait_load": "domcontentloaded"},
        {"wait_selector": "div.card"},
        {"evaluate": "cards", "key": "page2"},
    ]
    module.run_steps(page, steps, results)
    assert page.actions == [
        ("goto", "https://example.test/page2"),
        ("load", "domcontentloaded"),
        ("selector", "div.card"),
        ("evaluate", "cards"),
    ]


def test_driver_goto_failure_respects_optional():
    """A failing goto step is skipped when optional=True, but raises otherwise."""
    module = driver_module()

    class FailingGotoPage(FakePage):
        def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")

    # Optional goto: skipped silently, later steps still execute
    page = FailingGotoPage()
    results: dict = {}
    steps = [
        {"goto": "https://example.test/page2", "optional": True},
        {"evaluate": "cards", "key": "page2"},
    ]
    module.run_steps(page, steps, results)
    assert results.get("page2") == ["card"]
    assert page.actions == [("evaluate", "cards")]

    # Non-optional goto: raises RuntimeError
    with pytest.raises(RuntimeError, match="step 0 .* failed"):
        module.run_steps(
            FailingGotoPage(),
            [{"goto": "https://example.test/page2"}],
            {},
        )


def test_block_detection_only_fires_on_an_empty_challenge_page():
    module = driver_module()
    assert module.detect_block("<html>Just a moment…</html>", {}) is True
    assert module.detect_block("<html>Just a moment…</html>", {"cards": [1]}) is False
    assert module.detect_block("<html>15 jobs</html>", {}) is False


def test_block_detection_needs_an_empty_page():
    module = driver_module()
    # Summary pages (and Indeed's own footer) legitimately contain "Ray ID", so
    # a marker alone must never block a response that carried cards. A page
    # with real results is trusted; an empty page with a marker is a challenge.
    assert module.detect_block("<html>Ray ID: abc</html>", {"cards": [{"title": "x"}]}) is False
    assert module.detect_block("<html>Ray ID: abc</html>", {"cards": []}) is True
    assert module.detect_block("<html>nothing here</html>", {}) is False


# --- Playwright fallback (_run_playwright) ---------------------------------


class AsyncFakeLocator:
    """Async locator stub matching Playwright's locator interface."""

    def __init__(self, actions: list[tuple[str, object]], selector: str, present: bool = True) -> None:
        self.actions = actions
        self.selector = selector
        self.present = present

    async def count(self) -> int:
        return 1 if self.present else 0

    @property
    def first(self) -> AsyncFakeLocator:
        return self

    async def click(self) -> None:
        self.actions.append(("click", self.selector))


class AsyncFakePage:
    """Async page stub matching Playwright's Page interface used by _run_playwright."""

    def __init__(
        self,
        *,
        url: str = "https://example.test/initial",
        title: str = "Test Title",
        content: str = "<html><head><title>Test Title</title></head><body><h1>Cards</h1></body></html>",
        present_selectors: set[str] | None = None,
    ) -> None:
        self.actions: list[tuple[str, object]] = []
        self._url = url
        self._title = title
        self._content = content
        self._present_selectors = present_selectors if present_selectors is not None else set()

    @property
    def url(self) -> str:
        return self._url

    async def title(self) -> str:
        return self._title

    async def content(self) -> str:
        return self._content

    async def goto(self, url: str, *, wait_until: str = "domcontentloaded", timeout: int | None = None) -> None:
        self._url = url
        self.actions.append(("goto", url))

    async def wait_for_selector(
        self, selector: str, *, state: str = "attached", timeout: int | None = None
    ) -> None:
        self.actions.append(("selector", selector))

    async def wait_for_load_state(self, state: str = "domcontentloaded") -> None:
        self.actions.append(("load", state))

    async def wait_for_timeout(self, ms: int) -> None:
        self.actions.append(("wait", ms))

    async def evaluate(self, js: str) -> Any:
        self.actions.append(("evaluate", js))
        return ["card"] if js == "cards" else js

    def locator(self, selector: str) -> AsyncFakeLocator:
        return AsyncFakeLocator(self.actions, selector, present=selector in self._present_selectors)


class MinimalScraper(BaseScraper):
    """Minimal concrete scraper for testing BaseScraper fallback methods."""

    @property
    def platform(self) -> SourcePlatform:
        return SourcePlatform.INDEED

    async def fetch_jobs(self) -> list[JobPosting]:
        return []


def test_playwright_goto_ordering_and_payload(monkeypatch: pytest.MonkeyPatch):
    """Playwright backend executes goto before wait_load/selector/evaluate and returns payload."""
    fake_page = AsyncFakePage(
        url="https://example.test/initial",
        title="Indeed Jobs",
        content="<html><body><div class='job'>Engineer</div></body></html>",
        present_selectors={"button#filter"},
    )

    @contextlib.asynccontextmanager
    async def fake_browser_page(self):
        yield fake_page

    monkeypatch.setattr(BaseScraper, "browser_page", fake_browser_page)
    scraper = MinimalScraper()

    spec = {
        "url": "https://example.test/search",
        "mode": "dynamic",
        "steps": [
            {"scroll": 800, "times": 1},
            {"click": "button#filter"},
            {"goto": "https://example.test/search?start=10", "wait_until": "domcontentloaded"},
            {"wait_load": "domcontentloaded"},
            {"wait_selector": "div.job"},
            {"evaluate": "cards", "key": "page2_cards"},
            {"capture": "page2_html"},
        ],
    }

    payload = asyncio.run(scraper._run_playwright(spec))

    # Verify action order: initial goto, scroll, click, goto, load, selector, evaluate
    assert fake_page.actions == [
        ("goto", "https://example.test/search"),
        ("evaluate", "window.scrollBy(0, 800)"),
        ("click", "button#filter"),
        ("goto", "https://example.test/search?start=10"),
        ("load", "domcontentloaded"),
        ("selector", "div.job"),
        ("evaluate", "cards"),
    ]

    # Explicit ordering assertion: goto before wait_load, wait_selector, and evaluate
    goto_idx = fake_page.actions.index(("goto", "https://example.test/search?start=10"))
    load_idx = fake_page.actions.index(("load", "domcontentloaded"))
    sel_idx = fake_page.actions.index(("selector", "div.job"))
    eval_idx = fake_page.actions.index(("evaluate", "cards"))
    assert goto_idx < load_idx < sel_idx < eval_idx

    # Assert directly on returned payload dict
    assert payload == {
        "ok": True,
        "status": None,
        "url": "https://example.test/search?start=10",
        "title": "Indeed Jobs",
        "results": {
            "page2_cards": ["card"],
            "page2_html": "<html><body><div class='job'>Engineer</div></body></html>",
        },
        "blocked": False,
        "mode": "dynamic",
        "html_len": len("<html><body><div class='job'>Engineer</div></body></html>"),
    }


def test_playwright_goto_failure_respects_optional(monkeypatch: pytest.MonkeyPatch):
    """Playwright backend skips failing goto when optional=True, raises ScraperError otherwise."""
    monkeypatch.setattr("jobreach.scrapers.base.RETRY_BASE_DELAY", 0.0001)

    class FailingGotoPage(AsyncFakePage):
        async def goto(self, url: str, *, wait_until: str = "domcontentloaded", timeout: int | None = None) -> None:
            if "fail" in url:
                raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
            await super().goto(url, wait_until=wait_until, timeout=timeout)

    fake_page = FailingGotoPage()

    @contextlib.asynccontextmanager
    async def fake_browser_page(self):
        yield fake_page

    monkeypatch.setattr(BaseScraper, "browser_page", fake_browser_page)
    scraper = MinimalScraper()

    # Optional goto failure: skipped, subsequent evaluate succeeds
    spec_optional = {
        "url": "https://example.test/search",
        "steps": [
            {"goto": "https://example.test/fail", "optional": True},
            {"evaluate": "cards", "key": "cards"},
        ],
    }
    payload = asyncio.run(scraper._run_playwright(spec_optional))
    assert payload["ok"] is True
    assert payload["results"]["cards"] == ["card"]

    # Non-optional goto failure: raises ScraperError
    spec_required = {
        "url": "https://example.test/search",
        "steps": [
            {"goto": "https://example.test/fail"},
        ],
    }
    with pytest.raises(ScraperError, match="step 0 failed"):
        asyncio.run(scraper._run_playwright(spec_required))


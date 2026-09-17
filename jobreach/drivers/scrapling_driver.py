#!/usr/bin/env python3
"""Fetch one page with Scrapling, driven by a JSON spec on stdin.

This script is executed by the interpreter that has Scrapling installed — not
by the plugin's own engine, which is standard-library-only. The contract is
deliberately dumb, because the two sides live in different processes and often
in different virtualenvs:

stdin
    ``{"url": …, "mode": "stealthy"|"dynamic"|"basic", "headless": true,
       "timeout_ms": 90000, "wait_selector": "…", "steps": [ … ]}``

stdout
    exactly one line starting with ``__JOBREACH_RESULT__ `` holding
    ``{"ok": true, "status": 200, "url": …, "title": …, "results": {…}}``.

Everything else Scrapling prints (and it prints — "No Cloudflare challenge
found" on every non-challenged page) is noise on the same stream, which is why
the result is *marked* rather than being the whole body.

Steps are the vocabulary defined in :mod:`jobreach.fetchers` and implemented a
second time for Playwright in :mod:`jobreach.scrapers.base`; the two are kept
deliberately in step with each other.

Exit code is 0 whenever a result line was written — including a failed fetch,
which is reported *inside* the payload. A non-zero exit means the driver itself
could not run (bad spec, Scrapling import error), which is a different problem
and deserves a different message.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from typing import Any

MARKER = "__JOBREACH_RESULT__ "

#: Strings that mean "the site served a challenge instead of the page". Checked
#: only when no cards were extracted, to avoid failing a page that merely
#: mentions one of them.
BLOCK_MARKERS = (
    "Just a moment",
    "Additional Verification Required",
    "Checking your browser",
    "Enable JavaScript and cookies to continue",
    "cf-chl",
    "Ray ID",
)

DEFAULT_TIMEOUT_MS = 90_000


def emit(payload: dict[str, Any]) -> None:
    """Write the single marked result line and flush it."""
    sys.stdout.write("\n" + MARKER + json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def run_steps(page: Any, steps: list[dict[str, Any]], results: dict[str, Any]) -> None:
    """Execute the step list against a live page.

    Order inside one step: scroll → click → wait_load → wait_selector →
    evaluate → capture, with ``wait_ms`` meaning "settle". For a scroll step it
    waits after *each* pass (that is how lazy content is triggered); for every
    other step — including a bare ``{"wait_ms": …}`` — it waits once at the end.

    Raises:
        Exception: the first non-optional step that failed. ``optional`` steps
            (a pager that is absent on the last page) are skipped silently.
    """
    for index, step in enumerate(steps):
        try:
            settle_ms = int(step.get("wait_ms") or 0)
            waited_inside = False

            if "scroll" in step:
                pixels = int(step["scroll"])
                for _ in range(int(step.get("times") or 1)):
                    page.evaluate(f"window.scrollBy(0, {pixels})")
                    if settle_ms:
                        page.wait_for_timeout(settle_ms)
                waited_inside = True
            if "click" in step:
                if step.get("optional") and not page.query_selector(str(step["click"])):
                    continue
                page.click(str(step["click"]))
            if "wait_load" in step:
                page.wait_for_load_state(str(step["wait_load"]))
            if "wait_selector" in step:
                page.wait_for_selector(
                    str(step["wait_selector"]),
                    state=str(step.get("state") or "attached"),
                    timeout=step.get("timeout_ms"),
                )
            if "evaluate" in step:
                results[str(step.get("key") or f"eval{index}")] = page.evaluate(str(step["evaluate"]))
            if "capture" in step:
                results[str(step["capture"])] = page.content()
            if settle_ms and not waited_inside:
                page.wait_for_timeout(settle_ms)
        except Exception as exc:  # noqa: BLE001 — the step, not the fetch, failed
            if step.get("optional"):
                continue
            raise RuntimeError(
                f"step {index} ({', '.join(k for k in step if k != 'wait_ms') or 'unknown'}) failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc


def page_action_factory(steps: list[dict[str, Any]], results: dict[str, Any]):
    """Build the ``page_action`` callable Scrapling runs after navigation."""

    def page_action(page: Any) -> None:
        run_steps(page, steps, results)

    return page_action


def detect_block(html: str, results: dict[str, Any]) -> bool:
    """Whether the response looks like a bot challenge rather than results.

    Only consulted when the scraper asked for cards and got none — a challenge
    page is the one case where "no results" must not be reported as "no jobs".
    """
    if any(isinstance(value, list) and value for value in results.values()):
        return False
    return any(marker in html[:200_000] for marker in BLOCK_MARKERS)


def fetch(spec: dict[str, Any]) -> dict[str, Any]:
    """Run one fetch and return the payload (never raises)."""
    started = time.time()
    url = str(spec.get("url") or "")
    mode = str(spec.get("mode") or "stealthy")
    steps = list(spec.get("steps") or [])
    timeout_ms = int(spec.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    results: dict[str, Any] = {}

    if not url:
        return {"ok": False, "error": "the spec has no url", "url": url, "mode": mode}

    try:
        from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher
    except Exception as exc:  # noqa: BLE001 — a broken install, reported as data
        return {
            "ok": False,
            "error": f"Scrapling could not be imported: {type(exc).__name__}: {exc}",
            "url": url,
            "mode": mode,
        }

    kwargs: dict[str, Any] = {
        "timeout": timeout_ms,
        "page_action": page_action_factory(steps, results),
        "locale": str(spec.get("locale") or "ja-JP"),
        "timezone_id": str(spec.get("timezone_id") or "Asia/Tokyo"),
        "network_idle": False,
    }
    if spec.get("wait_selector"):
        kwargs["wait_selector"] = str(spec["wait_selector"])

    try:
        if mode == "basic":
            response = Fetcher.get(url, timeout=timeout_ms / 1000.0, stealthy_headers=True)
        else:
            fetcher = StealthyFetcher if mode == "stealthy" else DynamicFetcher
            kwargs["headless"] = bool(spec.get("headless", True))
            kwargs["block_ads"] = bool(spec.get("block_ads", True))
            if mode == "stealthy":
                kwargs["solve_cloudflare"] = True
            response = fetcher.fetch(url, **kwargs)
    except Exception as exc:  # noqa: BLE001 — every fetcher failure becomes data
        return {
            "ok": False,
            "error": f"{mode} fetch failed: {type(exc).__name__}: {exc}",
            "url": url,
            "mode": mode,
            "traceback": traceback.format_exc()[-1500:],
            "elapsed_s": round(time.time() - started, 2),
        }

    try:
        html = response.html_content
    except Exception:  # noqa: BLE001 — a body we cannot read is still a response
        html = ""

    return {
        "ok": True,
        "status": getattr(response, "status", None),
        "url": str(getattr(response, "url", url)),
        "title": (response.css("title::text").get() or "").strip()[:200],
        "results": results,
        "blocked": detect_block(html, results),
        "mode": mode,
        "elapsed_s": round(time.time() - started, 2),
        "html_len": len(html),
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        emit({"ok": False, "error": f"invalid spec JSON: {exc}"})
        return 0
    if not isinstance(spec, dict):
        emit({"ok": False, "error": "the spec must be a JSON object"})
        return 0

    try:
        payload = fetch(spec)
    except Exception as exc:  # noqa: BLE001 — last line of defence
        payload = {
            "ok": False,
            "error": f"driver crashed: {type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-1500:],
        }
    emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Direct HTTP for boards that expose a JSON API.

Not every board needs a browser. Wantedly, for instance, serves its search
results from ``/api/v1/projects`` as plain JSON with real server-side
filtering — no JavaScript, no Chromium, no stealth. Reading that endpoint with
:mod:`urllib.request` turns a 20-second browser scrape into a sub-second HTTP
call, and it keeps working when the browser stack is not installed at all.

This module is therefore the *fast path*: a tiny, standard-library HTTP client
with the three things a scraper actually needs —

* a plausible browser identity (boards reject default Python user agents),
* bounded retries with backoff on the failures that are worth retrying
  (timeouts, connection resets, 429 and 5xx), and
* one exception type (:class:`~jobreach.errors.ScraperError`) so the pipeline's
  per-board failure isolation keeps working unchanged.

Anything that needs JavaScript or a Cloudflare challenge solved belongs in
:mod:`jobreach.fetchers` instead.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .errors import ScraperError
from .logging_setup import get_logger

logger = get_logger("webclient")

#: Chrome-on-macOS UA, shared with the scrapers so the plugin looks like one
#: client rather than several. A default ``Python-urllib/3.x`` agent is refused
#: outright by most Japanese boards.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)

ACCEPT_LANGUAGE = "ja,en-US;q=0.9,en;q=0.8"

#: Document-ish Accept header for page-level probes (boards serve HTML, not JSON).
PROBE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

#: Maximum characters kept from a probe response body. Challenge pages are ~1 KB,
#: so 20 000 characters easily captures challenge signatures without blowing up memory.
PROBE_BODY_CHARS = 20_000

PROBE_DEFAULT_TIMEOUT = 10.0

#: Retries for transient failures. Three attempts over a few seconds is the
#: sweet spot: it rides out a flaky connection without stretching a search past
#: the agent's patience.
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.5

#: Status codes worth retrying — the request may succeed unchanged later.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})

DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The outcome of a non-raising HTTP probe.

    Unlike :class:`HttpError`, a probe represents the HTTP exchange as data
    whether it succeeded, returned an HTTP error status (such as a 403 challenge),
    or failed at the transport level.
    """

    status: int | None        # None when the request never produced a response
    headers: dict[str, str]   # keys lower-cased
    text: str                 # decoded body, truncated; "" when there is none
    error: str                # human-readable failure, "" on success


class HttpError(ScraperError):
    """Raised when a request fails after every retry.

    Carries the status code when there was one, so a caller can tell "blocked"
    (403/429 after retries) from "the endpoint moved" (404).
    """

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


def build_url(url: str, params: dict[str, Any] | None = None) -> str:
    """Append *params*, skipping ``None`` values, and return the full URL."""
    if not params:
        return url
    clean = {key: value for key, value in params.items() if value is not None}
    if not clean:
        return url
    separator = "&" if urllib.parse.urlparse(url).query else "?"
    return f"{url}{separator}{urllib.parse.urlencode(clean)}"


def request_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = MAX_ATTEMPTS,
) -> str:
    """GET *url* and return the decoded body.

    Raises:
        HttpError: when every attempt failed.
    """
    target = build_url(url, params)
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Accept": "application/json, text/plain, */*",
        # Ask for gzip explicitly and decode it here: urllib does not do it for
        # us, and the boards send it whether or not we ask.
        "Accept-Encoding": "gzip",
        **(headers or {}),
    }

    last_error: str = "no attempt was made"
    last_status: int | None = None

    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(target, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read()
                encoding = response.headers.get("Content-Encoding", "")
                if "gzip" in encoding:
                    raw = gzip.decompress(raw)
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code not in RETRY_STATUS:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < max_attempts:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "request failed, retrying",
                extra={"url": target, "attempt": attempt, "delay": delay, "error": last_error},
            )
            time.sleep(delay)

    raise HttpError(
        f"could not fetch {target} after {max_attempts} attempt(s): {last_error}",
        status=last_status,
        url=target,
    )


def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = MAX_ATTEMPTS,
) -> Any:
    """GET *url* and decode its JSON body.

    Raises:
        HttpError: the request failed, or the body was not JSON.
    """
    target = build_url(url, params)
    body = request_text(
        url, params=params, headers=headers, timeout=timeout, max_attempts=max_attempts
    )
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HttpError(
            f"{target} did not return JSON: {exc}; first 200 chars: {body[:200]!r}",
            url=target,
        ) from exc


def _lower_headers(raw_headers: Any) -> dict[str, str]:
    """Return response headers as a dictionary with lower-cased keys."""
    if raw_headers is None:
        return {}
    if hasattr(raw_headers, "items"):
        return {str(k).lower(): str(v) for k, v in raw_headers.items()}
    return {}


def _decode_probe_body(raw: bytes, raw_headers: Any) -> str:
    """Decompress gzip if present, decode with replacement, and truncate."""
    if not raw:
        return ""
    encoding = ""
    charset = None
    if raw_headers is not None:
        if hasattr(raw_headers, "get"):
            encoding = raw_headers.get("Content-Encoding", "") or raw_headers.get(
                "content-encoding", ""
            )
        if hasattr(raw_headers, "get_content_charset"):
            charset = raw_headers.get_content_charset()
    if "gzip" in str(encoding).lower():
        with contextlib.suppress(Exception):
            raw = gzip.decompress(raw)
    target_charset = charset or "utf-8"
    try:
        return raw.decode(target_charset, errors="replace")[:PROBE_BODY_CHARS]
    except Exception:  # noqa: BLE001 — fallback for unrecognized charset names
        return raw.decode("utf-8", errors="replace")[:PROBE_BODY_CHARS]


def probe(
    url: str,
    *,
    timeout: float = PROBE_DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> ProbeResult:
    """Perform a single, non-raising GET request to inspect the response.

    Unlike :func:`request_text`, which treats failures and non-2xx responses as
    exceptions and retries transient errors, ``probe`` makes exactly one GET
    attempt without retries and returns the outcome as data. A block probe wants
    an HTTP 403 *as the answer*, together with the ``cf-mitigated`` header and
    the challenge body, rather than an exception.

    Network or protocol errors return :class:`ProbeResult` with ``status=None``
    and ``error`` populated. This function never raises.
    """
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Accept": PROBE_ACCEPT,
        "Accept-Encoding": "gzip",
        **(headers or {}),
    }

    try:
        request = urllib.request.Request(url, headers=request_headers)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(status=None, headers={}, text="", error=f"{type(exc).__name__}: {exc}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
            status = getattr(response, "status", None)
            if status is None:
                status = getattr(response, "code", None)
            resp_headers = _lower_headers(getattr(response, "headers", None))
            body = _decode_probe_body(raw, getattr(response, "headers", None))
            return ProbeResult(
                status=int(status) if status is not None else 200,
                headers=resp_headers,
                text=body,
                error="",
            )
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = getattr(exc, "code", getattr(exc, "status", None))
        resp_headers = _lower_headers(getattr(exc, "headers", None))
        body = _decode_probe_body(raw, getattr(exc, "headers", None))
        return ProbeResult(
            status=int(status) if status is not None else None,
            headers=resp_headers,
            text=body,
            error="",
        )
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return ProbeResult(status=None, headers={}, text="", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(status=None, headers={}, text="", error=f"{type(exc).__name__}: {exc}")

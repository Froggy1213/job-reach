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

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
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

#: Retries for transient failures. Three attempts over a few seconds is the
#: sweet spot: it rides out a flaky connection without stretching a search past
#: the agent's patience.
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.5

#: Status codes worth retrying — the request may succeed unchanged later.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})

DEFAULT_TIMEOUT = 30.0


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

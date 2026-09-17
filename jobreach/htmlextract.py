"""Reading server-rendered HTML with the standard library only.

Two of the boards added in 2.2 (Daijob, Japan Dev) serve their results as plain
HTML — no JSON API, no JavaScript needed. That makes them cheap to read, but the
engine may not import BeautifulSoup (or any parser that is not stdlib), so this
module provides the *small* slice of that job the scrapers actually need:

* :func:`cards` — split a page into the repeating blocks (one per listing), with
  an HTML tokenizer rather than a regex, so nested tags inside a card cannot
  truncate it;
* :func:`first_link` / :func:`element_text` / :func:`definition` — pull one
  field out of a card;
* :func:`next_data` — the JSON payload Next.js embeds in the page
  (``<script id="__NEXT_DATA__">``), which is how Green serves its results.

The API is deliberately tiny and returns strings; every scraper still maps
records to the domain model itself, which is where the decisions belong.
"""

from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser

#: Elements that never have a closing tag. They must not affect nesting depth.
VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
     "meta", "param", "source", "track", "wbr"}
)

_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_tags(fragment: str) -> str:
    """Text content of *fragment*, whitespace-collapsed and unescaped."""
    text = _TAG_RE.sub(" ", fragment)
    text = unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


class _CardSplitter(HTMLParser):
    """Collects the raw HTML of every element matching a tag + class pair."""

    def __init__(self, tag: str, class_contains: str, limit: int | None) -> None:
        super().__init__(convert_charrefs=False)
        self.tag = tag
        self.class_contains = class_contains
        self.limit = limit
        self.found: list[str] = []
        self._depth = 0
        self._buffer: list[str] = []
        self._done = False

    # -- parser hooks --------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text() or ""
        if self._done:
            return
        if self._depth:
            self._buffer.append(raw)
            if tag not in VOID_ELEMENTS:
                self._depth += 1
            return
        if tag == self.tag and self._matches(attrs):
            self._buffer = [raw]
            self._depth = 1

    def handle_endtag(self, tag):
        if not self._depth:
            return
        if tag in VOID_ELEMENTS:
            # A self-closing void tag (<img/>, <br/>) fired handle_starttag —
            # which does not add depth — so it must not remove any either.
            # Counting it would end the card at the first image.
            return
        self._buffer.append(f"</{tag}>")
        # Any closing tag counts: an inner <div> must decrement too, or the
        # card's depth would grow forever and never close.
        self._depth -= 1
        if self._depth <= 0:
            self.found.append("".join(self._buffer))
            self._buffer = []
            self._depth = 0
            # Stop early once the caller has what it asked for: a listing page
            # can be a megabyte, and the tail is not worth parsing.
            if self.limit is not None and len(self.found) >= self.limit:
                self._done = True

    def handle_data(self, data):
        if self._depth:
            self._buffer.append(data)

    def handle_entityref(self, name):
        if self._depth:
            self._buffer.append(f"&{name};")

    def handle_charref(self, name):
        if self._depth:
            self._buffer.append(f"&#{name};")

    # -- helpers -------------------------------------------------------------

    def _matches(self, attrs) -> bool:
        for key, value in attrs:
            if key == "class" and value and self.class_contains in value:
                return True
        return False


def cards(html: str, *, tag: str = "li", class_contains: str = "", limit: int | None = None) -> list[str]:
    """Split *html* into the repeating listing blocks.

    Args:
        html: the page.
        tag: element name of one block (``li``, ``article``, ``div`` …).
        class_contains: substring that must appear in the block's ``class``.
        limit: stop after this many blocks.

    Returns:
        One raw HTML string per block, in document order.
    """
    splitter = _CardSplitter(tag, class_contains, limit)
    splitter.feed(html)
    splitter.close()
    return splitter.found[:limit] if limit else splitter.found


def links(
    fragment: str,
    *,
    href_contains: str | None = None,
) -> list[tuple[str, str]]:
    """Every ``<a>`` in *fragment* as ``(href, text)``, in document order."""
    found: list[tuple[str, str]] = []
    for match in re.finditer(r"<a\b([^>]*)>(.*?)</a>", fragment, re.S | re.I):
        href = _attribute(match.group(1), "href")
        if href is None:
            continue
        if href_contains and href_contains not in href:
            continue
        found.append((unescape(href), strip_tags(match.group(2))))
    return found


def first_link(
    fragment: str,
    *,
    href_contains: str | None = None,
    class_contains: str | None = None,
    attr: str | None = None,
    attr_value: str | None = None,
) -> tuple[str, str] | None:
    """Return ``(href, text)`` of the first matching ``<a>``, or ``None``.

    Args:
        fragment: card HTML.
        href_contains: require this substring in the href.
        class_contains: require this substring in the anchor's class.
        attr / attr_value: require an arbitrary attribute (e.g. ``id="_job"``).
    """
    for match in re.finditer(r"<a\b([^>]*)>(.*?)</a>", fragment, re.S | re.I):
        attrs, inner = match.group(1), match.group(2)
        href = _attribute(attrs, "href")
        if href is None:
            continue
        if href_contains and href_contains not in href:
            continue
        if class_contains and class_contains not in (_attribute(attrs, "class") or ""):
            continue
        if attr and (_attribute(attrs, attr) or "") != (attr_value or ""):
            continue
        return unescape(href), strip_tags(inner)
    return None


class _TextFinder(HTMLParser):
    """Text content of the first element matching a tag + class pair.

    Depth-tracked like :class:`_CardSplitter` (a regex cannot be trusted here:
    listing markup nests the same tag name several levels deep), and it stops
    at the element's own closing tag rather than at the first one it sees.
    """

    def __init__(self, tag: str, class_contains: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tag = tag
        self.class_contains = class_contains
        self.text = ""
        self.capturing = False
        self.done = False
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self.capturing:
            if tag not in VOID_ELEMENTS:
                self._depth += 1
            self.text += " "
            return
        if tag == self.tag and any(k == "class" and v and self.class_contains in v for k, v in attrs):
            self.capturing = True
            self._depth = 1

    def handle_endtag(self, tag):
        if self.done or not self.capturing:
            return
        if tag in VOID_ELEMENTS:
            # A self-closing void tag never added depth, so it may not remove
            # any either — counting it would end the read at the first image.
            return
        self._depth -= 1
        self.text += " "
        if self._depth <= 0:
            self.capturing = False
            self.done = True

    def handle_data(self, data):
        if self.capturing:
            self.text += data


def leaf_text(fragment: str, *, class_contains: str) -> str:
    """Text of the first element whose class matches, read up to its next tag.

    For *leaf* elements whose content is a single text node. It is safe on
    markup that a depth-tracking parser can be desynced by (Japan Dev's SSR
    emits elements without closing tags), at the price of stopping at the first
    child tag — so use it when the value cannot contain markup.
    """
    pattern = re.compile(
        r'class="[^"]*' + re.escape(class_contains) + r'[^"]*"[^>]*>([^<]*)',
        re.S | re.I,
    )
    match = pattern.search(fragment)
    return strip_tags(match.group(1)) if match else ""


def element_text(
    fragment: str,
    *,
    class_contains: str,
    tag: str = "div",
) -> str:
    """Text of the first *tag* element whose class contains *class_contains*.

    Nesting-aware: the text is everything inside that element (child-element
    boundaries read as spaces), so a container of tags comes back as one
    sentence rather than as run-together words.
    """
    if not fragment:
        return ""
    finder = _TextFinder(tag, class_contains)
    finder.feed(fragment)
    finder.close()
    return _WHITESPACE_RE.sub(" ", finder.text).strip()


def definition(fragment: str, label: str) -> str:
    """Value of a ``<dt>label</dt><dd>value</dd>`` pair, or ``""``.

    Daijob renders every structured field of a card this way (勤務地, 年収,
    仕事内容 …), so this is the one accessor its scraper needs for all of them.
    """
    pattern = re.compile(
        r"<dt\b[^>]*>\s*(?:<[^>]+>\s*)*" + re.escape(label) + r"\s*(?:</[^>]+>\s*)*</dt>\s*"
        r"<dd\b[^>]*>(.*?)</dd>",
        re.S | re.I,
    )
    match = pattern.search(fragment)
    return strip_tags(match.group(1)) if match else ""


def next_data(html: str, *, script_id: str = "__NEXT_DATA__") -> dict | None:
    """Decode the JSON payload Next.js embeds in a page, if present.

    Green ships its whole search result set there, which turns a JS-rendered
    board into a plain JSON read — the fastest kind of scraper this plugin has.
    """
    pattern = re.compile(
        r'<script[^>]*id="' + re.escape(script_id) + r'"[^>]*>(.*?)</script>', re.S
    )
    match = pattern.search(html)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _attribute(attrs: str, name: str) -> str | None:
    """Value of *name* inside a raw attribute string, or ``None``."""
    match = re.search(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"', attrs, re.I)
    if match:
        return match.group(1)
    match = re.search(rf"\b{re.escape(name)}\s*=\s*'([^']*)'", attrs, re.I)
    return match.group(1) if match else None

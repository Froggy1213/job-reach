"""The stdlib HTML helpers: card splitting, field reads, embedded JSON.

Every test here exists because the naive version of the same code failed on
real markup: a depth counter that ignored inner tags never closed a card; one
that counted ``<img/>`` closed cards at the first image; and a regex that read
"the element with this class" ran past the element on pages whose SSR leaves
tags unclosed. These are the lessons, pinned.
"""

from __future__ import annotations

import pytest

from jobreach.htmlextract import (
    cards,
    definition,
    element_text,
    first_link,
    leaf_text,
    links,
    next_data,
    strip_tags,
)

PAGE = """<html><body>
<ul class="results">
  <li class="job-item"><h2><a href="/jobs/acme/role-1" class="job-item__title">Role One</a></h2>
      <div class="job__tag-desc">Tokyo</div></li>
  <li class="other"><span>not a card</span></li>
  <li class="job-item"><h2><a href="/jobs/acme/role-2" class="job-item__title">Role Two</a></h2>
      <div class="job__tag-desc">Osaka</div></li>
</ul>
</body></html>"""


# --- cards() ---------------------------------------------------------------


def test_cards_returns_every_matching_block():
    chunks = cards(PAGE, tag="li", class_contains="job-item")
    assert len(chunks) == 2
    assert "Role One" in chunks[0] and "Role Two" in chunks[1]


def test_cards_keeps_nested_tags_inside_the_block():
    """A card full of nested divs must not end at the first inner close tag."""
    html = '<div class="card"><div><div><p>deep</p></div></div><span>tail</span></div><div class="card">two</div>'
    chunks = cards(html, tag="div", class_contains="card")
    assert len(chunks) == 2
    assert "tail" in chunks[0], "the block ended early"


def test_cards_is_not_confused_by_self_closing_void_tags():
    """<img/> fires a start *and* an end event; counting both ends the card."""
    html = '<li class="card"><img src="x.png" alt="logo"><span>title</span></li><li class="card">two</li>'
    chunks = cards(html, tag="li", class_contains="card")
    assert len(chunks) == 2
    assert "title" in chunks[0]


def test_cards_limit_stops_early():
    html = "".join(f'<li class="card">{i}</li>' for i in range(10))
    assert len(cards(html, tag="li", class_contains="card", limit=3)) == 3


def test_cards_merges_markup_that_never_closes_its_blocks():
    """Unclosed blocks cannot be split cleanly — they must not crash or hang."""
    html = '<li class="card"><div><li class="card">second'
    chunks = cards(html, tag="li", class_contains="card")
    assert len(chunks) <= 1


# --- field reads -----------------------------------------------------------


def test_first_link_filters_by_href_and_attribute():
    chunk = '<a href="/jobs/detail/1"><img alt="logo"></a><h2><a href="/jobs/detail/1" id="_job">Title</a></h2>'
    assert first_link(chunk, href_contains="/jobs/detail/", attr="id", attr_value="_job") == (
        "/jobs/detail/1",
        "Title",
    )
    assert first_link(chunk, href_contains="/nope/") is None


def test_links_keeps_document_order_and_skips_textless_ones():
    chunk = '<a href="/jobs/detail/1"><img></a><a href="/jobs/detail/1">Company</a>'
    found = links(chunk, href_contains="/jobs/detail/")
    assert found == [("/jobs/detail/1", ""), ("/jobs/detail/1", "Company")]


def test_leaf_text_stops_at_the_next_tag():
    """The reason this helper exists: unclosed SSR markup defeats a parser."""
    chunk = '<div class="job-item__contract-type">Two Sigma・Fintech<div>Japanese Required</div>'
    assert leaf_text(chunk, class_contains="job-item__contract-type") == "Two Sigma・Fintech"


def test_element_text_reads_a_container_of_tags():
    chunk = '<div class="tags"><span>Apply from Abroad</span><span>Partial Remote</span></div>'
    assert element_text(chunk, class_contains="tags") == "Apply from Abroad Partial Remote"


def test_element_text_stops_at_its_own_closing_tag():
    chunk = '<div class="x">inner</div><div class="x">second</div>'
    assert element_text(chunk, class_contains="x") == "inner"


def test_definition_reads_a_description_list_pair():
    chunk = "<dl><dt><span>勤務地</span></dt><dd><p>東京都 新宿区</p></dd><dt>年収</dt><dd>500万円</dd></dl>"
    assert definition(chunk, "勤務地") == "東京都 新宿区"
    assert definition(chunk, "年収") == "500万円"
    assert definition(chunk, "無い") == ""


def test_strip_tags_unescapes_entities_and_collapses_space():
    assert strip_tags("<b>A&amp;B</b>\n   <i>C</i>") == "A&B C"


# --- embedded JSON ---------------------------------------------------------


def test_next_data_decodes_the_payload():
    html = '<script id="__NEXT_DATA__" type="application/json">{"props": {"a": 1}}</script>'
    assert next_data(html) == {"props": {"a": 1}}


@pytest.mark.parametrize(
    "html",
    [
        "<html>no payload here</html>",
        '<script id="__NEXT_DATA__">{oops</script>',
        '<script id="__NEXT_DATA__">[1, 2]</script>',  # a list is not a payload
    ],
)
def test_next_data_returns_none_when_there_is_nothing_to_read(html: str):
    assert next_data(html) is None

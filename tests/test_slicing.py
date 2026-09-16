"""Byte-exact extraction of an element's inner markup.

The scanner is what lets ``description_html`` be labelled ``source-substring``.
Whenever it cannot demonstrate the element's extent it must say so rather than
hand back a slice that only looks right.
"""

from __future__ import annotations

from rowanjobs.extract.slicing import find_by_id, inner_source, slice_element


def slice_by_id(markup: str, element_id: str = "job-details", tag: str = "div"):
    return find_by_id(markup, element_id, tag)


def test_simple_element_is_sliced_exactly() -> None:
    markup = '<body><div id="job-details">hello</div><p>after</p></body>'
    found = slice_by_id(markup)
    assert found is not None
    assert found.exact is True
    assert inner_source(markup, found) == "hello"
    assert markup[found.start : found.end] == '<div id="job-details">hello</div>'


def test_nested_elements_of_the_same_tag_are_counted_not_guessed() -> None:
    markup = (
        '<body><div id="job-details"><div><div>deep</div></div>tail</div><div>next</div></body>'
    )
    found = slice_by_id(markup)
    assert found is not None
    assert found.exact is True
    assert inner_source(markup, found) == "<div><div>deep</div></div>tail"


def test_comment_containing_a_closing_tag_does_not_end_the_slice() -> None:
    markup = '<div id="job-details">a<!-- </div> not really --><p>b</p></div><div>x</div>'
    found = slice_by_id(markup)
    assert found is not None
    assert found.exact is True
    assert inner_source(markup, found) == "a<!-- </div> not really --><p>b</p>"


def test_script_body_containing_a_closing_tag_does_not_end_the_slice() -> None:
    markup = '<div id="job-details">a<script>var s = "</div>";</script>b</div><div>after</div>'
    found = slice_by_id(markup)
    assert found is not None
    assert found.exact is True
    assert inner_source(markup, found) == 'a<script>var s = "</div>";</script>b'


def test_attribute_value_containing_angle_brackets_does_not_confuse_the_scanner() -> None:
    markup = '<div id="job-details"><a title="a > b" href="/x">link</a></div>'
    found = slice_by_id(markup)
    assert found is not None
    assert found.exact is True
    assert inner_source(markup, found) == '<a title="a > b" href="/x">link</a>'


def test_void_and_self_closing_children_do_not_open_a_level() -> None:
    markup = '<div id="job-details">a<br><img src="x.png"><hr/>b</div><div>after</div>'
    found = slice_by_id(markup)
    assert found is not None
    assert found.exact is True
    assert inner_source(markup, found) == 'a<br><img src="x.png"><hr/>b'


def test_self_closing_target_element_has_an_empty_interior() -> None:
    markup = '<body><section id="s"/><p>after</p></body>'
    found = slice_element(markup, markup.index("<section"), "section")
    assert found.exact is True
    assert inner_source(markup, found) == ""


def test_void_target_element_has_an_empty_interior() -> None:
    markup = '<body><img src="x.png"><p>after</p></body>'
    found = slice_element(markup, markup.index("<img"), "img")
    assert found.exact is True
    assert inner_source(markup, found) == ""


def test_unbalanced_markup_is_reported_instead_of_claiming_byte_identity() -> None:
    markup = '<body><div id="job-details">never closed<p>more'
    found = slice_by_id(markup)
    assert found is not None
    assert found.exact is False
    assert found.reason is not None
    assert "no balancing </div>" in found.reason


def test_unquoted_and_single_quoted_id_attributes_are_both_found() -> None:
    for markup in (
        "<div id=job-details>x</div>",
        "<div id='job-details'>x</div>",
        '<div class="c" id="job-details" data-x="1">x</div>',
    ):
        found = slice_by_id(markup)
        assert found is not None, markup
        assert inner_source(markup, found) == "x"


def test_missing_element_returns_none_rather_than_an_empty_slice() -> None:
    assert slice_by_id("<div id='other'>x</div>") is None


def test_id_match_is_exact_and_not_a_prefix() -> None:
    assert slice_by_id('<div id="job-details-extra">x</div>') is None


def test_slice_element_works_from_an_explicit_offset() -> None:
    markup = "<p>lead</p><section>body</section>"
    start = markup.index("<section>")
    found = slice_element(markup, start, "section")
    assert found.exact is True
    assert inner_source(markup, found) == "body"

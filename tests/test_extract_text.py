"""The verbatim markup -> text contract.

These pin the promises in ``rowanjobs.extract.text``'s module docstring: an
archived description must read the way the source published it, with nothing
transliterated, corrected or invented.
"""

from __future__ import annotations

import pytest
from lxml import html

from rowanjobs.extract.pageup_detail import parse_detail
from rowanjobs.extract.text import html_string_to_text, html_to_text, inner_html

from .conftest import read_fixture


@pytest.fixture
def fidelity_markup() -> str:
    return read_fixture("detail_fidelity")


@pytest.fixture
def fidelity_text(fidelity_markup: str) -> str:
    extraction = parse_detail(fidelity_markup, "https://jobs.rowan.edu/en-us/job/502100/lab")
    assert extraction.description_text is not None
    return extraction.description_text


def _text(markup: str) -> str:
    return html_to_text(html.fromstring(markup))


def test_blocks_are_separated_by_one_blank_line_and_keep_source_order(fidelity_text: str) -> None:
    summary = fidelity_text.index("Summary:")
    duties = fidelity_text.index("Major Duties:")
    salary = fidelity_text.index("Salary Schedule:")
    assert summary < duties < salary
    assert "Summary:\n\nThe coordinator supports" in fidelity_text


def test_each_list_item_is_its_own_line_and_no_bullet_is_invented(fidelity_text: str) -> None:
    lines = fidelity_text.splitlines()
    assert "Maintain instructional equipment." in lines
    assert "scheduling" in lines
    assert "timesheet review" in lines
    # Ordered list items keep no invented numbering either.
    assert "First reporting step" in lines
    assert "1. First reporting step" not in fidelity_text
    # A bullet the source published itself is kept exactly as published.
    assert "• Order consumables." in lines
    assert not any(line.startswith(("- ", "* ")) for line in lines)


def test_table_cells_are_tab_joined_and_each_row_is_its_own_line(fidelity_text: str) -> None:
    lines = fidelity_text.splitlines()
    assert "Step\tAnnual\tEffective" in lines
    assert "Step 1\t$52,000\tJul 1 2026" in lines
    assert "Step 2\t$54,600\tJul 1 2027" in lines


def test_non_breaking_space_stays_u00a0_and_is_never_folded_to_a_plain_space(
    fidelity_text: str,
) -> None:
    assert "is $1.50 per hour & is paid monthly." in fidelity_text
    assert "is $1.50 per hour" not in fidelity_text


def test_curly_quotes_and_em_dashes_are_not_transliterated(fidelity_text: str) -> None:
    assert "Department’s teaching laboratories — including" in fidelity_text
    assert "“maker space”" in fidelity_text
    assert '"maker space"' not in fidelity_text
    assert "--" not in fidelity_text


def test_br_ends_the_line_without_ending_the_block(fidelity_text: str) -> None:
    assert "Contact the Chair:\nDr. A. Example\nexample@rowan.edu" in fidelity_text


def test_pre_whitespace_is_preserved_verbatim(fidelity_text: str) -> None:
    assert "Mon    08:00-16:00    Lab A\nTue    10:00-18:00    Lab B\n    (overlap shift)" in (
        fidelity_text
    )


def test_pre_keeps_blank_line_runs_that_collapse_everywhere_else() -> None:
    preformatted = _text("<div><pre>one\n    two\n\n\n\nthree\n</pre></div>")
    assert preformatted == "one\n    two\n\n\n\nthree\n"
    ordinary = _text("<div><p>one</p>\n\n\n\n   <p>   two   </p></div>")
    assert ordinary == "one\n\ntwo"


def test_script_style_and_comments_are_not_rendered(fidelity_text: str) -> None:
    assert "var applicants" not in fidelity_text
    assert "margin: 0" not in fidelity_text
    assert "internal note" not in fidelity_text


def test_entities_are_decoded_to_the_characters_they_denote() -> None:
    assert _text("<div><p>AT&amp;T &lt;tag&gt; &#8212; &#160;</p></div>") == ("AT&T <tag> —  ")


def test_source_indentation_collapses_but_words_and_single_spaces_survive() -> None:
    markup = "<div><p>one\n     two\tthree\r\nfour</p></div>"
    assert _text(markup) == "one two three four"


def test_html_to_text_excludes_the_elements_own_tail() -> None:
    doc = html.fromstring("<div><p id='target'>body</p>tail text</div>")
    target = doc.get_element_by_id("target")
    assert html_to_text(target) == "body"
    assert html_to_text(doc) == "body\n\ntail text"


def test_html_string_to_text_accepts_bare_and_multi_root_fragments() -> None:
    assert html_string_to_text("<p>one</p><p>two</p>") == "one\n\ntwo"
    assert html_string_to_text("plain text") == "plain text"
    assert html_string_to_text("   ") == ""


def test_inner_html_reserialises_children_only() -> None:
    node = html.fromstring("<div id='x'>lead <b>bold</b> tail</div>")
    assert inner_html(node) == "lead <b>bold</b> tail"

"""Byte-exact extraction of an element's inner markup from the source document.

lxml can tell us *which* element holds the description, but re-serialising it
produces markup that only resembles the source. For fidelity we would rather
hand back a literal slice of the decoded document, so
``description_html`` can honestly be labelled ``source-substring``.

The scanner below walks raw markup from a known start offset, tracking open and
closed tags of one name while skipping comments, CDATA, ``<script>``/``<style>``
bodies and quoted attribute values. If anything about the document defeats it,
the caller falls back to re-serialisation and labels the result accordingly. We
never claim byte identity we have not demonstrated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TAG_START = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9:-]*)")
_RAW_TEXT_TAGS = {"script", "style", "textarea", "title"}
VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }
)


@dataclass(slots=True)
class Slice:
    start: int
    end: int
    inner_start: int
    inner_end: int
    exact: bool
    reason: str | None = None


def _skip_comment(markup: str, i: int) -> int | None:
    if markup.startswith("<!--", i):
        end = markup.find("-->", i + 4)
        return len(markup) if end == -1 else end + 3
    if markup.startswith("<![CDATA[", i):
        end = markup.find("]]>", i + 9)
        return len(markup) if end == -1 else end + 3
    if markup.startswith("<!", i):
        end = markup.find(">", i + 2)
        return len(markup) if end == -1 else end + 1
    return None


def _end_of_tag(markup: str, i: int) -> tuple[int, bool]:
    """Return (index just past '>', self_closing)."""
    j = i
    quote = ""
    while j < len(markup):
        ch = markup[j]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            self_closing = markup[j - 1] == "/" if j > 0 else False
            return j + 1, self_closing
        j += 1
    return len(markup), False


def _skip_raw_text(markup: str, i: int, tag: str) -> int:
    closer = f"</{tag}"
    idx = markup.lower().find(closer, i)
    if idx == -1:
        return len(markup)
    end, _ = _end_of_tag(markup, idx)
    return end


def slice_element(markup: str, open_tag_start: int, tag_name: str) -> Slice:
    """Locate the full extent of the element beginning at ``open_tag_start``."""
    tag_name = tag_name.lower()
    inner_start, self_closing = _end_of_tag(markup, open_tag_start)
    if self_closing or tag_name in VOID_TAGS:
        return Slice(open_tag_start, inner_start, inner_start, inner_start, True)

    depth = 1
    i = inner_start
    n = len(markup)
    while i < n:
        lt = markup.find("<", i)
        if lt == -1:
            break
        skipped = _skip_comment(markup, lt)
        if skipped is not None:
            i = skipped
            continue
        match = _TAG_START.match(markup, lt)
        if not match:
            i = lt + 1
            continue
        closing = match.group(1) == "/"
        name = match.group(2).lower()
        end, self_closed = _end_of_tag(markup, lt)

        if not closing and name in _RAW_TEXT_TAGS and not self_closed:
            i = _skip_raw_text(markup, end, name)
            continue

        if name == tag_name:
            if closing:
                depth -= 1
                if depth == 0:
                    return Slice(open_tag_start, end, inner_start, lt, True)
            elif not self_closed and name not in VOID_TAGS:
                depth += 1
        i = end

    return Slice(
        open_tag_start,
        n,
        inner_start,
        n,
        False,
        reason=f"no balancing </{tag_name}> found before end of document",
    )


def find_by_id(markup: str, element_id: str, tag_name: str) -> Slice | None:
    """Find ``<tag ... id="element_id" ...>`` and return its extent."""
    pattern = re.compile(
        rf"""<{tag_name}\b[^>]*\bid\s*=\s*(["']?){re.escape(element_id)}\1[\s>]""",
        re.IGNORECASE,
    )
    match = pattern.search(markup)
    if not match:
        return None
    return slice_element(markup, match.start(), tag_name)


def inner_source(markup: str, sl: Slice) -> str:
    return markup[sl.inner_start : sl.inner_end]

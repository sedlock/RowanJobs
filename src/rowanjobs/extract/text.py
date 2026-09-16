"""The verbatim extraction contract: markup -> readable plain text.

This is the exact, versioned specification. Changing any rule here requires
bumping ``TEXT_CONTRACT_VERSION``, which produces new extraction rows rather
than rewriting old ones -- a parser change can never masquerade as a website
edit.

Rules (contract 1.0.0)
----------------------

Preserved exactly
    Wording, spelling, capitalisation, punctuation, paragraph order, list order
    and list content. Characters are never transliterated, "corrected" or
    normalised: curly quotes stay curly, ``U+00A0`` stays ``U+00A0``, and
    typographic dashes stay as published.

Dropped (non-rendered markup only)
    ``<script>``, ``<style>``, ``<noscript>``, ``<template>``, ``<svg>``,
    ``<head>`` and HTML comments. These are not shown to a reader. They are still
    present in the archived page and in ``description_html``.

Block boundaries
    ``p, div, h1..h6, li, tr, table, thead, tbody, blockquote, section,
    article, header, footer, aside, form, fieldset, hr, ul, ol, dl, dt, dd,
    pre, figure, figcaption, address`` each end the current line. Blocks are
    joined with a single blank line (``\\n\\n``).

``<br>``
    A single newline inside the current block.

Lists
    Each ``<li>`` becomes its own line. **No bullet or number is inserted** --
    adding one would change the text. Rowan's postings frequently carry their
    own ``•`` or ``·`` characters in the source; those are kept as published.

Tables
    Cells within a row are joined with a single TAB (``\\t``); each row is its
    own line.

Entities
    Decoded to the characters they denote (``&amp;`` -> ``&``, ``&#160;`` ->
    ``U+00A0``). The non-breaking space is **kept as U+00A0** and is not
    converted to an ordinary space.

Whitespace
    Inside a text node, runs of ASCII space, tab, CR and LF collapse to one
    space -- this is what HTML rendering does, and it removes only source
    indentation, never content. ``<pre>`` subtrees are exempt and keep their
    whitespace byte for byte. Leading and trailing whitespace is trimmed per
    block. Three or more consecutive newlines collapse to two.

Encoding
    See :mod:`rowanjobs.extract.decode`. Undecodable input is reported, never
    silently replaced.
"""

from __future__ import annotations

import re

from lxml import etree, html

TEXT_CONTRACT_RULES = "verbatim-1.0.0"

DROP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "head", "meta", "link", "iframe"}
)

BLOCK_TAGS = frozenset(
    {
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table",
        "thead", "tbody", "tfoot", "blockquote", "section", "article", "header",
        "footer", "aside", "form", "fieldset", "hr", "ul", "ol", "dl", "dt",
        "dd", "pre", "figure", "figcaption", "address", "main", "nav",
    }
)

CELL_TAGS = frozenset({"td", "th"})

_WS_RUN = re.compile(r"[ \t\r\n]+")
_TRAILING_WS = re.compile(r"[ \t]+\n")
_LEADING_WS = re.compile(r"\n[ \t]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")


def _collapse(value: str) -> str:
    return _WS_RUN.sub(" ", value)


class _Renderer:
    __slots__ = ("out", "_pre_depth")

    def __init__(self) -> None:
        self.out: list[str] = []
        self._pre_depth = 0

    def emit(self, value: str) -> None:
        if value:
            self.out.append(value)

    def newline(self, count: int = 1) -> None:
        # Never stack more separators than needed.
        existing = 0
        for chunk in reversed(self.out):
            stripped = chunk.strip(" \t")
            if stripped == "":
                if "\n" in chunk:
                    existing += chunk.count("\n")
                continue
            trailing = len(chunk) - len(chunk.rstrip("\n"))
            existing += trailing
            break
        if not self.out:
            return
        if existing < count:
            self.out.append("\n" * (count - existing))

    def walk(self, node: html.HtmlElement, *, include_tail: bool = True) -> None:
        tag = node.tag
        if not isinstance(tag, str):
            # Comments and processing instructions are not rendered text.
            if include_tail and node.tail:
                self.emit(self._text(node.tail))
            return
        tag = tag.lower()
        if tag in DROP_TAGS:
            if include_tail and node.tail:
                self.emit(self._text(node.tail))
            return

        is_pre = tag == "pre"
        if is_pre:
            self._pre_depth += 1

        if tag == "br":
            self.newline(1)
        elif tag in BLOCK_TAGS:
            self.newline(2)

        if node.text:
            self.emit(self._text(node.text))

        first_cell = True
        for child in node:
            child_tag = child.tag if isinstance(child.tag, str) else ""
            if child_tag.lower() in CELL_TAGS:
                if not first_cell:
                    self.emit("\t")
                first_cell = False
            self.walk(child)

        if tag in BLOCK_TAGS:
            self.newline(2)

        if is_pre:
            self._pre_depth -= 1

        if include_tail and node.tail:
            self.emit(self._text(node.tail))

    def _text(self, value: str) -> str:
        return value if self._pre_depth else _collapse(value)

    def result(self) -> str:
        text = "".join(self.out)
        text = _TRAILING_WS.sub("\n", text)
        text = _LEADING_WS.sub("\n", text)
        text = _MANY_NEWLINES.sub("\n\n", text)
        return text.strip("\n").strip(" \t")


def html_to_text(fragment: html.HtmlElement) -> str:
    """Render an lxml element to readable text under the verbatim contract.

    The element's own ``tail`` -- text that follows it in its parent -- belongs
    to the parent, not to this element, and is excluded.
    """
    renderer = _Renderer()
    renderer.walk(fragment, include_tail=False)
    return renderer.result()


def html_string_to_text(markup: str) -> str:
    """Convenience wrapper for a markup string."""
    if not markup.strip():
        return ""
    try:
        fragment = html.fragment_fromstring(markup, create_parent="div")
    except etree.ParserError:
        fragment = html.fromstring(f"<div>{markup}</div>")
    return html_to_text(fragment)


def inner_html(node: html.HtmlElement) -> str:
    """Re-serialise an element's children.

    The result is *reconstructed* markup. It is semantically equivalent but not
    byte-identical to the source, which is why callers label it
    ``description_html_kind='reserialized'``.
    """
    parts: list[str] = []
    if node.text:
        parts.append(node.text)
    for child in node:
        parts.append(etree.tostring(child, encoding="unicode", method="html"))
    return "".join(parts)

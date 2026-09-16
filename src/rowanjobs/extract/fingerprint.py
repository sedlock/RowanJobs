"""Content fingerprints and the comparison contract.

Four independent fingerprints are kept so a later question can ask "did the
prose change, or only a metadata field?" without re-reading every archived page:

``payload``      SHA-256 of the archived bytes (owned by the archive store)
``description_text``  SHA-256 of the readable text under the text contract
``description_html``  SHA-256 of the description markup
``metadata``     SHA-256 of the ordered, labelled source fields
``content``      SHA-256 over the three above plus the title

Every fingerprint is namespaced with its contract version. Two versions are only
ever compared when their contract versions match, which is what stops a parser
upgrade from being recorded as a website edit.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

import rowanjobs


def _digest(*parts: str) -> str:
    h = sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def text_fingerprint(text: str | None, contract: str | None = None) -> str:
    return _digest("text", contract or rowanjobs.TEXT_CONTRACT_VERSION, text or "")


def html_fingerprint(markup: str | None, contract: str | None = None) -> str:
    return _digest("html", contract or rowanjobs.CONTRACT_VERSION, markup or "")


def metadata_fingerprint(values: list[dict[str, Any]], contract: str | None = None) -> str:
    """Fingerprint the labelled source fields.

    Order matters: the source presents Job no / Work type / Location /
    Categories in a deliberate order, and a reordering is a real change. Only
    the verbatim parts take part -- derived normalisations do not, so improving
    normalisation never looks like an edit.
    """
    canonical = [
        {
            "field_key": v.get("field_key"),
            "source_label": v.get("source_label"),
            "ordinal": v.get("ordinal", 0),
            "value_text": v.get("value_text"),
            "field_state": v.get("field_state"),
            "origin": v.get("origin"),
        }
        for v in values
    ]
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _digest("metadata", contract or rowanjobs.CONTRACT_VERSION, blob)


def comparison_lineage(
    parser: str | None = None,
    contract: str | None = None,
    text_contract: str | None = None,
) -> str:
    """The identity of the interpretation, as opposed to the content.

    Two extracted versions are only comparable when this matches. Folding it
    into the content fingerprint is what stops a parser or contract upgrade from
    ever being recorded as a website edit: the upgraded reading starts a
    parallel line of versions instead of appearing to change the old one.
    """
    return (
        f"{parser or rowanjobs.PARSER_VERSION}"
        f"|{contract or rowanjobs.CONTRACT_VERSION}"
        f"|{text_contract or rowanjobs.TEXT_CONTRACT_VERSION}"
    )


def content_fingerprint(
    *,
    title: str | None,
    description_text_fp: str,
    description_html_fp: str,
    metadata_fp: str,
    contract: str | None = None,
    parser: str | None = None,
    text_contract: str | None = None,
) -> str:
    return _digest(
        "content",
        comparison_lineage(parser, contract, text_contract),
        title or "",
        description_text_fp,
        description_html_fp,
        metadata_fp,
    )

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

from .. import CONTRACT_VERSION, TEXT_CONTRACT_VERSION


def _digest(*parts: str) -> str:
    h = sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def text_fingerprint(text: str | None, contract: str = TEXT_CONTRACT_VERSION) -> str:
    return _digest("text", contract, text or "")


def html_fingerprint(markup: str | None, contract: str = CONTRACT_VERSION) -> str:
    return _digest("html", contract, markup or "")


def metadata_fingerprint(values: list[dict[str, Any]], contract: str = CONTRACT_VERSION) -> str:
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
    return _digest("metadata", contract, blob)


def content_fingerprint(
    *,
    title: str | None,
    description_text_fp: str,
    description_html_fp: str,
    metadata_fp: str,
    contract: str = CONTRACT_VERSION,
) -> str:
    return _digest(
        "content", contract, title or "", description_text_fp, description_html_fp, metadata_fp
    )

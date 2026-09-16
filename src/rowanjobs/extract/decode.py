"""Byte -> text decoding, with the outcome recorded rather than hidden.

Order of preference:

1. the charset declared in the HTTP ``Content-Type`` header;
2. a ``<meta charset>`` / ``<meta http-equiv="content-type">`` inside the first
   4 KiB of the document;
3. UTF-8;
4. Windows-1252, which is what mis-labelled "ISO-8859-1" web pages usually are.

If every strict attempt fails we decode with ``errors="replace"`` and report the
replacement count. We never substitute characters silently: ``decode_error_count``
ends up on the extraction row and in its warnings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:+-]+)""", re.IGNORECASE
)
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

REPLACEMENT = "�"


@dataclass(slots=True)
class DecodeResult:
    text: str
    encoding: str
    strategy: str
    error_count: int
    candidates: tuple[str, ...]

    @property
    def lossless(self) -> bool:
        return self.error_count == 0


def sniff_meta_charset(data: bytes) -> str | None:
    match = _META_CHARSET.search(data[:4096])
    if not match:
        return None
    try:
        return match.group(1).decode("ascii").strip().lower()
    except UnicodeDecodeError:  # pragma: no cover - defensive
        return None


def decode_html(data: bytes, declared_charset: str | None = None) -> DecodeResult:
    candidates: list[str] = []

    for bom, enc in _BOMS:
        if data.startswith(bom):
            candidates.append(enc)
            break

    for value in (declared_charset, sniff_meta_charset(data)):
        if value:
            norm = value.strip().lower()
            if norm and norm not in candidates:
                candidates.append(norm)

    for fallback in ("utf-8", "cp1252"):
        if fallback not in candidates:
            candidates.append(fallback)

    for enc in candidates:
        try:
            return DecodeResult(
                text=data.decode(enc),
                encoding=enc,
                strategy="strict",
                error_count=0,
                candidates=tuple(candidates),
            )
        except (UnicodeDecodeError, LookupError):
            continue

    text = data.decode("utf-8", errors="replace")
    return DecodeResult(
        text=text,
        encoding="utf-8",
        strategy="replace",
        error_count=text.count(REPLACEMENT),
        candidates=tuple(candidates),
    )

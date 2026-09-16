# The verbatim extraction contract

Version: `TEXT_CONTRACT_VERSION = "1.0.0"`, internal rules tag
`TEXT_CONTRACT_RULES = "verbatim-1.0.0"`
(`src/rowanjobs/__init__.py`, `src/rowanjobs/extract/text.py`).

This document is the specification. The implementation is
`src/rowanjobs/extract/text.py`; supporting pieces are
`src/rowanjobs/extract/decode.py` (bytes → text),
`src/rowanjobs/extract/slicing.py` (byte-exact markup slices) and
`src/rowanjobs/extract/fingerprint.py` (the comparison contract).

**Changing any rule below requires bumping `TEXT_CONTRACT_VERSION.`** Doing so
produces new `extractions` rows rather than rewriting old ones, so a parser
change can never masquerade as a website edit.

---

## 1. What is preserved exactly

Wording, spelling, capitalisation, punctuation, paragraph order, list order and
list content.

Characters are never transliterated, "corrected" or normalised:

- curly quotes stay curly (`U+2018`, `U+2019`, `U+201C`, `U+201D`);
- typographic dashes stay as published (`U+2013`, `U+2014`);
- `U+00A0` (non-breaking space) stays `U+00A0`;
- bullet characters the source itself printed (`•` `U+2022`, `·` `U+00B7`) stay
  exactly where they are.

Nothing is spell-checked, capitalisation-normalised, re-cased, translated,
summarised or reworded. No language model touches an archived description at any
point (`CLAUDE.md`, "Never" rule 3).

## 2. What is dropped, and why

Only markup that a reader is not shown:

`<script>`, `<style>`, `<noscript>`, `<template>`, `<svg>`, `<head>`, `<meta>`,
`<link>`, `<iframe>`, and HTML comments
(`DROP_TAGS` plus the comment/processing-instruction branch in
`_Renderer.walk`).

These contribute no rendered text, so including them would add content the
source never displayed. They are **not lost**: the full page is in `artifacts`
byte-for-byte, and `description_html` retains the body markup. Only the *plain
text rendering* omits them.

An HTML comment's **tail text** — the text that follows the comment in its
parent — is still emitted, because that text *is* displayed.

This matters on the Rowan listing page, where the duplicated `recent-jobs`
section has its summary rows commented out; they are correctly not rendered.

## 3. Block boundaries

Each of these tags ends the current line, and blocks are joined with a single
blank line (`\n\n`):

```
p  div  h1 h2 h3 h4 h5 h6  li  tr  table  thead  tbody  tfoot
blockquote  section  article  header  footer  aside  form  fieldset
hr  ul  ol  dl  dt  dd  pre  figure  figcaption  address  main  nav
```

(`BLOCK_TAGS`.) The renderer never stacks more separators than needed: before
emitting a break it counts the newlines already present at the tail of the
output and only tops up the difference (`_Renderer.newline`). Leading output is
never prefixed with a blank line.

## 4. `<br>`

A single newline (`\n`) inside the current block. Not a paragraph break, not a
space.

## 5. Lists

Each `<li>` becomes its own line.

**No bullet, dash, number or other marker is inserted.** Adding one would change
the text: it would put characters in the archive that the employer did not
write. Rowan's postings frequently carry their own `•` or `·` characters inside
the list item content; those are part of the source text and are kept as
published.

The consequence is that a rendered list looks like a sequence of lines. That is
correct: the marker is presentation, supplied by the browser's stylesheet, not
content.

## 6. Tables

- Cells (`<td>`, `<th>`, `CELL_TAGS`) within a row are joined with a single TAB
  (`\t`).
- Each `<tr>` is its own line (it is in `BLOCK_TAGS`).

The TAB separator is chosen because it is unambiguous, round-trips into
spreadsheets, and is not a character Rowan's advertisement prose uses for
layout.

## 7. Entities

HTML entities are decoded to the characters they denote: `&amp;` → `&`,
`&#160;` → `U+00A0`, `&mdash;` → `—`. (Decoding happens in the HTML parser, ahead
of the renderer.)

**The non-breaking space is kept as `U+00A0` and is never converted to an
ordinary space.** It is a different character with different rendering and
different search behaviour; silently folding it would be a content change
performed by the archive rather than by the source.

## 8. Whitespace collapse

Inside a text node, runs of ASCII space, tab, CR and LF collapse to a single
space (`_WS_RUN = [ \t\r\n]+` → `" "`).

This is a **formatting-only transformation that matches browser rendering**: it
is exactly what an HTML renderer does with source indentation. It removes only
the whitespace the page author used to lay out the markup, never content. Note
what it deliberately does *not* touch: `U+00A0` is not in the character class, so
non-breaking spaces survive collapse untouched.

Post-processing (`_Renderer.result`):

- trailing space/tab before a newline is removed (`[ \t]+\n` → `\n`);
- leading space/tab after a newline is removed (`\n[ \t]+` → `\n`);
- three or more consecutive newlines collapse to two (`\n{3,}` → `\n\n`);
- the whole result is stripped of leading/trailing newlines, spaces and tabs.

So blocks are trimmed and paragraph separation is at most one blank line.

### `<pre>` exemption

Subtrees inside `<pre>` are **exempt from whitespace collapse** and keep their
whitespace byte for byte. Preformatted text is content, not layout.

The exemption is complete, including against the post-processing rules above.
Those regexes operate on the whole assembled document and would otherwise reach
inside a `<pre>` and strip indentation that is content. So while `_pre_depth` is
non-zero, `_Renderer._text` sets the run aside in `_preserved` and emits a
`\x00<index>\x00` placeholder instead; `result()` runs the whitespace tidying
over the placeholder-bearing text and only then substitutes the original runs
back (`_PRESERVED`). Nothing inside a `<pre>` is ever collapsed, trimmed or
newline-folded.

## 9. Element scope and tails

`html_to_text(fragment)` renders an element **excluding its own `tail`** — the
text that follows the element in its parent belongs to the parent, not to the
element. `html_string_to_text(markup)` is the convenience wrapper for a markup
string; it wraps the fragment in a synthetic `<div>` parent.

---

## Character encoding

`src/rowanjobs/extract/decode.py`. Candidate encodings are tried in this order:

1. A byte-order mark, if present (`utf-8-sig`, `utf-16-le/be`, `utf-32-le/be`).
2. The charset declared in the HTTP `Content-Type` header.
3. A `<meta charset>` / `<meta http-equiv="content-type">` inside the **first
   4 KiB** of the document.
4. `utf-8`.
5. `cp1252` — what mis-labelled "ISO-8859-1" web pages usually are in practice.

The first candidate that decodes **strictly** wins; `DecodeResult.strategy` is
then `"strict"` and `error_count` is 0.

### Decoding failure handling

If every strict attempt fails, the bytes are decoded as UTF-8 with
`errors="replace"`, `strategy` becomes `"replace"`, and `error_count` is the
count of `U+FFFD` replacement characters produced.

**Nothing is substituted silently.** The encoding, the strategy and the error
count are written onto the `extractions` row (`decode_encoding`,
`decode_strategy`, `decode_error_count`) and surface in the extraction warnings,
so a description containing replacement characters is visibly flagged rather
than quietly wrong. `DecodeResult.lossless` is simply `error_count == 0`.

Resource text extraction uses the same machinery and marks the resource's
`text_extraction_state` as `failed` when decoding was not lossless
(`src/rowanjobs/collect/details.py::_resource_text`); the bytes are preserved
regardless.

---

## `description_html`: `source-substring` vs `reserialized`

`posting_versions.description_html_kind` is one of `source-substring`,
`reserialized` or `absent`, and it is an honesty label about the markup stored
beside it.

### `source-substring` — the preferred case

`src/rowanjobs/extract/slicing.py` walks the raw decoded document from the start
offset of `<div id="job-details" ...>` and finds the balancing `</div>` by
tracking open and closed tags of that one name, while:

- skipping HTML comments (`<!-- -->`), CDATA sections and other `<!…>`
  declarations;
- skipping the bodies of raw-text elements (`script`, `style`, `textarea`,
  `title`), so a `</div>` inside a script string cannot end the element early;
- respecting quoted attribute values, so a `>` inside an attribute does not end
  a tag early;
- treating void elements and self-closing tags as non-nesting (`VOID_TAGS`).

When the balancing tag is found, `Slice.exact` is true and
`description_html` is `markup[inner_start:inner_end]` — a **literal, byte-for-byte
slice of the decoded source document**. That is what `source-substring` claims,
and nothing else claims it.

### `reserialized` — the honest fallback

If the scanner cannot resolve the element's boundaries (no balancing tag before
the end of the document, or the element could not be located), the parser falls
back to `inner_html(node)`, which re-serialises the element's children with
lxml. The result is semantically equivalent but **not byte-identical**: attribute
order, quoting and empty-element syntax may differ.

That case is labelled `reserialized` and a warning is recorded on the extraction
stating that the markup was re-serialised and why
(`pageup_detail.py::_description`). The archive never claims byte identity it
has not demonstrated.

### `absent`

No `<div id="job-details">` was found. `description_html` is null, the
extraction status is `partial`, and a warning records that the description was
not isolated.

Note that `description_html_kind` is a property of *how the markup was obtained*,
not of the content. A version whose kind changed from `source-substring` to
`reserialized` between observations indicates a parsing difficulty, not a source
edit — though because `description_html` itself would then differ,
`description_html_fingerprint` would change too. This is one of the reasons the
fingerprints are kept separate: `description_text_fingerprint` would be
unaffected, making the cause visible.

---

## The four fingerprints

`src/rowanjobs/extract/fingerprint.py`. All are SHA-256 over
unit-separator-delimited (`\x1f`) UTF-8 parts, and every one of them is
**namespaced with its contract version**.

| Fingerprint | Column | Inputs |
|---|---|---|
| `description_text` | `posting_versions.description_text_fingerprint` | `"text"`, `TEXT_CONTRACT_VERSION`, the rendered text |
| `description_html` | `posting_versions.description_html_fingerprint` | `"html"`, `CONTRACT_VERSION`, the description markup |
| `metadata` | `posting_versions.metadata_fingerprint` | `"metadata"`, `CONTRACT_VERSION`, the canonical JSON of the ordered labelled fields |
| `content` | `posting_versions.content_fingerprint` | `"content"`, `CONTRACT_VERSION`, the title, then the three fingerprints above |

A fifth hash exists but belongs to the archive rather than to the comparison
contract: `artifacts.sha256`, the **payload** fingerprint, computed over the
uncompressed archived bytes by `src/rowanjobs/archive/store.py`.

Keeping them separate answers questions that a single hash cannot: *did the prose
change, or only a metadata field?* — without re-reading every archived page.
`rowanjobs diff` reports exactly that breakdown (`title`, `description_text`,
`description_html`, `metadata`).

### The metadata fingerprint's canonical form

Only the verbatim parts take part:

```json
{"field_key": …, "source_label": …, "ordinal": …,
 "value_text": …, "field_state": …, "origin": …}
```

**Order matters.** The source presents Job no / Work type / Location /
Categories in a deliberate order, and a reordering is a real change, so the list
is serialised in source order (the JSON object keys are sorted for stability,
the list is not re-sorted).

**Derived normalisations are excluded** (`normalized_json`, parsed dates). That
is deliberate: improving the normaliser must never look like a source edit.

## How the comparison contract stops a parser upgrade looking like a source edit

Four mechanisms, all of which have to hold:

1. **Extractions are keyed by the contract.** `extractions` is unique on
   `(artifact_id, parser_name, parser_version, contract_version,
   text_contract_version)`. A parser upgrade produces a *new row* against the
   same artifact; the old interpretation is never rewritten
   (`m0001_initial.py`).

2. **Fingerprints are namespaced with the contract version.** The version string
   is hashed into the digest itself, so the same text under two text contracts
   produces two different `description_text_fingerprint` values — they can never
   be accidentally equal, and they can never be accidentally compared.

3. **Versions are only compared within one `contract_version`.**
   `posting_versions` is unique on `(posting_id, contract_version,
   content_fingerprint)`, and the change detector explicitly filters the previous
   version by `v.contract_version = ?` with the *current* `CONTRACT_VERSION`
   (`src/rowanjobs/collect/events.py::_record_content_events`). A version from a
   different contract is invisible to the comparison, so it cannot produce a
   spurious `content_changed` event.

4. **Reprocessing keeps the original observation time.**
   `src/rowanjobs/reprocess.py` re-parses archived payloads only, makes no
   network request, and passes the **original** `observed_at_utc` when ensuring a
   version — it did not observe anything, it reinterpreted stored evidence. The
   report says so explicitly.

The practical result: bumping `TEXT_CONTRACT_VERSION` or `CONTRACT_VERSION` and
running `rowanjobs reprocess` starts a parallel line of extractions and versions
alongside the old one. The old line remains queryable and still means what it
meant. `rowanjobs diff` compares within the highest contract version present for
that posting and prints the note that a parser upgrade cannot appear there as a
source edit.

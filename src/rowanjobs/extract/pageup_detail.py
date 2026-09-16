"""Detail-page adapter for the PageUp career site at jobs.rowan.edu.

Verified against live captures on 2026-09-16 (docs/SOURCE_ADAPTER_AUDIT.md).
Structure inside ``<div id="job"><div id="job-content">``:

    <h2>TITLE</h2>
    <p>
      <b>Job no:</b>    <span class="job-externalJobNo">501826</span><br>
      <b>Work type:</b> <span class="work-type temporary-part-time">Temporary Part-Time</span><br>
      <b>Location:</b>  <span class="location">Glassboro, New Jersey</span><br>
      <b>Categories:</b><span class="categories">Public Safety/Security</span><br>
    </p>
    <div id="job-details"> ... advertisement body ... </div>
    <p>
      <b>Advertised:</b>        <span class="open-date"><time datetime="...">Sep 15 2026 </time></span> Eastern Daylight Time<br>
      <b>Applications close:</b><span class="close-date"><time datetime="...">Sep 29 2026 11:55 PM </time></span> Eastern Daylight Time
    </p>

Labels are read generically from the ``<b>`` elements, so a label this adapter
has never seen is still captured -- flagged ``known_label = 0`` rather than
dropped.

Multivalued fields (Location, Categories, Work type) keep the source's own
separator. Locations are **not** split on commas: "Glassboro, New Jersey" is one
place, and PageUp separates genuinely distinct values with ";" or with repeated
spans.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urljoin, urlparse

from lxml import html

from .. import PARSER_VERSION
from .dates import ParsedDate, parse_source_date
from .slicing import find_by_id, inner_source
from .text import html_to_text, inner_html

PARSER_NAME = "pageup_detail"

# Labels this adapter understands. Anything else is still stored, with
# known_label = 0, so a new source field is visible instead of lost.
KNOWN_LABELS: dict[str, str] = {
    "job no": "job_no",
    "job no.": "job_no",
    "work type": "work_type",
    "location": "location",
    "locations": "location",
    "categories": "categories",
    "category": "categories",
    "department": "department",
    "division": "division",
    "unit": "unit",
    "business unit": "business_unit",
    "appointment type": "appointment_type",
    "employment type": "employment_type",
    "salary": "salary",
    "classification": "classification",
    "faculty/staff": "faculty_staff",
    "advertised": "advertised",
    "applications close": "applications_close",
    "closes": "applications_close",
    "reference number": "reference_number",
}

DATE_FIELD_KEYS = {"advertised", "applications_close"}

# Values that PageUp separates with ";" rather than "," -- see module docstring.
MULTIVALUE_SEPARATOR = re.compile(r"\s*;\s*")

_JOB_PATH = re.compile(r"^/(?P<locale>[a-z]{2}-[a-z]{2})/job/(?P<job_id>\d+)")


@dataclass(slots=True)
class FieldValue:
    field_key: str
    source_label: str | None
    ordinal: int
    value_text: str | None
    value_html: str | None
    field_state: str
    origin: str
    known_label: bool
    normalized: dict[str, Any] | None = None
    date: ParsedDate | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "field_key": self.field_key,
            "source_label": self.source_label,
            "ordinal": self.ordinal,
            "value_text": self.value_text,
            "value_html": self.value_html,
            "field_state": self.field_state,
            "origin": self.origin,
            "known_label": self.known_label,
            "normalized": self.normalized,
        }
        if self.date is not None:
            out["date"] = self.date.as_dict()
        return out


@dataclass(slots=True)
class DetailExtraction:
    status: str
    structure_recognized: bool
    title: str | None
    external_job_id: str | None
    description_html: str | None
    description_html_kind: str
    description_text: str | None
    values: list[FieldValue] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    closure_signal: str | None = None
    warnings: list[str] = field(default_factory=list)
    failure_detail: str | None = None

    def value_dicts(self) -> list[dict[str, Any]]:
        return [v.as_dict() for v in self.values]

    def as_dict(self) -> dict[str, Any]:
        return {
            "parser": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "status": self.status,
            "structure_recognized": self.structure_recognized,
            "title": self.title,
            "external_job_id": self.external_job_id,
            "description_html_kind": self.description_html_kind,
            "description_present": self.description_html is not None,
            "closure_signal": self.closure_signal,
            "values": self.value_dicts(),
            "links": self.links,
            "warnings": self.warnings,
            "failure_detail": self.failure_detail,
        }


def _failed(detail: str) -> DetailExtraction:
    return DetailExtraction(
        status="failed",
        structure_recognized=False,
        title=None,
        external_job_id=None,
        description_html=None,
        description_html_kind="absent",
        description_text=None,
        failure_detail=detail,
    )


def parse_detail(markup: str, base_url: str) -> DetailExtraction:
    warnings: list[str] = []
    try:
        doc = cast("html.HtmlElement", html.document_fromstring(markup))
    except Exception as exc:  # noqa: BLE001
        return _failed(f"HTML parse failed: {exc}")

    job_content = doc.get_element_by_id("job-content", None)
    job_wrapper = doc.get_element_by_id("job", None)
    if job_content is None:
        return _failed("no <div id='job-content'>: not a recognised detail page")

    hidden = (
        "display:none" in (job_wrapper.get("style") or "").replace(" ", "").lower()
        if (job_wrapper is not None)
        else False
    )
    body_text = html_to_text(job_content).strip()
    if hidden or not body_text:
        return DetailExtraction(
            status="ok",
            structure_recognized=True,
            title=None,
            external_job_id=None,
            description_html=None,
            description_html_kind="absent",
            description_text=None,
            closure_signal="empty-job-content",
            warnings=["job container present but carried no advertisement content"],
        )

    heading = next(iter(job_content.iter("h1", "h2")), None)
    title = html_to_text(heading).strip() if heading is not None else None
    if not title:
        warnings.append("no heading found inside job-content")

    description_html, kind, description_text, desc_node = _description(markup, doc, warnings)
    values = _labelled_values(job_content, warnings, desc_node)
    external_job_id = _external_job_id(job_content, values, warnings)
    links = _links(desc_node, job_content, base_url)
    closure = _closure_signal(doc, body_text)

    status = "ok" if description_html is not None else "partial"
    return DetailExtraction(
        status=status,
        structure_recognized=True,
        title=title,
        external_job_id=external_job_id,
        description_html=description_html,
        description_html_kind=kind,
        description_text=description_text,
        values=values,
        links=links,
        closure_signal=closure,
        warnings=warnings,
    )


# --------------------------------------------------------------------- fields


def _labelled_values(
    job_content: html.HtmlElement,
    warnings: list[str],
    description_node: html.HtmlElement | None = None,
) -> list[FieldValue]:
    """Read every ``<b>Label:</b> value`` pair in the metadata region.

    ``#job-details`` is excluded: Rowan's advertisement bodies use bold run-in
    headings ("Summary:", "Major Duties:") that are prose, not source metadata.
    Treating them as fields would invent empty metadata and, worse, make an
    edit to the prose look like a metadata change.
    """
    values: list[FieldValue] = []
    seen: dict[str, int] = {}

    for bold in job_content.iter("b", "strong"):
        if description_node is not None and _is_within(bold, description_node):
            continue
        raw_label = (bold.text_content() or "").strip()
        if not raw_label or not raw_label.endswith(":"):
            continue
        label = raw_label.rstrip(":").strip()
        key = KNOWN_LABELS.get(label.lower())
        known = key is not None
        if key is None:
            key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "unlabelled"
            warnings.append(f"unknown source label preserved: {raw_label!r}")

        value_nodes, value_text, value_html = _value_after(bold)
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1

        if value_text is None:
            state = "absent"
        elif value_text.strip() == "":
            state = "blank"
        else:
            state = "present"

        date: ParsedDate | None = None
        normalized: dict[str, Any] | None = None
        if key in DATE_FIELD_KEYS or _looks_like_date_block(value_nodes):
            machine, displayed, tz_text = _time_parts(value_nodes, value_text)
            # The date shown is the <time> element's text; anything after it
            # ("Eastern Daylight Time") is the source's timezone wording.
            date = parse_source_date(displayed or value_text, machine, tz_text)
        elif state == "present" and value_text is not None:
            parts = [p for p in MULTIVALUE_SEPARATOR.split(value_text) if p.strip()]
            normalized = {
                "values": parts,
                "separator": ";" if len(parts) > 1 else None,
                "note": "locations are never split on commas; see adapter docs",
            }

        values.append(
            FieldValue(
                field_key=key,
                source_label=raw_label,
                ordinal=ordinal,
                value_text=value_text,
                value_html=value_html,
                field_state=state,
                origin="detail_labelled",
                known_label=known,
                normalized=normalized,
                date=date,
            )
        )
    return values


def _is_within(node: html.HtmlElement, ancestor: html.HtmlElement) -> bool:
    """True when ``node`` sits inside ``ancestor``.

    Identity, not ``id()``: lxml element proxies are created on demand and a
    freed proxy's ``id()`` can be handed to an unrelated element later. Holding
    ``ancestor`` keeps its proxy alive, so ``is`` is reliable.
    """
    if node is ancestor:
        return True
    return any(parent is ancestor for parent in node.iterancestors())


def _value_after(
    bold: html.HtmlElement,
) -> tuple[list[html.HtmlElement], str | None, str | None]:
    """Collect the nodes between this ``<b>`` and the next line break or label."""
    chunks: list[str] = []
    html_chunks: list[str] = []
    nodes: list[html.HtmlElement] = []

    if bold.tail:
        chunks.append(bold.tail)
        html_chunks.append(bold.tail)

    node: html.HtmlElement | None = bold.getnext()
    while node is not None:
        tag = node.tag if isinstance(node.tag, str) else ""
        tag = tag.lower()
        if tag == "br":
            break
        if tag in ("b", "strong"):
            text = (node.text_content() or "").strip()
            if text.endswith(":"):
                break
        nodes.append(node)
        chunks.append(html_to_text(node))
        from lxml import etree

        html_chunks.append(etree.tostring(node, encoding="unicode", method="html"))
        if node.tail:
            chunks.append(node.tail)
            html_chunks.append(node.tail)
        node = node.getnext()

    text = " ".join(part for part in " ".join(chunks).split())
    markup = "".join(html_chunks).strip()
    if not chunks:
        return nodes, None, None
    return nodes, text, markup or None


def _looks_like_date_block(nodes: list[html.HtmlElement]) -> bool:
    return any(node.tag == "time" or list(node.iter("time")) for node in nodes)


def _time_parts(
    nodes: list[html.HtmlElement], value_text: str | None
) -> tuple[str | None, str | None, str | None]:
    """Split a date cell into (machine value, displayed date, timezone wording)."""
    machine: str | None = None
    display: str | None = None
    for node in nodes:
        time_els = [node] if node.tag == "time" else list(node.iter("time"))
        for time_el in time_els:
            machine = time_el.get("datetime")
            display = " ".join((time_el.text_content() or "").split()) or None
            break
        if display is not None:
            break
    tz_text = None
    if value_text and display and display in value_text:
        tz_text = value_text.split(display, 1)[1].strip() or None
    return machine, display, tz_text


def _external_job_id(
    job_content: html.HtmlElement, values: list[FieldValue], warnings: list[str]
) -> str | None:
    nodes = job_content.find_class("job-externalJobNo")
    if nodes:
        text = (nodes[0].text_content() or "").strip()
        if text:
            return text
    for value in values:
        if value.field_key == "job_no" and value.value_text:
            return value.value_text.strip()
    warnings.append("no job number displayed on the detail page")
    return None


# ---------------------------------------------------------------- description


def _description(
    markup: str, doc: html.HtmlElement, warnings: list[str]
) -> tuple[str | None, str, str | None, html.HtmlElement | None]:
    node = doc.get_element_by_id("job-details", None)
    if node is None:
        warnings.append("no <div id='job-details'>: description not isolated")
        return None, "absent", None, None

    sl = find_by_id(markup, "job-details", "div")
    if sl is not None and sl.exact:
        description_html = inner_source(markup, sl)
        kind = "source-substring"
    else:
        description_html = inner_html(node)
        kind = "reserialized"
        warnings.append(
            "description markup was re-serialised, not sliced from the source; "
            + (sl.reason if sl and sl.reason else "element boundaries not resolvable")
        )
    return description_html, kind, html_to_text(node), node


# ---------------------------------------------------------------------- links


def _links(
    desc_node: html.HtmlElement | None, job_content: html.HtmlElement, base_url: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    position = 0
    scopes = [("posting_description", desc_node), ("job_content", job_content)]
    seen: set[tuple[str, str]] = set()
    for parent_kind, scope in scopes:
        if scope is None:
            continue
        for anchor in scope.iter("a"):
            href = anchor.get("href")
            if not href:
                continue
            resolved = urljoin(base_url, href)
            key = (parent_kind, href)
            if key in seen:
                continue
            seen.add(key)
            position += 1
            classification, decision, reason = classify_link(resolved, anchor)
            out.append(
                {
                    "parent_kind": parent_kind,
                    "url_raw": href,
                    "url_resolved": resolved,
                    "link_text": (anchor.text_content() or "").strip()[:300],
                    "rel": anchor.get("rel"),
                    "position": position,
                    "classification": classification,
                    "collection_decision": decision,
                    "exclusion_reason": reason,
                }
            )
    return out


DOCUMENT_SUFFIXES = (
    ".pdf",
    ".doc",
    ".docx",
    ".rtf",
    ".odt",
    ".xls",
    ".xlsx",
    ".csv",
    ".txt",
    ".ppt",
    ".pptx",
)


def classify_link(url: str, anchor: html.HtmlElement) -> tuple[str, str, str | None]:
    """Decide what a link is and whether v1 retrieves it."""
    classes = (anchor.get("class") or "").split()
    lowered = url.lower()

    if lowered.startswith("mailto:"):
        return "mailto", "exclude", "mail link"
    if lowered.startswith("#") or lowered.startswith("javascript:"):
        return "anchor", "exclude", "in-page or script link"
    if "apply-link" in classes or "employee-referral-link" in classes:
        return "apply_workflow", "exclude", "application submission workflow is never followed"

    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path.lower()

    if "/apply/" in lowered or "pageuppeople.com" in host:
        return "apply_workflow", "exclude", "application submission workflow is never followed"

    if path.endswith(DOCUMENT_SUFFIXES):
        if host.endswith("jobs.rowan.edu"):
            return "job_document", "fetch", None
        return (
            "job_document",
            "exclude",
            f"document is hosted off the career site ({host}); outside the v1 fetch scope",
        )

    if host.endswith("jobs.rowan.edu"):
        return "internal_nav", "exclude", "career-site navigation, not job-specific content"
    return "external", "exclude", "general university or third-party site; not crawled"


# -------------------------------------------------------------------- closure


CLOSURE_PHRASES = (
    "no longer available",
    "this job is no longer",
    "position is no longer",
    "applications are closed",
    "job not found",
    "the job you are looking for",
    "vacancy is closed",
)


def _closure_signal(doc: html.HtmlElement, body_text: str) -> str | None:
    messages = doc.get_element_by_id("message-list", None)
    if messages is not None:
        text = html_to_text(messages).strip()
        if text:
            return f"message-list: {text[:400]}"
    lowered = body_text.lower()
    for phrase in CLOSURE_PHRASES:
        if phrase in lowered:
            return f"closure phrase: {phrase!r}"
    return None


def job_id_from_url(url: str) -> str | None:
    match = _JOB_PATH.match(urlparse(url).path)
    return match.group("job_id") if match else None

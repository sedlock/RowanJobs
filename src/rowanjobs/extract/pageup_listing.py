"""Listing-page adapter for the PageUp career site at jobs.rowan.edu.

Verified against live captures on 2026-09-16 (see
docs/SOURCE_ADAPTER_AUDIT.md). Structure:

* ``<tbody id="search-results-content">`` -- the authoritative result rows.
* ``<tbody id="recent-jobs-content">`` -- a *repeat* of the same rows under the
  "Current Opportunities" heading, with the summary rows commented out. Counting
  ``a.job-link`` across the page would double every advertisement, so entries are
  recorded per section and logical advertisements are deduplicated by source id.
* Each advertisement occupies two rows: a data row (link, ``span.location``,
  ``span.close-date``) followed by an optional ``tr.summary``.
* Pagination: ``<a class="more-link" href="?page=N&page-items=20">More Jobs
  <span class="count">113</span></a>``. The count is the number of advertisements
  *remaining after this page*, not the total. Its absence marks the final page.
* Past the final page the site still answers ``200`` with the full table markup
  and zero rows -- a validated empty result, quite different from an error page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from urllib.parse import urljoin, urlparse

from lxml import html

from .. import PARSER_VERSION
from .text import html_to_text

PARSER_NAME = "pageup_listing"

SECTION_IDS = {
    "search-results-content": "search-results",
    "recent-jobs-content": "recent-jobs",
}

JOB_HREF = re.compile(r"^/(?P<locale>[a-z]{2}-[a-z]{2})/job/(?P<job_id>\d+)(?:/(?P<slug>.*))?$")


@dataclass(slots=True)
class ListingEntry:
    section: str
    position_in_section: int
    href_raw: str | None
    href_resolved: str | None
    external_job_id: str | None
    resolution_state: str
    resolution_detail: str | None
    title_text: str | None
    summary_text: str | None
    summary_html: str | None
    displayed_metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "position_in_section": self.position_in_section,
            "href_raw": self.href_raw,
            "href_resolved": self.href_resolved,
            "external_job_id": self.external_job_id,
            "resolution_state": self.resolution_state,
            "resolution_detail": self.resolution_detail,
            "title_text": self.title_text,
            "summary_text": self.summary_text,
            "displayed_metadata": self.displayed_metadata,
        }


@dataclass(slots=True)
class ListingExtraction:
    status: str
    structure_recognized: bool
    empty_result_validated: bool
    entries: list[ListingEntry]
    sections_found: list[str]
    more_link_url: str | None
    more_link_remaining: int | None
    page_signature: str
    other_links: list[dict[str, Any]]
    warnings: list[str]
    failure_detail: str | None = None

    @property
    def authoritative_entries(self) -> list[ListingEntry]:
        return [e for e in self.entries if e.section == "search-results"]

    def unique_job_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for entry in self.authoritative_entries:
            if entry.external_job_id:
                seen.setdefault(entry.external_job_id, None)
        return list(seen)

    def unresolved_count(self) -> int:
        return sum(1 for e in self.entries if e.resolution_state != "resolved")

    def as_dict(self) -> dict[str, Any]:
        return {
            "parser": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "status": self.status,
            "structure_recognized": self.structure_recognized,
            "empty_result_validated": self.empty_result_validated,
            "sections_found": self.sections_found,
            "more_link_url": self.more_link_url,
            "more_link_remaining": self.more_link_remaining,
            "page_signature": self.page_signature,
            "entries": [e.as_dict() for e in self.entries],
            "other_links": self.other_links,
            "warnings": self.warnings,
            "failure_detail": self.failure_detail,
        }


def job_id_from_href(href: str) -> str | None:
    path = urlparse(href).path
    match = JOB_HREF.match(path)
    return match.group("job_id") if match else None


def _text(node: html.HtmlElement | None) -> str | None:
    if node is None:
        return None
    return html_to_text(node) or None


def _first(node: html.HtmlElement, css_class: str) -> html.HtmlElement | None:
    found = node.find_class(css_class)
    return found[0] if found else None


def parse_listing(markup: str, base_url: str) -> ListingExtraction:
    warnings: list[str] = []
    try:
        doc = html.document_fromstring(markup)
    except Exception as exc:  # noqa: BLE001
        return ListingExtraction(
            status="failed",
            structure_recognized=False,
            empty_result_validated=False,
            entries=[],
            sections_found=[],
            more_link_url=None,
            more_link_remaining=None,
            page_signature="",
            other_links=[],
            warnings=warnings,
            failure_detail=f"HTML parse failed: {exc}",
        )

    entries: list[ListingEntry] = []
    sections_found: list[str] = []
    row_counts: dict[str, int] = {}

    for tbody_id, section in SECTION_IDS.items():
        body = doc.get_element_by_id(tbody_id, None)
        if body is None:
            continue
        sections_found.append(section)
        section_entries = _parse_section(body, section, base_url, warnings)
        row_counts[section] = len(section_entries)
        entries.extend(section_entries)

    structure_recognized = "search-results" in sections_found
    if not structure_recognized:
        warnings.append("listing structure not recognised: no <tbody id='search-results-content'>")

    # A zero-row page only counts as an empty result when the surrounding
    # structure is intact. An error page that happens to yield no rows must not
    # be mistaken for "there are no jobs".
    heading_ok = any((el.text or "").strip().lower() == "search results" for el in doc.iter("h2"))
    headers = [(th.text or "").strip().lower() for th in doc.iter("th")]
    columns_ok = {"position", "location", "closes"}.issubset(set(headers))
    empty_validated = bool(
        structure_recognized
        and heading_ok
        and columns_ok
        and row_counts.get("search-results", 0) == 0
    )

    more_url, more_remaining = _parse_more_link(doc, base_url, warnings)
    other_links = _collect_other_links(doc, base_url)

    signature_ids = ",".join(
        e.external_job_id or f"?{e.position_in_section}"
        for e in entries
        if e.section == "search-results"
    )
    signature = sha256(f"{signature_ids}|{more_url or ''}".encode()).hexdigest()

    status = "ok" if structure_recognized else "partial"
    return ListingExtraction(
        status=status,
        structure_recognized=structure_recognized,
        empty_result_validated=empty_validated,
        entries=entries,
        sections_found=sections_found,
        more_link_url=more_url,
        more_link_remaining=more_remaining,
        page_signature=signature,
        other_links=other_links,
        warnings=warnings,
    )


def _parse_section(
    body: html.HtmlElement, section: str, base_url: str, warnings: list[str]
) -> list[ListingEntry]:
    entries: list[ListingEntry] = []
    position = 0
    rows = list(body.iter("tr"))
    index = 0
    while index < len(rows):
        row = rows[index]
        classes = (row.get("class") or "").split()
        if "summary" in classes:
            # A summary row without a preceding data row: keep it visible.
            warnings.append(f"{section}: orphan summary row at index {index}")
            index += 1
            continue

        links = [a for a in row.iter("a") if "job-link" in (a.get("class") or "").split()]
        summary_text: str | None = None
        summary_html: str | None = None
        if index + 1 < len(rows) and "summary" in (rows[index + 1].get("class") or "").split():
            summary_row = rows[index + 1]
            cell = next(iter(summary_row.iter("td")), None)
            summary_text = _text(cell)
            if cell is not None:
                from .text import inner_html

                summary_html = inner_html(cell)
            index += 1

        if not links:
            text = _text(row)
            if text:
                position += 1
                entries.append(
                    ListingEntry(
                        section=section,
                        position_in_section=position,
                        href_raw=None,
                        href_resolved=None,
                        external_job_id=None,
                        resolution_state="unrecognized_row",
                        resolution_detail="row carried content but no job link",
                        title_text=text,
                        summary_text=summary_text,
                        summary_html=summary_html,
                    )
                )
            index += 1
            continue

        anchor = links[0]
        href = anchor.get("href")
        resolved = urljoin(base_url, href) if href else None
        job_id = job_id_from_href(href) if href else None
        if job_id:
            state, detail = "resolved", None
        else:
            state = "unresolved_id"
            detail = f"job link href did not match the expected pattern: {href!r}"
            warnings.append(detail)

        position += 1
        entries.append(
            ListingEntry(
                section=section,
                position_in_section=position,
                href_raw=href,
                href_resolved=resolved,
                external_job_id=job_id,
                resolution_state=state,
                resolution_detail=detail,
                title_text=_text(anchor),
                summary_text=summary_text,
                summary_html=summary_html,
                displayed_metadata=_row_metadata(row),
            )
        )
        index += 1
    return entries


def _row_metadata(row: html.HtmlElement) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    location = _first(row, "location")
    if location is not None:
        meta["location"] = {"label": "Location", "text": _text(location)}
    close = _first(row, "close-date")
    if close is not None:
        time_el = next(iter(close.iter("time")), None)
        meta["close_date"] = {
            "label": "Closes",
            "text": _text(close),
            "machine_value": time_el.get("datetime") if time_el is not None else None,
        }
    # Preserve any other labelled span we do not yet know about.
    for span in row.iter("span"):
        classes = [c for c in (span.get("class") or "").split() if c]
        for cls in classes:
            if cls in {"location", "close-date"}:
                continue
            meta.setdefault("unknown_spans", []).append({"class": cls, "text": _text(span)})
    return meta


_COUNT = re.compile(r"\d+")


def _parse_more_link(
    doc: html.HtmlElement, base_url: str, warnings: list[str]
) -> tuple[str | None, int | None]:
    candidates = doc.find_class("more-link")
    if not candidates:
        return None, None
    anchor = candidates[0]
    href = anchor.get("href")
    url = urljoin(base_url, href) if href else None
    remaining: int | None = None
    count_nodes = anchor.find_class("count")
    if count_nodes:
        raw = (count_nodes[0].text or "").strip()
        match = _COUNT.search(raw)
        if match:
            remaining = int(match.group(0))
        else:
            warnings.append(f"more-link count was not a number: {raw!r}")
    else:
        warnings.append("more-link present without a count span")
    return url, remaining


def _collect_other_links(doc: html.HtmlElement, base_url: str) -> list[dict[str, Any]]:
    """Every non-job link on the page, with why it is out of scope.

    Kept so a later question about what the collector chose *not* to follow can
    be answered from the archive instead of guessed.
    """
    out: list[dict[str, Any]] = []
    for anchor in doc.iter("a"):
        href = anchor.get("href")
        if not href:
            continue
        classes = (anchor.get("class") or "").split()
        if "job-link" in classes:
            continue
        resolved = urljoin(base_url, href)
        out.append(
            {
                "url_raw": href,
                "url_resolved": resolved,
                "text": (anchor.text_content() or "").strip()[:200],
                "classes": classes,
                "classification": _classify(resolved, classes),
            }
        )
    return out


def _classify(url: str, classes: list[str]) -> str:
    if url.startswith("mailto:"):
        return "mailto"
    if url.startswith("#"):
        return "anchor"
    if "apply-link" in classes or "/apply/" in url or "employee-referral" in url:
        return "apply_workflow"
    host = urlparse(url).hostname or ""
    if host.endswith("jobs.rowan.edu"):
        return "internal_nav"
    return "external"

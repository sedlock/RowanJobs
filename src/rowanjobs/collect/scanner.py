"""Complete unfiltered pagination traversal.

The traversal does not stop because a page contributed no new identifiers --
that would confuse a repeated section with the end of the results. It stops for
exactly one of the reasons in
:data:`~rowanjobs.collect.qualify.LEGITIMATE_TERMINATION`, or it records why it
could not, which costs the scan its qualification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..extract.decode import decode_html
from ..extract.pageup_listing import ListingExtraction, parse_listing
from ..timeutil import utc_str
from .qualify import Assessment, ScanFacts, assess

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from ..net.client import SourceClient
    from .repo import Repository

PARSER_NAME = "pageup_listing"


def page_url(base_listing_url: str, page: int, page_items: int) -> str:
    parts = urlsplit(base_listing_url)
    if page <= 1 and not parts.query:
        return base_listing_url
    query = urlencode({"page": page, "page-items": page_items})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


@dataclass
class PageOutcome:
    page_number: int
    url: str
    listing_page_id: int
    fetch_id: int
    artifact_id: int | None
    extraction: ListingExtraction | None
    extraction_id: int | None
    ok: bool
    detail: str | None = None


@dataclass
class ScanResult:
    scan_id: int
    scan_role: str
    started_at_utc: str
    ended_at_utc: str
    pages: list[PageOutcome] = field(default_factory=list)
    entries_seen: int = 0
    unique_ids: list[str] = field(default_factory=list)
    unresolved_candidates: int = 0
    duplicate_occurrences: int = 0
    termination_reason: str | None = None
    source_reported_total: int | None = None
    assessment: Assessment | None = None
    facts: ScanFacts | None = None
    entry_times: dict[str, str] = field(default_factory=dict)

    @property
    def qualified(self) -> bool:
        return bool(self.assessment and self.assessment.qualified)

    @property
    def id_set(self) -> set[str]:
        return set(self.unique_ids)


class ListingScanner:
    def __init__(
        self,
        *,
        cfg: Config,
        repo: Repository,
        client: SourceClient,
        run_id: int,
        source_id: int,
    ) -> None:
        self.cfg = cfg
        self.repo = repo
        self.client = client
        self.run_id = run_id
        self.source_id = source_id

    def scan(self, *, scan_ordinal: int, scan_role: str) -> ScanResult:
        cfg = self.cfg.collection
        start_url = self.cfg.listing_url
        scan_id = self.repo.start_scan(
            run_id=self.run_id, scan_ordinal=scan_ordinal, scan_role=scan_role
        )
        started = utc_str()
        facts = ScanFacts(
            requested_start_url=start_url,
            expected_start_url=start_url,
            max_pages_bound=cfg.max_listing_pages,
        )
        result = ScanResult(
            scan_id=scan_id, scan_role=scan_role, started_at_utc=started, ended_at_utc=started
        )

        seen_ids: dict[str, None] = {}
        occurrences = 0
        page = 1
        next_url: str | None = start_url

        while next_url is not None:
            if page > cfg.max_listing_pages:
                result.termination_reason = "max_pages"
                break

            outcome = self._fetch_page(scan_id, page, next_url, facts)
            result.pages.append(outcome)
            facts.pages_requested += 1

            if not outcome.ok or outcome.extraction is None:
                facts.pages_failed += 1
                result.termination_reason = "fetch_failure"
                break

            facts.pages_ok += 1
            extraction = outcome.extraction
            if not extraction.structure_recognized:
                facts.structure_unrecognized_pages.append(page)
                result.termination_reason = "structure_unrecognized"
                break

            facts.loop_signatures.append(extraction.page_signature)
            if len(facts.loop_signatures) != len(set(facts.loop_signatures)):
                result.termination_reason = "loop_detected"
                break

            observed_at = utc_str()
            authoritative = extraction.authoritative_entries
            occurrences += len(extraction.entries)
            new_ids: list[str] = []
            for entry in authoritative:
                if entry.external_job_id and entry.external_job_id not in seen_ids:
                    seen_ids[entry.external_job_id] = None
                    new_ids.append(entry.external_job_id)
                    # Exact time this advertisement was seen in this traversal.
                    result.entry_times.setdefault(entry.external_job_id, observed_at)
            facts.unresolved_candidates += extraction.unresolved_count()

            posting_ids = self._ensure_postings(new_ids, observed_at)
            self._ensure_urls(authoritative, posting_ids, observed_at)
            all_posting_ids = self._known_posting_ids(extraction)

            self.repo.record_listing_entries(
                listing_page_id=outcome.listing_page_id,
                scan_id=scan_id,
                run_id=self.run_id,
                extraction_id=outcome.extraction_id,
                page_number=page,
                entries=extraction.entries,
                posting_ids=all_posting_ids,
                observed_at_utc=observed_at,
            )

            if page == 1 and extraction.more_link_remaining is not None:
                # The source reports how many advertisements remain after this
                # page, so the total is that plus the rows on this page.
                facts.source_reported_total = extraction.more_link_remaining + len(
                    extraction.unique_job_ids()
                )
                result.source_reported_total = facts.source_reported_total

            if extraction.more_link_url:
                next_url = extraction.more_link_url
                page += 1
                continue

            # No "More Jobs" link. Either the final page, or -- if it had no rows
            # at all -- a validated empty result.
            if not authoritative and extraction.empty_result_validated:
                result.termination_reason = "empty_validated_page"
                facts.empty_result_validated = True
            elif not authoritative:
                result.termination_reason = "structure_unrecognized"
                facts.empty_result_validated = False
            else:
                result.termination_reason = "no_more_link"
            next_url = None

        result.entries_seen = occurrences
        result.unique_ids = list(seen_ids)
        result.unresolved_candidates = facts.unresolved_candidates
        result.duplicate_occurrences = max(0, occurrences - len(seen_ids))
        result.ended_at_utc = utc_str()

        facts.entries_seen = occurrences
        facts.unique_ids = len(seen_ids)
        facts.duplicate_occurrences = result.duplicate_occurrences
        facts.termination_reason = result.termination_reason
        if facts.empty_result_validated is None and len(seen_ids) == 0:
            facts.empty_result_validated = False

        assessment = assess(facts)
        result.assessment = assessment
        result.facts = facts

        self.repo.finish_scan(
            result.scan_id,
            ended_at_utc=result.ended_at_utc,
            pages_requested=facts.pages_requested,
            pages_ok=facts.pages_ok,
            pages_failed=facts.pages_failed,
            last_page_number=len(result.pages),
            termination_reason=result.termination_reason,
            entries_seen=result.entries_seen,
            unique_ids_seen=len(seen_ids),
            unresolved_candidates=result.unresolved_candidates,
            duplicate_occurrences=result.duplicate_occurrences,
            source_reported_total=facts.source_reported_total,
            encountered_ids=list(seen_ids),
        )
        self.repo.record_scan_assessment(
            scan_id=result.scan_id,
            rules_version=assessment.rules_version,
            qualified=assessment.qualified,
            reason=assessment.reason,
            checks=assessment.checks,
        )
        return result

    # ------------------------------------------------------------------ pages

    def _fetch_page(self, scan_id: int, page: int, url: str, facts: ScanFacts) -> PageOutcome:
        fetch = self.client.fetch(url, purpose="listing_page")
        fetch_id, artifact_id = self.repo.record_fetch(fetch, self.run_id)

        if fetch.access_control_signal:
            facts.access_control_pages.append(page)
        if fetch.http_status is not None and fetch.http_status != 200:
            facts.non_200_pages.append({"page": page, "status": fetch.http_status})
        if fetch.redirect_chain:
            final = fetch.final_url or ""
            if "/listing" not in final:
                facts.unexpected_redirect_pages.append(
                    {"page": page, "final_url": final, "hops": fetch.redirect_count}
                )

        extraction: ListingExtraction | None = None
        extraction_id: int | None = None
        ok = False
        detail = fetch.failure_detail

        if fetch.ok and artifact_id is not None and fetch.body is not None:
            decoded = decode_html(fetch.body, fetch.charset_declared)
            extraction = parse_listing(decoded.text, url)
            extraction_id = self.repo.record_extraction(
                artifact_id=artifact_id,
                parser_name=PARSER_NAME,
                run_id=self.run_id,
                status=extraction.status,
                output=extraction.as_dict(),
                warnings=extraction.warnings,
                failure_detail=extraction.failure_detail,
                decode_encoding=decoded.encoding,
                decode_strategy=decoded.strategy,
                decode_error_count=decoded.error_count,
            )
            ok = extraction.status != "failed"
            if not ok:
                detail = extraction.failure_detail

        listing_page_id = self.repo.record_listing_page(
            scan_id=scan_id,
            page_number=page,
            page_items=self.cfg.collection.page_items,
            url=url,
            fetch_id=fetch_id,
            extraction_id=extraction_id,
            structure_recognized=(
                None if extraction is None else (1 if extraction.structure_recognized else 0)
            ),
            empty_result_validated=(
                None if extraction is None else (1 if extraction.empty_result_validated else 0)
            ),
            entry_count=None if extraction is None else len(extraction.entries),
            unique_id_count=None if extraction is None else len(extraction.unique_job_ids()),
            more_link_url=None if extraction is None else extraction.more_link_url,
            more_link_remaining=None if extraction is None else extraction.more_link_remaining,
            page_signature=None if extraction is None else extraction.page_signature,
            observed_at_utc=utc_str(),
        )
        return PageOutcome(
            page_number=page,
            url=url,
            listing_page_id=listing_page_id,
            fetch_id=fetch_id,
            artifact_id=artifact_id,
            extraction=extraction,
            extraction_id=extraction_id,
            ok=ok,
            detail=detail,
        )

    # ---------------------------------------------------------------- helpers

    def _ensure_postings(self, new_ids: list[str], observed_at: str) -> dict[str, int]:
        baseline_exists = self.repo.has_qualified_baseline()
        out: dict[str, int] = {}
        for job_id in new_ids:
            posting_id, _created = self.repo.ensure_posting(
                source_id=self.source_id,
                external_job_id=job_id,
                run_id=self.run_id,
                # Advertisements present at the first qualified collection are a
                # baseline, not newly posted.
                discovery_basis="observed-new" if baseline_exists else "baseline",
                observed_at_utc=observed_at,
            )
            out[job_id] = posting_id
        return out

    def _ensure_urls(
        self, entries: list[Any], posting_ids: dict[str, int], observed_at: str
    ) -> None:
        for entry in entries:
            if not entry.external_job_id or not entry.href_resolved:
                continue
            posting_id = posting_ids.get(entry.external_job_id)
            if posting_id is None:
                row = self.repo.get_posting(entry.external_job_id)
                if row is None:
                    continue
                posting_id = int(row["posting_id"])
            self.repo.record_posting_url(
                posting_id, entry.href_resolved, "listing-link", "listing_entry", observed_at
            )

    def _known_posting_ids(self, extraction: ListingExtraction) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in extraction.entries:
            job_id = entry.external_job_id
            if not job_id or job_id in out:
                continue
            row = self.repo.get_posting(job_id)
            if row:
                out[job_id] = int(row["posting_id"])
        return out

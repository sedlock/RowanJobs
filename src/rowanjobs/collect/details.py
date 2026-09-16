"""Detail retrieval, identity checking and resource capture.

Identity is checked every time: the job number the page *displays* is compared
with the job number we *expected*. A mismatch, or a redirect to a different
advertisement, is preserved as a conflict rather than resolved by taking
whichever identifier is convenient. The destination's description is never
assigned to the original posting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..archive.store import content_hash
from ..extract.decode import decode_html
from ..extract.pageup_detail import job_id_from_url, parse_detail
from ..timeutil import utc_str

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from ..net.client import SourceClient
    from .repo import Repository

PARSER_NAME = "pageup_detail"

TEXT_MEDIA_PREFIXES = ("text/",)
TEXT_MEDIA_TYPES = {"application/json", "application/xml", "application/xhtml+xml"}


@dataclass
class DetailOutcome:
    external_job_id: str
    posting_id: int | None
    observation_id: int | None
    availability_state: str
    identity_state: str
    posting_version_id: int | None = None
    version_created: bool = False
    artifact_deduplicated: bool = False
    resources: list[dict[str, Any]] = field(default_factory=list)
    detail: str | None = None

    @property
    def captured(self) -> bool:
        return self.availability_state == "content_captured"

    @property
    def uncertain(self) -> bool:
        return self.availability_state in ("access_control_challenge", "retrieval_failed")


class DetailCollector:
    def __init__(
        self,
        *,
        cfg: Config,
        repo: Repository,
        client: SourceClient,
        run_id: int,
    ) -> None:
        self.cfg = cfg
        self.repo = repo
        self.client = client
        self.run_id = run_id
        # One resource retrieval can serve several parents inside a run.
        self._resource_cache: dict[str, int] = {}

    def collect(
        self,
        *,
        external_job_id: str,
        url: str,
        posting_id: int | None,
        checked_because: str,
    ) -> DetailOutcome:
        fetch = self.client.fetch(url, purpose="posting_detail")
        fetch_id, artifact_id = self.repo.record_fetch(fetch, self.run_id)
        observed_at = fetch.started_at_utc or utc_str()

        conflicts: list[dict[str, Any]] = []
        redirect_class = self._redirect_class(url, fetch.final_url, fetch.redirect_count)

        if fetch.access_control_signal:
            return self._record(
                external_job_id,
                posting_id,
                url,
                fetch_id,
                observed_at,
                identity_state="not_observed",
                availability_state="access_control_challenge",
                availability_detail=fetch.failure_detail,
                redirect_class=redirect_class,
                checked_because=checked_because,
                conflicts=conflicts,
            )

        if not fetch.ok or fetch.body is None or artifact_id is None:
            state = "retrieval_failed"
            if fetch.http_status == 404:
                state = "not_found"
            return self._record(
                external_job_id,
                posting_id,
                url,
                fetch_id,
                observed_at,
                identity_state="not_observed",
                availability_state=state,
                availability_detail=fetch.failure_detail or f"HTTP {fetch.http_status}",
                redirect_class=redirect_class,
                checked_because=checked_because,
                conflicts=conflicts,
            )

        decoded = decode_html(fetch.body, fetch.charset_declared)
        extraction = parse_detail(decoded.text, fetch.final_url or url)
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

        if redirect_class == "to_listing":
            return self._record(
                external_job_id,
                posting_id,
                url,
                fetch_id,
                observed_at,
                identity_state="absent_on_page",
                availability_state="redirected_to_listing",
                availability_detail=(
                    f"detail URL redirected to {fetch.final_url}; this is not a "
                    "successful detail retrieval"
                ),
                redirect_class=redirect_class,
                checked_because=checked_because,
                extraction_id=extraction_id,
                conflicts=conflicts,
            )

        observed_id = extraction.external_job_id
        if observed_id is None:
            identity_state = "absent_on_page"
        elif str(observed_id) == str(external_job_id):
            identity_state = "match"
        else:
            identity_state = "mismatch"
            conflicts.append(
                {
                    "kind": "job_id_mismatch",
                    "expected": str(external_job_id),
                    "observed": str(observed_id),
                    "requested_url": url,
                    "final_url": fetch.final_url,
                    "note": "the destination's content is NOT assigned to the expected "
                    "posting; both identities are preserved unresolved",
                }
            )

        if identity_state == "mismatch":
            return self._record(
                external_job_id,
                posting_id,
                url,
                fetch_id,
                observed_at,
                identity_state=identity_state,
                availability_state=(
                    "redirected_to_other_job"
                    if redirect_class == "to_other_job"
                    else "identity_mismatch"
                ),
                availability_detail=(
                    f"page displayed job {observed_id}, expected {external_job_id}"
                ),
                redirect_class=redirect_class,
                checked_because=checked_because,
                extraction_id=extraction_id,
                observed_external_job_id=str(observed_id),
                conflicts=conflicts,
            )

        if extraction.description_html is None:
            # No advertisement body. A closure template says so explicitly; an
            # empty container does not, and the two are kept apart.
            return self._record(
                external_job_id,
                posting_id,
                url,
                fetch_id,
                observed_at,
                identity_state=identity_state,
                availability_state=(
                    "explicit_closure" if extraction.closure_signal else "not_found"
                ),
                availability_detail=extraction.closure_signal
                or "recognised page carried no advertisement body",
                redirect_class=redirect_class,
                checked_because=checked_because,
                extraction_id=extraction_id,
                observed_external_job_id=observed_id,
                conflicts=conflicts,
            )

        if extraction.closure_signal:
            # A closure notice alongside a full advertisement body is a
            # contradiction, not a closure. Capturing the content and recording
            # the disagreement keeps the evidence; treating it as closure would
            # throw the description away and assert something the page did not.
            conflicts.append(
                {
                    "kind": "closure_signal_with_content",
                    "closure_signal": extraction.closure_signal,
                    "note": "the page showed a closure-like notice and a complete "
                    "advertisement body; the content is captured and the notice is "
                    "preserved unresolved",
                }
            )

        if posting_id is None:
            return self._record(
                external_job_id,
                None,
                url,
                fetch_id,
                observed_at,
                identity_state=identity_state,
                availability_state="content_captured",
                availability_detail="posting identity not yet registered",
                redirect_class=redirect_class,
                checked_because=checked_because,
                extraction_id=extraction_id,
                observed_external_job_id=observed_id,
                conflicts=conflicts,
            )

        version_id, created = self.repo.ensure_posting_version(
            posting_id=posting_id,
            extraction=extraction,
            extraction_id=extraction_id,
            run_id=self.run_id,
            observed_at_utc=observed_at,
        )
        conflicts.extend(self._field_conflicts(extraction))

        self.repo.record_posting_url(
            posting_id, fetch.final_url or url, "canonical-detail", "detail_page", observed_at
        )

        outcome = self._record(
            external_job_id,
            posting_id,
            url,
            fetch_id,
            observed_at,
            identity_state=identity_state,
            availability_state="content_captured",
            availability_detail=None,
            redirect_class=redirect_class,
            checked_because=checked_because,
            extraction_id=extraction_id,
            observed_external_job_id=observed_id,
            posting_version_id=version_id,
            conflicts=conflicts,
        )
        outcome.version_created = created
        if self.cfg.collection.collect_resources and outcome.observation_id:
            outcome.resources = self._collect_resources(
                version_id, outcome.observation_id, extraction
            )
        return outcome

    # ---------------------------------------------------------------- helpers

    def _record(
        self,
        external_job_id: str,
        posting_id: int | None,
        url: str,
        fetch_id: int,
        observed_at: str,
        *,
        identity_state: str,
        availability_state: str,
        availability_detail: str | None,
        redirect_class: str | None,
        checked_because: str,
        extraction_id: int | None = None,
        observed_external_job_id: str | None = None,
        posting_version_id: int | None = None,
        conflicts: list[dict[str, Any]] | None = None,
    ) -> DetailOutcome:
        observation_id = self.repo.record_observation(
            run_id=self.run_id,
            posting_id=posting_id,
            expected_external_job_id=str(external_job_id),
            requested_url=url,
            fetch_id=fetch_id,
            observed_at_utc=observed_at,
            observed_external_job_id=observed_external_job_id,
            identity_state=identity_state,
            availability_state=availability_state,
            availability_detail=availability_detail,
            redirect_class=redirect_class,
            extraction_id=extraction_id,
            posting_version_id=posting_version_id,
            checked_because=checked_because,
            conflicts=conflicts or None,
        )
        return DetailOutcome(
            external_job_id=str(external_job_id),
            posting_id=posting_id,
            observation_id=observation_id,
            availability_state=availability_state,
            identity_state=identity_state,
            posting_version_id=posting_version_id,
            detail=availability_detail,
        )

    @staticmethod
    def _redirect_class(requested: str, final: str | None, hops: int) -> str:
        if not final or hops == 0 or final == requested:
            return "none"
        path = urlparse(final).path
        if "/listing" in path:
            return "to_listing"
        destination = job_id_from_url(final)
        origin = job_id_from_url(requested)
        if destination and origin and destination != origin:
            return "to_other_job"
        if destination and origin and destination == origin:
            return "same_job_canonicalised"
        return "to_other"

    @staticmethod
    def _field_conflicts(extraction: Any) -> list[dict[str, Any]]:
        """Note disagreements between structured fields without resolving them.

        Conflicting values are recorded side by side. Reconciling them into one
        "correct" value would invent a fact the source never published.
        """
        out: list[dict[str, Any]] = []
        job_no = next((v.value_text for v in extraction.values if v.field_key == "job_no"), None)
        if job_no and extraction.external_job_id and job_no != extraction.external_job_id:
            out.append(
                {
                    "kind": "job_no_label_vs_span",
                    "label_value": job_no,
                    "span_value": extraction.external_job_id,
                    "note": "both preserved; no authoritative value invented",
                }
            )
        for key in ("location", "categories", "work_type"):
            values = [
                v.value_text
                for v in extraction.values
                if v.field_key == key and v.field_state == "present"
            ]
            if len(set(values)) > 1:
                out.append(
                    {
                        "kind": f"{key}_multiple_values",
                        "values": values,
                        "note": "the source presented this field more than once with "
                        "differing values; all are preserved in order",
                    }
                )
        return out

    # -------------------------------------------------------------- resources

    def _collect_resources(
        self, version_id: int, observation_id: int, extraction: Any
    ) -> list[dict[str, Any]]:
        """Retrieve the in-scope documents this extraction identified.

        The decision comes from the *current* extraction, not from the
        ``resource_links`` rows written when the version was first seen. Those
        rows record what the adapter decided at the time and stay as they are;
        if the collection scope is widened later, a re-observation of unchanged
        content still picks up the newly in-scope document.
        """
        out: list[dict[str, Any]] = []
        for link in extraction.links:
            if link["collection_decision"] != "fetch":
                continue
            url = str(link["url_resolved"])
            row = self.repo.db.one(
                "SELECT resource_link_id FROM resource_links WHERE posting_version_id = ? "
                "AND url_raw = ? ORDER BY resource_link_id LIMIT 1",
                (version_id, link["url_raw"]),
            )
            link_id = int(row["resource_link_id"]) if row else None
            cached = self._resource_cache.get(url)
            if cached is not None:
                self.repo.associate_resource(cached, observation_id, link_id)
                out.append({"url": url, "outcome": "reused_within_run"})
                continue
            resource_observation_id, outcome = self._fetch_resource(url)
            self._resource_cache[url] = resource_observation_id
            self.repo.associate_resource(resource_observation_id, observation_id, link_id)
            out.append({"url": url, "outcome": outcome})
        return out

    def _fetch_resource(self, url: str) -> tuple[int, str]:
        limit = self.cfg.network.max_resource_bytes
        fetch = self.client.fetch(url, purpose="resource", max_bytes=limit)
        fetch_id, artifact_id = self.repo.record_fetch(fetch, self.run_id)
        observed_at = fetch.started_at_utc or utc_str()

        outcome: str
        detail: str | None
        if fetch.access_control_signal:
            outcome, detail = "failed", f"access control: {fetch.access_control_signal}"
        elif fetch.failure_kind == "blocked_destination":
            outcome, detail = "blocked_destination", fetch.failure_detail
        elif fetch.capture_state == "partial":
            outcome, detail = "too_large", fetch.capture_exception
        elif not fetch.ok or fetch.body is None:
            outcome, detail = "failed", fetch.failure_detail or f"HTTP {fetch.http_status}"
        else:
            outcome, detail = "captured", None

        text_state, text_content = self._resource_text(fetch, outcome)
        observation_id = self.repo.record_resource_observation(
            run_id=self.run_id,
            url_resolved=url,
            fetch_id=fetch_id,
            observed_at_utc=observed_at,
            outcome=outcome,
            outcome_detail=detail,
            artifact_id=artifact_id,
            content_sha256=content_hash(fetch.body) if fetch.body else None,
            media_type=fetch.media_type,
            byte_length=fetch.received_bytes,
            text_extraction_state=text_state,
            text_content=text_content,
        )
        return observation_id, outcome

    @staticmethod
    def _resource_text(fetch: Any, outcome: str) -> tuple[str, str | None]:
        """Text extraction is secondary; the bytes are always preserved."""
        if outcome != "captured" or not fetch.body:
            return "not_attempted", None
        media = (fetch.media_type or "").lower()
        if media.startswith(TEXT_MEDIA_PREFIXES) or media in TEXT_MEDIA_TYPES:
            decoded = decode_html(fetch.body, fetch.charset_declared)
            return ("ok" if decoded.lossless else "failed"), decoded.text
        return "unavailable", None

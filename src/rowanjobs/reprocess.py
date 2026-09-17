"""Offline reprocessing.

Re-runs the parsers over *archived payloads only*. It never contacts the
website, so it cannot create a new observation or a new retrieval time. A parser
upgrade therefore produces new extraction rows -- and, if the comparison
contract changed, a parallel line of content versions -- without any of it
looking like a source edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import CONTRACT_VERSION, PARSER_VERSION, TEXT_CONTRACT_VERSION
from .archive import ArchiveStore
from .collect.repo import Repository
from .db import Database
from .extract.decode import decode_html
from .extract.fingerprint import comparison_lineage
from .extract.pageup_detail import parse_detail
from .extract.pageup_listing import parse_listing
from .timeutil import utc_str


@dataclass
class ReprocessReport:
    parser: str
    artifacts_considered: int = 0
    extractions_created: int = 0
    extractions_reused: int = 0
    versions_created: int = 0
    observations_relinked: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    started_at_utc: str = ""
    ended_at_utc: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "parser": self.parser,
            "parser_version": PARSER_VERSION,
            "contract_version": CONTRACT_VERSION,
            "text_contract_version": TEXT_CONTRACT_VERSION,
            "artifacts_considered": self.artifacts_considered,
            "extractions_created": self.extractions_created,
            "extractions_reused": self.extractions_reused,
            "versions_created": self.versions_created,
            "observations_relinked": self.observations_relinked,
            "failures": self.failures,
            "started_at_utc": self.started_at_utc,
            "ended_at_utc": self.ended_at_utc,
            "note": "no network requests were made; observation times are unchanged",
        }


def reprocess_details(
    db: Database,
    *,
    job_id: str | None = None,
    limit: int | None = None,
    relink: bool = False,
) -> ReprocessReport:
    """Re-parse archived detail payloads.

    ``relink`` rewrites ``posting_observations.extraction_id`` and
    ``posting_version_id`` to point at the new reading. It is **off by default**:
    it is the one operation in this project that mutates an evidence row, and it
    destroys the record of which interpretation an observation was originally
    made under. Suppressing spurious change events does not need it -- the
    lineage scoping in :mod:`rowanjobs.collect.events` already does that.
    """
    repo = Repository(db)
    store = ArchiveStore(db)
    report = ReprocessReport(parser="pageup_detail", started_at_utc=utc_str())

    sql = """
        SELECT o.observation_id, o.posting_id, o.expected_external_job_id,
               o.requested_url, o.observed_at_utc, o.run_id, f.artifact_id,
               a.charset_declared, p.external_job_id
          FROM posting_observations o
          JOIN fetches f ON f.fetch_id = o.fetch_id
          JOIN artifacts a ON a.artifact_id = f.artifact_id
          LEFT JOIN postings p ON p.posting_id = o.posting_id
         WHERE f.artifact_id IS NOT NULL
           AND o.availability_state = 'content_captured'
           AND o.identity_state = 'match'
    """
    params: list[Any] = []
    if job_id:
        sql += " AND o.expected_external_job_id = ?"
        params.append(str(job_id))
    sql += " ORDER BY o.observed_at_utc"
    if limit:
        sql += f" LIMIT {int(limit)}"

    for row in db.query(sql, tuple(params)):
        artifact_id = int(row["artifact_id"])
        report.artifacts_considered += 1
        try:
            payload = store.get(artifact_id)
        except Exception as exc:  # noqa: BLE001
            report.failures.append({"artifact_id": artifact_id, "detail": str(exc)})
            continue

        existing = db.one(
            "SELECT extraction_id FROM extractions WHERE artifact_id = ? AND parser_name = ? "
            "AND parser_version = ? AND contract_version = ? AND text_contract_version = ?",
            (artifact_id, "pageup_detail", PARSER_VERSION, CONTRACT_VERSION, TEXT_CONTRACT_VERSION),
        )
        decoded = decode_html(payload, row["charset_declared"])
        extraction = parse_detail(decoded.text, str(row["requested_url"]))
        extraction_id = repo.record_extraction(
            artifact_id=artifact_id,
            parser_name="pageup_detail",
            run_id=None,
            status=extraction.status,
            output=extraction.as_dict(),
            warnings=extraction.warnings,
            failure_detail=extraction.failure_detail,
            decode_encoding=decoded.encoding,
            decode_strategy=decoded.strategy,
            decode_error_count=decoded.error_count,
        )
        if existing:
            report.extractions_reused += 1
        else:
            report.extractions_created += 1

        if extraction.description_html is None or row["posting_id"] is None:
            continue
        version_id, created = repo.ensure_posting_version(
            posting_id=int(row["posting_id"]),
            extraction=extraction,
            extraction_id=extraction_id,
            run_id=None,
            # Reprocessing keeps the ORIGINAL observation time. It did not
            # observe anything; it reinterpreted stored evidence.
            observed_at_utc=str(row["observed_at_utc"]),
        )
        if created:
            report.versions_created += 1
        if relink and _observation_lineage(db, row["observation_id"]) != comparison_lineage():
            with db.write():
                db.execute(
                    "UPDATE posting_observations SET extraction_id = ?, "
                    "posting_version_id = ? WHERE observation_id = ?",
                    (extraction_id, version_id, int(row["observation_id"])),
                )
            report.observations_relinked += 1

    report.ended_at_utc = utc_str()
    return report


def _observation_lineage(db: Database, observation_id: Any) -> str | None:
    """The comparison lineage an observation is currently linked to.

    Comparing only ``contract_version`` meant that after a ``PARSER_VERSION`` or
    ``TEXT_CONTRACT_VERSION`` bump -- exactly the cases lineage scoping exists
    for -- the guard compared equal and ``--relink`` silently did nothing.
    """
    row = db.one(
        "SELECT v.parser_version, v.contract_version, v.text_contract_version "
        "FROM posting_observations o "
        "JOIN posting_versions v ON v.posting_version_id = o.posting_version_id "
        "WHERE o.observation_id = ?",
        (int(observation_id),),
    )
    if row is None:
        return None
    return comparison_lineage(
        str(row["parser_version"] or ""),
        str(row["contract_version"]),
        str(row["text_contract_version"]),
    )


def reprocess_listings(db: Database, *, limit: int | None = None) -> ReprocessReport:
    repo = Repository(db)
    store = ArchiveStore(db)
    report = ReprocessReport(parser="pageup_listing", started_at_utc=utc_str())
    sql = (
        "SELECT lp.listing_page_id, lp.url, f.artifact_id, a.charset_declared "
        "FROM listing_pages lp JOIN fetches f ON f.fetch_id = lp.fetch_id "
        "JOIN artifacts a ON a.artifact_id = f.artifact_id "
        "WHERE f.artifact_id IS NOT NULL ORDER BY lp.listing_page_id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    for row in db.query(sql):
        artifact_id = int(row["artifact_id"])
        report.artifacts_considered += 1
        try:
            payload = store.get(artifact_id)
        except Exception as exc:  # noqa: BLE001
            report.failures.append({"artifact_id": artifact_id, "detail": str(exc)})
            continue
        existing = db.one(
            "SELECT extraction_id FROM extractions WHERE artifact_id = ? AND parser_name = ? "
            "AND parser_version = ? AND contract_version = ? AND text_contract_version = ?",
            (
                artifact_id,
                "pageup_listing",
                PARSER_VERSION,
                CONTRACT_VERSION,
                TEXT_CONTRACT_VERSION,
            ),
        )
        decoded = decode_html(payload, row["charset_declared"])
        extraction = parse_listing(decoded.text, str(row["url"]))
        repo.record_extraction(
            artifact_id=artifact_id,
            parser_name="pageup_listing",
            run_id=None,
            status=extraction.status,
            output=extraction.as_dict(),
            warnings=extraction.warnings,
            failure_detail=extraction.failure_detail,
            decode_encoding=decoded.encoding,
            decode_strategy=decoded.strategy,
            decode_error_count=decoded.error_count,
        )
        if existing:
            report.extractions_reused += 1
        else:
            report.extractions_created += 1
    report.ended_at_utc = utc_str()
    return report

"""Persistence for collection.

Every method here owns a short transaction, so progress is durable as it is
made: if the process is killed halfway through a harvest, everything already
written stays written and the run resumes from the work queue rather than
starting over.

No method performs network I/O, and no write transaction is ever held open
across one.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from typing import TYPE_CHECKING, Any

from .. import (
    CONTRACT_VERSION,
    PARSER_VERSION,
    SOURCE_NAMESPACE,
    TEXT_CONTRACT_VERSION,
    __version__,
)
from ..archive import ArchiveStore
from ..db import Database
from ..db.runtime import runtime_info
from ..extract.fingerprint import (
    content_fingerprint,
    html_fingerprint,
    metadata_fingerprint,
    text_fingerprint,
)
from ..timeutil import utc_str

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from ..extract.pageup_detail import DetailExtraction
    from ..net.client import FetchResult


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.archive = ArchiveStore(db)

    # ---------------------------------------------------------------- source

    def ensure_source(self, cfg: Config) -> int:
        row = self.db.one("SELECT source_id FROM sources WHERE namespace = ?", (SOURCE_NAMESPACE,))
        if row:
            return int(row["source_id"])
        with self.db.write():
            return self.db.insert(
                "INSERT INTO sources(namespace, display_name, base_url, adapter, created_at_utc) "
                "VALUES (?,?,?,?,?)",
                (
                    SOURCE_NAMESPACE,
                    "Rowan University career site (PageUp)",
                    cfg.collection.base_url,
                    "pageup_v1",
                    utc_str(),
                ),
            )

    def ensure_source_config(self, source_id: int, cfg: Config) -> int:
        digest = cfg.config_hash()
        row = self.db.one(
            "SELECT source_config_id FROM source_configs WHERE source_id = ? AND config_hash = ?",
            (source_id, digest),
        )
        if row:
            return int(row["source_config_id"])
        with self.db.write():
            return self.db.insert(
                """
                INSERT INTO source_configs(
                    source_id, config_hash, config_json, scope_label, locale,
                    start_urls_json, retrieval_policy_json, comparability_group,
                    app_version, created_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source_id,
                    digest,
                    _json(cfg.effective_dict()),
                    cfg.collection.scope_label,
                    cfg.collection.locale,
                    _json(cfg.start_urls()),
                    _json(cfg.retrieval_policy()),
                    cfg.collection.comparability_group,
                    __version__,
                    utc_str(),
                ),
            )

    # ------------------------------------------------------------------- runs

    def start_run(
        self,
        *,
        source_id: int,
        source_config_id: int,
        run_kind: str,
        scheduled_slot_utc: str | None,
        scheduled_slot_local_date: str | None,
        parent_run_id: int | None = None,
        attempt_no: int = 1,
        app_revision: str | None = None,
        is_baseline: bool = False,
    ) -> tuple[int, str]:
        run_uuid = str(uuid.uuid4())
        now = utc_str()
        with self.db.write():
            run_id = self.db.insert(
                """
                INSERT INTO collection_runs(
                    run_uuid, source_id, source_config_id, run_kind,
                    scheduled_slot_utc, scheduled_slot_local_date, parent_run_id,
                    attempt_no, app_version, app_revision, host, os_user, pid,
                    sqlite_runtime_json, started_at_utc, heartbeat_at_utc, is_baseline)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_uuid,
                    source_id,
                    source_config_id,
                    run_kind,
                    scheduled_slot_utc,
                    scheduled_slot_local_date,
                    parent_run_id,
                    attempt_no,
                    __version__,
                    app_revision,
                    socket.gethostname(),
                    os.environ.get("USER") or os.environ.get("LOGNAME") or "",
                    os.getpid(),
                    _json(runtime_info().as_dict()),
                    now,
                    now,
                    1 if is_baseline else 0,
                ),
            )
        return run_id, run_uuid

    def heartbeat(self, run_id: int) -> None:
        with self.db.write():
            self.db.execute(
                "UPDATE collection_runs SET heartbeat_at_utc = ? WHERE run_id = ?",
                (utc_str(), run_id),
            )

    def finish_run(
        self,
        run_id: int,
        *,
        outcome: str,
        outcome_detail: str | None = None,
        counts: dict[str, Any] | None = None,
        errors: list[Any] | None = None,
        coverage: dict[str, Any] | None = None,
    ) -> None:
        with self.db.write():
            self.db.execute(
                """
                UPDATE collection_runs
                   SET ended_at_utc = ?, heartbeat_at_utc = ?, outcome = ?,
                       outcome_detail = ?, counts_json = ?, errors_json = ?,
                       coverage_json = ?
                 WHERE run_id = ?
                """,
                (
                    utc_str(),
                    utc_str(),
                    outcome,
                    outcome_detail,
                    _json(counts),
                    _json(errors),
                    _json(coverage),
                    run_id,
                ),
            )

    def abandon_stale_runs(self, *, exclude_run_id: int | None = None) -> list[int]:
        """Mark runs that never finished as aborted.

        Called at startup while the collector lock is held: nothing else can be
        running, so any open run belongs to a process that died.
        """
        rows = self.db.query(
            "SELECT run_id FROM collection_runs WHERE ended_at_utc IS NULL "
            "AND run_id != COALESCE(?, -1)",
            (exclude_run_id,),
        )
        ids = [int(r["run_id"]) for r in rows]
        if not ids:
            return []
        with self.db.write():
            for run_id in ids:
                self.db.execute(
                    "UPDATE collection_runs SET ended_at_utc = ?, outcome = 'aborted', "
                    "outcome_detail = ? WHERE run_id = ?",
                    (
                        utc_str(),
                        "run did not finish; the process ended before it could be closed. "
                        "Evidence already written is preserved.",
                        run_id,
                    ),
                )
                self.db.execute(
                    "UPDATE work_queue SET state = 'abandoned', updated_at_utc = ?, "
                    "last_error = 'owning run was abandoned' "
                    "WHERE run_id = ? AND state IN ('pending','in_progress')",
                    (utc_str(), run_id),
                )
        return ids

    # ---------------------------------------------------------------- fetches

    def record_fetch(self, result: FetchResult, run_id: int | None) -> tuple[int, int | None]:
        """Archive the payload and persist the retrieval attempt. Returns ids."""
        with self.db.write():
            artifact = self.archive.put_fetch(result, run_id)
            fetch_id = self.db.insert(
                """
                INSERT INTO fetches(
                    run_id, purpose, requested_url, final_url, method, transport,
                    attempt_no, started_at_utc, ended_at_utc, duration_ms,
                    http_status, http_version, response_state, redirect_count,
                    redirect_chain_json, request_headers_json, response_headers_json,
                    content_encoding, received_bytes, artifact_id,
                    access_control_signal, retry_after, failure_kind, failure_detail)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    result.purpose,
                    result.requested_url,
                    result.final_url,
                    "GET",
                    result.transport,
                    result.attempt_no,
                    result.started_at_utc,
                    result.ended_at_utc,
                    result.duration_ms,
                    result.http_status,
                    result.http_version,
                    result.response_state,
                    result.redirect_count,
                    _json(result.redirect_chain_json()) if result.redirect_chain else None,
                    _json(result.request_headers),
                    _json(result.response_headers),
                    result.content_encoding,
                    result.received_bytes,
                    artifact.artifact_id if artifact else None,
                    result.access_control_signal,
                    result.retry_after,
                    result.failure_kind,
                    result.failure_detail,
                ),
            )
        return fetch_id, (artifact.artifact_id if artifact else None)

    # ------------------------------------------------------------ extractions

    def record_extraction(
        self,
        *,
        artifact_id: int,
        parser_name: str,
        run_id: int | None,
        status: str,
        output: dict[str, Any] | None,
        warnings: list[str] | None,
        failure_detail: str | None,
        decode_encoding: str | None,
        decode_strategy: str | None,
        decode_error_count: int,
    ) -> int:
        existing = self.db.one(
            "SELECT extraction_id FROM extractions WHERE artifact_id = ? AND parser_name = ? "
            "AND parser_version = ? AND contract_version = ? AND text_contract_version = ?",
            (artifact_id, parser_name, PARSER_VERSION, CONTRACT_VERSION, TEXT_CONTRACT_VERSION),
        )
        if existing:
            return int(existing["extraction_id"])
        with self.db.write():
            return self.db.insert(
                """
                INSERT INTO extractions(
                    artifact_id, parser_name, parser_version, contract_version,
                    text_contract_version, extracted_at_utc, run_id, status,
                    output_json, warnings_json, failure_detail, decode_encoding,
                    decode_strategy, decode_error_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    artifact_id,
                    parser_name,
                    PARSER_VERSION,
                    CONTRACT_VERSION,
                    TEXT_CONTRACT_VERSION,
                    utc_str(),
                    run_id,
                    status,
                    _json(output),
                    _json(warnings) if warnings else None,
                    failure_detail,
                    decode_encoding,
                    decode_strategy,
                    decode_error_count,
                ),
            )

    # ------------------------------------------------------------- postings

    def get_posting(self, external_job_id: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT * FROM postings WHERE source_namespace = ? AND external_job_id = ?",
            (SOURCE_NAMESPACE, str(external_job_id)),
        )

    def ensure_posting(
        self,
        *,
        source_id: int,
        external_job_id: str,
        run_id: int,
        discovery_basis: str,
        observed_at_utc: str,
    ) -> tuple[int, bool]:
        """Return ``(posting_id, created)``."""
        existing = self.get_posting(external_job_id)
        if existing:
            return int(existing["posting_id"]), False
        with self.db.write():
            # Re-check inside the transaction: a concurrent reader could have
            # raced us between the SELECT above and here.
            again = self.db.one(
                "SELECT posting_id FROM postings WHERE source_namespace = ? "
                "AND external_job_id = ?",
                (SOURCE_NAMESPACE, str(external_job_id)),
            )
            if again:
                return int(again["posting_id"]), False
            posting_id = self.db.insert(
                """
                INSERT INTO postings(
                    source_id, source_namespace, external_job_id,
                    first_discovered_at_utc, first_discovered_run_id, discovery_basis)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    source_id,
                    SOURCE_NAMESPACE,
                    str(external_job_id),
                    observed_at_utc,
                    run_id,
                    discovery_basis,
                ),
            )
        return posting_id, True

    def record_posting_url(
        self, posting_id: int, url: str, role: str, provenance: str, at_utc: str
    ) -> None:
        with self.db.write():
            existing = self.db.one(
                "SELECT posting_url_id FROM posting_urls WHERE posting_id = ? AND url = ? "
                "AND role = ?",
                (posting_id, url, role),
            )
            if existing:
                self.db.execute(
                    "UPDATE posting_urls SET last_seen_at_utc = ?, seen_count = seen_count + 1 "
                    "WHERE posting_url_id = ?",
                    (at_utc, int(existing["posting_url_id"])),
                )
            else:
                self.db.execute(
                    "INSERT INTO posting_urls(posting_id, url, role, provenance, "
                    "first_seen_at_utc, last_seen_at_utc, seen_count) VALUES (?,?,?,?,?,?,1)",
                    (posting_id, url, role, provenance, at_utc, at_utc),
                )

    def has_qualified_baseline(self) -> bool:
        row = self.db.one("SELECT 1 AS present FROM v_qualified_scans LIMIT 1")
        return row is not None

    # ---------------------------------------------------------- listing scans

    def start_scan(self, *, run_id: int, scan_ordinal: int, scan_role: str) -> int:
        with self.db.write():
            return self.db.insert(
                "INSERT INTO listing_scans(run_id, scan_ordinal, scan_role, started_at_utc) "
                "VALUES (?,?,?,?)",
                (run_id, scan_ordinal, scan_role, utc_str()),
            )

    def finish_scan(self, scan_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields.setdefault("ended_at_utc", utc_str())
        if "encountered_ids" in fields:
            fields["encountered_ids_json"] = _json(sorted(fields.pop("encountered_ids")))
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.db.write():
            self.db.execute(
                f"UPDATE listing_scans SET {assignments} WHERE scan_id = ?",
                (*fields.values(), scan_id),
            )

    def record_listing_page(self, **fields: Any) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.db.write():
            return self.db.insert(
                f"INSERT INTO listing_pages({cols}) VALUES ({marks})", tuple(fields.values())
            )

    def record_listing_entries(
        self,
        *,
        listing_page_id: int,
        scan_id: int,
        run_id: int,
        extraction_id: int | None,
        page_number: int,
        entries: list[Any],
        posting_ids: dict[str, int],
        observed_at_utc: str,
    ) -> None:
        with self.db.write():
            for entry in entries:
                self.db.execute(
                    """
                    INSERT INTO listing_entries(
                        listing_page_id, scan_id, run_id, extraction_id, section,
                        position_in_section, page_number, href_raw, href_resolved,
                        external_job_id, posting_id, resolution_state, resolution_detail,
                        title_text, summary_text, summary_html, displayed_metadata_json,
                        observed_at_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        listing_page_id,
                        scan_id,
                        run_id,
                        extraction_id,
                        entry.section,
                        entry.position_in_section,
                        page_number,
                        entry.href_raw,
                        entry.href_resolved,
                        entry.external_job_id,
                        posting_ids.get(entry.external_job_id or ""),
                        entry.resolution_state,
                        entry.resolution_detail,
                        entry.title_text,
                        entry.summary_text,
                        entry.summary_html,
                        _json(entry.displayed_metadata) if entry.displayed_metadata else None,
                        observed_at_utc,
                    ),
                )

    def record_scan_assessment(
        self,
        *,
        scan_id: int,
        rules_version: str,
        qualified: bool,
        reason: str | None,
        checks: list[dict[str, Any]],
    ) -> int:
        with self.db.write():
            existing = self.db.one(
                "SELECT assessment_id FROM listing_scan_assessments WHERE scan_id = ? "
                "AND rules_version = ?",
                (scan_id, rules_version),
            )
            if existing:
                return int(existing["assessment_id"])
            return self.db.insert(
                "INSERT INTO listing_scan_assessments(scan_id, rules_version, assessed_at_utc, "
                "qualified, reason, checks_json) VALUES (?,?,?,?,?,?)",
                (scan_id, rules_version, utc_str(), 1 if qualified else 0, reason, _json(checks)),
            )

    # ----------------------------------------------------------- observations

    def record_observation(self, **fields: Any) -> int:
        if "conflicts" in fields:
            fields["conflicts_json"] = _json(fields.pop("conflicts"))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.db.write():
            return self.db.insert(
                f"INSERT INTO posting_observations({cols}) VALUES ({marks})",
                tuple(fields.values()),
            )

    # -------------------------------------------------------------- versions

    def ensure_posting_version(
        self,
        *,
        posting_id: int,
        extraction: DetailExtraction,
        extraction_id: int,
        run_id: int | None,
        observed_at_utc: str,
    ) -> tuple[int, bool]:
        """Return ``(posting_version_id, created)`` for this extracted content.

        Identical content re-observed tomorrow reuses today's version row, so a
        history of A -> B -> A is three observations across two versions.
        """
        values = extraction.value_dicts()
        text_fp = text_fingerprint(extraction.description_text)
        html_fp = html_fingerprint(extraction.description_html)
        meta_fp = metadata_fingerprint(values)
        content_fp = content_fingerprint(
            title=extraction.title,
            description_text_fp=text_fp,
            description_html_fp=html_fp,
            metadata_fp=meta_fp,
        )
        existing = self.db.one(
            "SELECT posting_version_id FROM posting_versions WHERE posting_id = ? "
            "AND contract_version = ? AND content_fingerprint = ?",
            (posting_id, CONTRACT_VERSION, content_fp),
        )
        if existing:
            return int(existing["posting_version_id"]), False

        with self.db.write():
            again = self.db.one(
                "SELECT posting_version_id FROM posting_versions WHERE posting_id = ? "
                "AND contract_version = ? AND content_fingerprint = ?",
                (posting_id, CONTRACT_VERSION, content_fp),
            )
            if again:
                return int(again["posting_version_id"]), False
            version_id = self.db.insert(
                """
                INSERT INTO posting_versions(
                    posting_id, contract_version, text_contract_version,
                    content_fingerprint, description_text_fingerprint,
                    description_html_fingerprint, metadata_fingerprint, title,
                    description_html, description_html_kind, description_text,
                    first_seen_at_utc, first_extraction_id, first_run_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    posting_id,
                    CONTRACT_VERSION,
                    TEXT_CONTRACT_VERSION,
                    content_fp,
                    text_fp,
                    html_fp,
                    meta_fp,
                    extraction.title,
                    extraction.description_html,
                    extraction.description_html_kind,
                    extraction.description_text,
                    observed_at_utc,
                    extraction_id,
                    run_id,
                ),
            )
            for value in values:
                date = value.get("date") or {}
                self.db.execute(
                    """
                    INSERT INTO version_values(
                        posting_version_id, field_key, source_label, ordinal,
                        value_text, value_html, field_state, origin, known_label,
                        normalized_json, date_parse_state, source_precision,
                        source_tz_text, source_machine_value, parsed_utc,
                        parsed_local_date)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        version_id,
                        value["field_key"],
                        value["source_label"],
                        value["ordinal"],
                        value["value_text"],
                        value["value_html"],
                        value["field_state"],
                        value["origin"],
                        1 if value["known_label"] else 0,
                        _json(value.get("normalized")),
                        date.get("parse_state"),
                        date.get("precision"),
                        date.get("tz_text"),
                        date.get("machine_value"),
                        date.get("parsed_utc"),
                        date.get("parsed_local_date"),
                    ),
                )
            for position, link in enumerate(extraction.links, start=1):
                self.db.execute(
                    """
                    INSERT OR IGNORE INTO resource_links(
                        posting_version_id, extraction_id, parent_kind, url_raw,
                        url_resolved, link_text, rel, position, classification,
                        collection_decision, exclusion_reason, first_seen_at_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        version_id,
                        extraction_id,
                        link["parent_kind"],
                        link["url_raw"],
                        link["url_resolved"],
                        link["link_text"],
                        link["rel"],
                        link.get("position", position),
                        link["classification"],
                        link["collection_decision"],
                        link["exclusion_reason"],
                        observed_at_utc,
                    ),
                )
        return version_id, True

    # -------------------------------------------------------------- resources

    def record_resource_observation(self, **fields: Any) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.db.write():
            return self.db.insert(
                f"INSERT INTO resource_observations({cols}) VALUES ({marks})",
                tuple(fields.values()),
            )

    def associate_resource(
        self, resource_observation_id: int, observation_id: int, resource_link_id: int | None
    ) -> None:
        with self.db.write():
            self.db.execute(
                "INSERT OR IGNORE INTO resource_associations("
                "resource_observation_id, observation_id, resource_link_id, created_at_utc) "
                "VALUES (?,?,?,?)",
                (resource_observation_id, observation_id, resource_link_id, utc_str()),
            )

    # ------------------------------------------------------------ work queue

    def enqueue(
        self,
        *,
        run_id: int,
        kind: str,
        work_key: str,
        payload: dict[str, Any] | None = None,
        priority: int = 100,
        max_attempts: int = 3,
    ) -> None:
        with self.db.write():
            self.db.execute(
                """
                INSERT INTO work_queue(
                    run_id, kind, work_key, payload_json, priority, state,
                    attempts, max_attempts, updated_at_utc)
                VALUES (?,?,?,?,?,'pending',0,?,?)
                ON CONFLICT(run_id, kind, work_key) DO UPDATE SET
                    priority = MIN(work_queue.priority, excluded.priority),
                    payload_json = excluded.payload_json,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (run_id, kind, work_key, _json(payload), priority, max_attempts, utc_str()),
            )

    def claim_next(self, run_id: int, kind: str, claim_token: str) -> dict[str, Any] | None:
        with self.db.write():
            row = self.db.one(
                "SELECT * FROM work_queue WHERE run_id = ? AND kind = ? AND state = 'pending' "
                "ORDER BY priority, work_id LIMIT 1",
                (run_id, kind),
            )
            if row is None:
                return None
            self.db.execute(
                "UPDATE work_queue SET state = 'in_progress', attempts = attempts + 1, "
                "claim_token = ?, claimed_at_utc = ?, updated_at_utc = ? WHERE work_id = ?",
                (claim_token, utc_str(), utc_str(), int(row["work_id"])),
            )
            row["attempts"] = int(row["attempts"]) + 1
            return row

    def complete_work(self, work_id: int, state: str, error: str | None = None) -> None:
        with self.db.write():
            self.db.execute(
                "UPDATE work_queue SET state = ?, last_error = ?, updated_at_utc = ?, "
                "claim_token = NULL WHERE work_id = ?",
                (state, error, utc_str(), work_id),
            )

    def requeue_or_fail(self, row: dict[str, Any], error: str) -> str:
        work_id = int(row["work_id"])
        if int(row["attempts"]) >= int(row["max_attempts"]):
            self.complete_work(work_id, "failed", error)
            return "failed"
        with self.db.write():
            self.db.execute(
                "UPDATE work_queue SET state = 'pending', last_error = ?, updated_at_utc = ?, "
                "claim_token = NULL WHERE work_id = ?",
                (error, utc_str(), work_id),
            )
        return "pending"

    def reclaim_orphans(self, run_id: int) -> int:
        """Return work left ``in_progress`` by a dead process to ``pending``."""
        with self.db.write():
            self.db.execute(
                "UPDATE work_queue SET state = 'pending', claim_token = NULL, "
                "updated_at_utc = ?, last_error = 'reclaimed after interrupted run' "
                "WHERE run_id = ? AND state = 'in_progress'",
                (utc_str(), run_id),
            )
            return int(self.db.conn.changes())

    def queue_summary(self, run_id: int) -> dict[str, int]:
        rows = self.db.query(
            "SELECT state, COUNT(*) AS n FROM work_queue WHERE run_id = ? GROUP BY state",
            (run_id,),
        )
        return {str(r["state"]): int(r["n"]) for r in rows}

    # ----------------------------------------------------------- coverage

    def record_gap(self, **fields: Any) -> int:
        if "evidence" in fields:
            fields["evidence_json"] = _json(fields.pop("evidence"))
        fields.setdefault("detected_at_utc", utc_str())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.db.write():
            return self.db.insert(
                f"INSERT INTO coverage_gaps({cols}) VALUES ({marks})", tuple(fields.values())
            )

    # ------------------------------------------------------- recheck policy

    def upsert_recheck(
        self,
        *,
        posting_id: int,
        tier: str,
        consecutive_terminal: int,
        last_checked_at_utc: str | None,
        next_due_at_utc: str | None,
        reason: str | None,
    ) -> None:
        with self.db.write():
            self.db.execute(
                """
                INSERT INTO recheck_policy(
                    posting_id, tier, consecutive_terminal_observations,
                    last_checked_at_utc, next_due_at_utc, reason, updated_at_utc)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(posting_id) DO UPDATE SET
                    tier = excluded.tier,
                    consecutive_terminal_observations =
                        excluded.consecutive_terminal_observations,
                    last_checked_at_utc = excluded.last_checked_at_utc,
                    next_due_at_utc = excluded.next_due_at_utc,
                    reason = excluded.reason,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    posting_id,
                    tier,
                    consecutive_terminal,
                    last_checked_at_utc,
                    next_due_at_utc,
                    reason,
                    utc_str(),
                ),
            )

    def recheck_row(self, posting_id: int) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM recheck_policy WHERE posting_id = ?", (posting_id,))

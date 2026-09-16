"""The health contract.

``rowanjobs status --json`` and the cached ``runtime/health.json`` share this
structure. It is versioned (:data:`HEALTH_SCHEMA_VERSION`) and is the contract a
future ControlPanel adapter would read -- see docs/OPERATIONS.md. ControlPanel
itself is not modified by RowanJobs.

Collection health, archive integrity, local backup health and off-host
protection are reported **separately**, because they fail independently and
collapsing them would hide exactly the failure an operator needs to see.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import (
    CONTRACT_VERSION,
    EVENT_RULES_VERSION,
    HEALTH_SCHEMA_VERSION,
    PARSER_VERSION,
    QUALIFICATION_RULES_VERSION,
    TEXT_CONTRACT_VERSION,
    __version__,
)
from ..db import Database
from ..db.migrations import SCHEMA_VERSION, current_version
from ..timeutil import duration_str, local_str, utc_str
from .atomic import write_json
from .backup import BackupManager
from .notify import Notifier
from .schedule import timer_status

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config


def _row(db: Database, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    return db.one(sql, params)


def build_health(cfg: Config, db: Database, *, include_timer: bool = True) -> dict[str, Any]:
    layout = cfg.layout
    backups = BackupManager(cfg)

    last_run = _row(
        db,
        "SELECT * FROM v_run_health ORDER BY started_at_utc DESC, run_id DESC LIMIT 1",
    )
    last_success = _row(
        db,
        "SELECT * FROM v_run_health WHERE outcome IN ('success','partial') "
        "ORDER BY started_at_utc DESC LIMIT 1",
    )
    last_qualified = _row(
        db,
        "SELECT scan_id, run_id, scan_role, started_at_utc, ended_at_utc, "
        "unique_ids_seen, entries_seen, duplicate_occurrences, source_reported_total, "
        "scheduled_slot_local_date FROM v_qualified_scans "
        "ORDER BY ended_at_utc DESC LIMIT 1",
    )
    last_capture = _row(
        db,
        "SELECT MAX(observed_at_utc) AS at_utc, COUNT(*) AS n FROM posting_observations "
        "WHERE availability_state = 'content_captured'",
    )
    open_run = _row(db, "SELECT * FROM v_run_health WHERE ended_at_utc IS NULL LIMIT 1")

    counts = {
        "postings_total": int(db.scalar("SELECT COUNT(*) FROM postings") or 0),
        "postings_baseline": int(
            db.scalar("SELECT COUNT(*) FROM postings WHERE discovery_basis='baseline'") or 0
        ),
        "postings_observed_new": int(
            db.scalar("SELECT COUNT(*) FROM postings WHERE discovery_basis='observed-new'") or 0
        ),
        "posting_versions": int(db.scalar("SELECT COUNT(*) FROM posting_versions") or 0),
        "observations": int(db.scalar("SELECT COUNT(*) FROM posting_observations") or 0),
        "artifacts": int(db.scalar("SELECT COUNT(*) FROM artifacts") or 0),
        "fetches": int(db.scalar("SELECT COUNT(*) FROM fetches") or 0),
        "resource_links": int(db.scalar("SELECT COUNT(*) FROM resource_links") or 0),
        "resource_observations": int(db.scalar("SELECT COUNT(*) FROM resource_observations") or 0),
        "listing_scans": int(db.scalar("SELECT COUNT(*) FROM listing_scans") or 0),
        "qualified_scans": int(db.scalar("SELECT COUNT(*) FROM v_qualified_scans") or 0),
        "runs": int(db.scalar("SELECT COUNT(*) FROM collection_runs") or 0),
    }

    final_count = int(last_qualified["unique_ids_seen"]) if last_qualified else None
    union_encountered = None
    if last_run and last_run.get("run_id") is not None:
        raw = db.scalar(
            "SELECT counts_json FROM collection_runs WHERE run_id = ?",
            (int(last_run["run_id"]),),
        )
        if raw:
            try:
                union_encountered = json.loads(str(raw)).get("union_encountered")
            except ValueError:
                union_encountered = None

    failures = {
        "retrieval": int(
            db.scalar("SELECT COUNT(*) FROM fetches WHERE response_state != 'complete'") or 0
        ),
        "access_control_responses": int(
            db.scalar("SELECT COUNT(*) FROM fetches WHERE access_control_signal IS NOT NULL") or 0
        ),
        "extraction_failed": int(
            db.scalar("SELECT COUNT(*) FROM extractions WHERE status = 'failed'") or 0
        ),
        "extraction_partial": int(
            db.scalar("SELECT COUNT(*) FROM extractions WHERE status = 'partial'") or 0
        ),
        "resource_exceptions": int(
            db.scalar("SELECT COUNT(*) FROM resource_observations WHERE outcome != 'captured'") or 0
        ),
        "identity_mismatch": int(
            db.scalar("SELECT COUNT(*) FROM posting_observations WHERE identity_state = 'mismatch'")
            or 0
        ),
    }

    gaps = db.query(
        "SELECT kind, COUNT(*) AS n, MAX(detected_at_utc) AS latest FROM coverage_gaps "
        "WHERE resolved_at_utc IS NULL GROUP BY kind ORDER BY n DESC"
    )

    archive_bytes = db.page_bytes()
    compression = db.one(
        "SELECT COALESCE(SUM(byte_length),0) AS raw, COALESCE(SUM(compressed_bytes),0) "
        "AS comp FROM artifacts"
    )
    raw_bytes = int(compression["raw"]) if compression else 0
    comp_bytes = int(compression["comp"]) if compression else 0

    disk = shutil.disk_usage(layout.data_root)

    collection_state = _collection_state(last_run, open_run, last_qualified)
    payload: dict[str, Any] = {
        "health_schema_version": HEALTH_SCHEMA_VERSION,
        "application": "rowanjobs",
        "app_version": __version__,
        "generated_at_utc": utc_str(),
        "generated_at_local": local_str(utc_str()),
        "operational_timezone": "America/New_York",
        "versions": {
            "schema": current_version(db),
            "schema_expected": SCHEMA_VERSION,
            "parser": PARSER_VERSION,
            "comparison_contract": CONTRACT_VERSION,
            "text_contract": TEXT_CONTRACT_VERSION,
            "qualification_rules": QUALIFICATION_RULES_VERSION,
            "event_rules": EVENT_RULES_VERSION,
        },
        "collection": {
            "state": collection_state,
            "last_attempt": _run_brief(last_run),
            "last_completed": _run_brief(last_success),
            "last_qualified_discovery": {
                "scan_id": int(last_qualified["scan_id"]),
                "run_id": int(last_qualified["run_id"]),
                "role": str(last_qualified["scan_role"]),
                "ended_at_utc": str(last_qualified["ended_at_utc"]),
                "ended_at_local": local_str(str(last_qualified["ended_at_utc"])),
                "slot_local_date": last_qualified["scheduled_slot_local_date"],
                "final_qualified_listing_count": int(last_qualified["unique_ids_seen"]),
                "source_occurrences_seen": int(last_qualified["entries_seen"]),
                "duplicate_occurrences": int(last_qualified["duplicate_occurrences"]),
                "source_reported_total": last_qualified["source_reported_total"],
            }
            if last_qualified
            else None,
            "last_reconciled_content_capture": {
                "at_utc": last_capture["at_utc"] if last_capture else None,
                "at_local": local_str(last_capture["at_utc"])
                if last_capture and last_capture["at_utc"]
                else None,
                "total_captured_observations": int(last_capture["n"]) if last_capture else 0,
            },
            "run_in_progress": _run_brief(open_run),
            "final_qualified_listing_count": final_count,
            "union_encountered_last_run": union_encountered,
            "counts": counts,
            "failures": failures,
            "coverage_gaps": [
                {"kind": str(g["kind"]), "count": int(g["n"]), "latest_utc": str(g["latest"])}
                for g in gaps
            ],
        },
        "archive": {
            "state": "VERIFIED" if not failures["extraction_failed"] else "DEGRADED",
            "database_path": str(layout.db_path),
            "database_bytes": archive_bytes,
            "journal_mode": db.journal_mode,
            "wal_deviation": db.wal_deviation,
            "sqlite_runtime": db.runtime.as_dict(),
            "payload_bytes_uncompressed": raw_bytes,
            "payload_bytes_compressed": comp_bytes,
            "compression_ratio": round(raw_bytes / comp_bytes, 2) if comp_bytes else None,
            "artifact_deduplication": {
                "artifacts": counts["artifacts"],
                "fetches_with_payload": int(
                    db.scalar("SELECT COUNT(*) FROM fetches WHERE artifact_id IS NOT NULL") or 0
                ),
            },
            "disk": {
                "path": str(layout.data_root),
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
                "free_pct": round(100 * disk.free / disk.total, 1) if disk.total else None,
            },
        },
        "backup": backups.status(db),
        "notifications": Notifier(cfg.notify).status(),
        "schedule": timer_status(cfg) if include_timer else None,
        "paths": {
            "data_root": str(layout.data_root),
            "database": str(layout.db_path),
            "backups": str(layout.backups_dir),
            "logs": str(layout.logs_dir),
            "runtime": str(layout.runtime_dir),
            "health": str(layout.health_path),
            "config": str(cfg.config_path) if cfg.config_path else None,
        },
        "notes": [
            "Host-down detection requires an external observer. A local timer cannot "
            "report while entropy is unavailable.",
            "Collection health, archive integrity, local backup health and off-host "
            "protection are independent; read them separately.",
        ],
    }
    return payload


def _run_brief(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "run_id": int(row["run_id"]),
        "run_kind": str(row["run_kind"]),
        "attempt_no": int(row["attempt_no"]),
        "parent_run_id": row["parent_run_id"],
        "slot_local_date": row["scheduled_slot_local_date"],
        "is_baseline": bool(row["is_baseline"]),
        "started_at_utc": str(row["started_at_utc"]),
        "started_at_local": local_str(str(row["started_at_utc"])),
        "ended_at_utc": row["ended_at_utc"],
        "ended_at_local": local_str(row["ended_at_utc"]) if row["ended_at_utc"] else None,
        "duration": duration_str(str(row["started_at_utc"]), row["ended_at_utc"]),
        "outcome": row["outcome"],
        "outcome_detail": row["outcome_detail"],
        "qualified_scans": int(row["qualified_scans"]),
        "fetches": int(row["fetches"]),
        "failed_fetches": int(row["failed_fetches"]),
        "access_control_responses": int(row["access_control_responses"]),
        "observations": int(row["observations"]),
        "captured": int(row["captured"]),
        "resources": int(row["resources"]),
        "coverage_gaps": int(row["coverage_gaps"]),
    }


def _collection_state(
    last_run: dict[str, Any] | None,
    open_run: dict[str, Any] | None,
    last_qualified: dict[str, Any] | None,
) -> str:
    if open_run is not None:
        return "RUNNING"
    if last_run is None:
        return "NEVER_RUN"
    outcome = str(last_run["outcome"] or "")
    if outcome == "success" and last_qualified is not None:
        return "HEALTHY"
    if outcome in ("partial", "lock_contention"):
        return "DEGRADED"
    if outcome in ("failed", "aborted"):
        return "FAILED"
    return "UNKNOWN"


def write_health(cfg: Config, db: Database) -> Path:
    payload = build_health(cfg, db)
    path = cfg.layout.health_path
    write_json(path, payload)
    return path


def exit_code_for(payload: dict[str, Any]) -> int:
    """Map health to a shell exit code.

    0  healthy
    1  collection failed
    2  collection degraded (partial coverage, unresolved reconciliation)
    3  collection fine but protection degraded (no verified/off-host backup)
    """
    state = payload["collection"]["state"]
    if state == "FAILED":
        return 1
    if state in ("DEGRADED", "UNKNOWN"):
        return 2
    backup = payload["backup"]
    if (
        backup["local"]["state"] in ("FAILED", "DEGRADED", "UNPROTECTED")
        or backup["offhost"]["state"] == "FAILED"
    ):
        return 3
    return 0

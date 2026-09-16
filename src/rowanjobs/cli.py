"""Command line interface.

Exit codes are meaningful, because a timer and an operator both read them:

    0  success / ordinary no-op
    1  collection failed
    2  collection degraded: partial coverage or unresolved reconciliation
    3  collection fine, protection degraded (backup problem)
    4  another collector held the lock; nothing was attempted
    5  usage or configuration error

Reporting commands open the archive **read-only**, so running a report can never
migrate or mutate production data.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, load_config
from .db import Database, open_db, open_readonly
from .db.migrations import SCHEMA_VERSION, apply_migrations, current_version
from .timeutil import duration_str, local_str, utc_str

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DEGRADED = 2
EXIT_PROTECTION = 3
EXIT_LOCKED = 4
EXIT_USAGE = 5


# --------------------------------------------------------------------- output


def emit(payload: Any, as_json: bool) -> None:
    if as_json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")


def line(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _config_from(args: argparse.Namespace) -> Config:
    cfg = load_config(Path(args.config) if args.config else None)
    if args.data_root:
        cfg.data_root = Path(args.data_root).expanduser()
    if args.db:
        cfg.db_path = Path(args.db).expanduser()
    return cfg


def _open_ro(cfg: Config) -> Database:
    return open_readonly(cfg.layout.db_path)


def _git_revision(root: Path) -> tuple[str | None, bool | None]:
    try:
        rev = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if rev.returncode != 0:
            return None, None
        status = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "status", "--porcelain"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return rev.stdout.strip(), bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None, None


# -------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    from .ops.doctor import run_doctor

    cfg = _config_from(args)
    report = run_doctor(cfg)
    if args.json:
        emit(report, True)
    else:
        line(f"RowanJobs {report['app_version']}  ({report['checked_at_local']})")
        line()
        for check in report["checks"]:
            mark = {True: "ok  ", False: "FAIL", None: "--  "}[check["ok"]]
            line(f"  [{mark}] {check['name']:24} {check['detail']}")
        line()
        line("doctor: OK" if report["ok"] else f"doctor: FAILED ({', '.join(report['failures'])})")
    return EXIT_OK if report["ok"] else EXIT_FAILED


# ------------------------------------------------------------------- migrate


def cmd_migrate(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    cfg.layout.ensure()
    db = open_db(cfg.layout.db_path, migrate=False)
    try:
        before = current_version(db)
        applied = apply_migrations(db)
        after = current_version(db)
    finally:
        db.close()
    payload = {
        "database": str(cfg.layout.db_path),
        "version_before": before,
        "version_after": after,
        "expected": SCHEMA_VERSION,
        "applied": applied,
    }
    if args.json:
        emit(payload, True)
    else:
        line(f"schema v{before} -> v{after} (applied: {applied or 'none'})")
    return EXIT_OK


# ------------------------------------------------------------------- collect


def cmd_collect(args: argparse.Namespace) -> int:
    from .collect.runner import Collector
    from .ops.backup import BackupManager
    from .ops.health import build_health, write_health
    from .ops.notify import Notifier

    cfg = _config_from(args)
    cfg.layout.ensure()
    revision, _dirty = _git_revision(Path(__file__).resolve().parents[2])
    collector = Collector(cfg, app_revision=revision)
    result = collector.run(
        run_kind=args.kind,
        parent_run_id=args.parent_run,
        attempt_no=args.attempt,
        max_details=args.max_details,
        skip_verification=args.no_verification,
    )

    backup_payload: dict[str, Any] | None = None
    health: dict[str, Any] = {}
    if result.run_id is not None:
        db = open_db(cfg.layout.db_path)
        try:
            # Back up after any ingestion that added evidence, including a
            # partial run. A backup failure never undoes ingestion.
            if not args.no_backup and result.outcome in ("success", "partial"):
                manager = BackupManager(cfg)
                backup = manager.create(db, kind="daily")
                backup_payload = {
                    "state": backup.state,
                    "path": str(backup.path) if backup.path else None,
                    "detail": backup.detail,
                    "pruned": backup.pruned,
                    "offhost": backup.offhost,
                }
                if manager.restore_check_due() and backup.path:
                    with tempfile.TemporaryDirectory(prefix="rowanjobs-restore-") as tmp:
                        check = manager.restore_check(backup.path, Path(tmp))
                    from .ops.atomic import write_json

                    write_json(
                        cfg.layout.restore_check_path,
                        {
                            "checked_at_utc": utc_str(),
                            "checked_at_local": local_str(utc_str()),
                            "source": check.source,
                            "ok": check.ok,
                            "detail": check.detail,
                            "checks": check.checks,
                        },
                    )
            health = build_health(cfg, db)
            write_health(cfg, db)
        finally:
            db.close()

    notifier = Notifier(cfg.notify)
    notification = notifier.maybe_notify(
        result.outcome,
        {
            "application": "rowanjobs",
            "host": os.environ.get("HOSTNAME") or "",
            "outcome": result.outcome,
            "detail": result.detail,
            "run_id": result.run_id,
            "counts": result.counts,
            "at_local": local_str(utc_str()),
        },
    )

    payload = {
        "run_id": result.run_id,
        "run_uuid": result.run_uuid,
        "outcome": result.outcome,
        "detail": result.detail,
        "started_at_utc": result.started_at_utc,
        "started_at_local": local_str(result.started_at_utc),
        "ended_at_utc": result.ended_at_utc,
        "ended_at_local": local_str(result.ended_at_utc),
        "duration": duration_str(result.started_at_utc, result.ended_at_utc),
        "counts": result.counts,
        "coverage": result.coverage,
        "errors": result.errors,
        "backup": backup_payload,
        "notification": {"state": notification.state, "detail": notification.detail},
    }
    if args.json:
        emit(payload, True)
    else:
        line(f"run {result.run_id} ({result.run_uuid}) -> {result.outcome}")
        if result.detail:
            line(f"  {result.detail}")
        line(
            f"  started {payload['started_at_local']}  ended {payload['ended_at_local']}"
            f"  ({payload['duration']})"
        )
        counts = result.counts
        line(f"  final qualified listing count : {counts.get('final_qualified_listing_count')}")
        line(f"  union encountered             : {counts.get('union_encountered')}")
        line(
            f"  detail captured / attempted   : {counts.get('detail_captured')}"
            f" / {counts.get('detail_attempted')}"
        )
        line(f"  new content versions          : {counts.get('versions_created')}")
        if backup_payload:
            line(
                f"  backup                        : {backup_payload['state']}"
                f" ({backup_payload['detail']})"
            )
        line(f"  notification                  : {notification.state}")
        for err in result.errors:
            line(f"  ! {err.get('kind')}: {str(err.get('detail'))[:160]}")

    if result.outcome == "lock_contention":
        return EXIT_LOCKED
    if result.outcome == "failed":
        return EXIT_FAILED
    if result.outcome == "partial":
        return EXIT_DEGRADED
    if backup_payload and backup_payload["state"] not in ("VERIFIED", "UNCONFIGURED"):
        return EXIT_PROTECTION
    if health:
        from .ops.health import exit_code_for

        return exit_code_for(health)
    return EXIT_OK


# -------------------------------------------------------------------- status


def cmd_status(args: argparse.Namespace) -> int:
    from .ops.health import build_health, exit_code_for

    cfg = _config_from(args)
    try:
        db = _open_ro(cfg)
    except FileNotFoundError as exc:
        line(str(exc))
        return EXIT_USAGE
    try:
        payload = build_health(cfg, db, include_timer=not args.no_timer)
    finally:
        db.close()

    if args.json:
        emit(payload, True)
        return exit_code_for(payload)

    c = payload["collection"]
    line(f"RowanJobs {payload['app_version']}   {payload['generated_at_local']}")
    line(f"collection      : {c['state']}")
    last = c["last_attempt"]
    if last:
        line(
            f"  last attempt  : run {last['run_id']} {last['run_kind']}"
            f" -> {last['outcome']} at {last['ended_at_local'] or 'in progress'}"
            f" ({last['duration'] or '-'})"
        )
        if last["outcome_detail"]:
            line(f"                  {last['outcome_detail']}")
    q = c["last_qualified_discovery"]
    if q:
        line(
            f"  last qualified discovery : {q['ended_at_local']}"
            f"  final count {q['final_qualified_listing_count']}"
            f"  (source said {q['source_reported_total']},"
            f" {q['duplicate_occurrences']} duplicate occurrences ignored)"
        )
    else:
        line("  last qualified discovery : none")
    cap = c["last_reconciled_content_capture"]
    line(
        f"  last content capture     : {cap['at_local'] or 'never'}"
        f"  ({cap['total_captured_observations']} captured observations)"
    )
    if c["run_in_progress"]:
        line(f"  RUN IN PROGRESS : run {c['run_in_progress']['run_id']}")
    counts = c["counts"]
    line(
        f"  postings {counts['postings_total']}"
        f" (baseline {counts['postings_baseline']},"
        f" observed-new {counts['postings_observed_new']})"
        f"  versions {counts['posting_versions']}"
        f"  observations {counts['observations']}"
    )
    f = c["failures"]
    line(
        f"  failures: retrieval {f['retrieval']}, access-control {f['access_control_responses']},"
        f" extraction {f['extraction_failed']}/{f['extraction_partial']},"
        f" resources {f['resource_exceptions']}, identity {f['identity_mismatch']}"
    )
    if c["coverage_gaps"]:
        line("  coverage gaps:")
        for gap in c["coverage_gaps"]:
            line(f"    - {gap['kind']}: {gap['count']} (latest {local_str(gap['latest_utc'])})")
    a = payload["archive"]
    line(
        f"archive         : {a['state']}  {a['database_bytes'] / 1024 / 1024:.1f} MiB"
        f"  journal={a['journal_mode']}  sqlite={a['sqlite_runtime']['sqlite_version']}"
    )
    line(
        f"  payloads      : {a['payload_bytes_uncompressed'] / 1024 / 1024:.1f} MiB raw ->"
        f" {a['payload_bytes_compressed'] / 1024 / 1024:.1f} MiB stored"
        f" (x{a['compression_ratio']})  {a['artifact_deduplication']['artifacts']} artifacts"
        f" for {a['artifact_deduplication']['fetches_with_payload']} payload fetches"
    )
    line(
        f"  disk          : {a['disk']['free_bytes'] / 1024**3:.1f} GiB free"
        f" ({a['disk']['free_pct']}%)"
    )
    b = payload["backup"]
    line(
        f"backup (local)  : {b['local']['state']}  {b['local']['detail']}"
        f"  [{b['local']['snapshot_count']} snapshots]"
    )
    line(f"backup (offhost): {b['offhost']['state']}  {b['offhost']['detail']}")
    rv = b.get("restore_verification")
    if rv:
        line(
            f"restore check   : {'OK' if rv.get('ok') else 'FAILED'}"
            f" at {rv.get('checked_at_local')}"
        )
    line(
        f"notifications   : {payload['notifications']['state']}"
        f"  {payload['notifications']['detail']}"
    )
    s = payload.get("schedule")
    if s:
        line(
            f"schedule        : {s['configured_expression']}"
            f"  next {s.get('next_collection_local') or 'not scheduled'}"
        )
        line(
            f"  lingering     : {'yes' if s['lingering']['enabled'] else 'NO'}"
            f"  ({s['lingering']['detail']})"
        )
    for note in payload["notes"]:
        line(f"note: {note}")
    return exit_code_for(payload)


# ---------------------------------------------------------------------- runs


def cmd_runs(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    db = _open_ro(cfg)
    try:
        rows = db.query(
            "SELECT * FROM v_run_health ORDER BY started_at_utc DESC LIMIT ?",
            (args.limit,),
        )
    finally:
        db.close()
    if args.json:
        emit({"runs": rows, "generated_at_utc": utc_str()}, True)
        return EXIT_OK
    if not rows:
        line("no runs recorded")
        return EXIT_OK
    line(
        f"{'run':>5} {'kind':<12} {'slot':<11} {'outcome':<16} {'start (ET)':<20}"
        f" {'dur':>8} {'qual':>4} {'obs':>5} {'cap':>5}"
    )
    for r in rows:
        line(
            f"{r['run_id']:>5} {r['run_kind']!s:<12} "
            f"{r['scheduled_slot_local_date'] or '-'!s:<11} "
            f"{r['outcome'] or 'running'!s:<16} "
            f"{local_str(str(r['started_at_utc']))!s:<20} "
            f"{duration_str(str(r['started_at_utc']), r['ended_at_utc']) or '-'!s:>8} "
            f"{r['qualified_scans']:>4} {r['observations']:>5} {r['captured']:>5}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------- show


def _posting_row(db: Database, job_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM v_posting_current WHERE external_job_id = ?", (str(job_id),))


def cmd_show(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    db = _open_ro(cfg)
    try:
        current = _posting_row(db, args.job_id)
        if current is None:
            line(f"no posting with source job id {args.job_id!r}")
            return EXIT_USAGE
        version_id = current["current_version_id"]
        version = (
            db.one("SELECT * FROM posting_versions WHERE posting_version_id = ?", (version_id,))
            if version_id
            else None
        )
        values = (
            db.query(
                "SELECT * FROM version_values WHERE posting_version_id = ? ORDER BY rowid",
                (version_id,),
            )
            if version_id
            else []
        )
        urls = db.query(
            "SELECT url, role, first_seen_at_utc, last_seen_at_utc, seen_count "
            "FROM posting_urls WHERE posting_id = ? ORDER BY role",
            (int(current["posting_id"]),),
        )
        links = (
            db.query(
                "SELECT url_resolved, link_text, classification, collection_decision, "
                "exclusion_reason FROM resource_links WHERE posting_version_id = ? "
                "ORDER BY position",
                (version_id,),
            )
            if version_id
            else []
        )
        absence = None
        if args.json:
            from .collect.events import repeatedly_unlisted
            from .collect.repo import Repository

            absence = repeatedly_unlisted(Repository(db), int(current["posting_id"]))
    finally:
        db.close()

    if args.json:
        emit(
            {
                "generated_at_utc": utc_str(),
                "current": current,
                "version": version,
                "values": values,
                "urls": urls,
                "resource_links": links,
                "absence_evidence": absence,
            },
            True,
        )
        return EXIT_OK

    line(f"job {current['external_job_id']}  {current['current_title'] or '(no title)'}")
    line(
        f"  discovery basis   : {current['discovery_basis']}"
        f"  (first seen {local_str(str(current['first_discovered_at_utc']))})"
    )
    line(f"  last listed (qualified scan) : {local_str(current['last_listed_at_utc']) or 'never'}")
    line(
        f"  last seen (any scan)         : "
        f"{local_str(current['last_seen_any_scan_at_utc']) or 'never'}"
    )
    line(f"  last captured     : {local_str(current['last_captured_at_utc']) or 'never'}")
    line(
        f"  last check        : {local_str(current['last_check_at_utc'])}"
        f"  -> {current['last_availability_state']} / {current['last_identity_state']}"
    )
    line(f"  content freshness : {current['content_freshness']}")
    line(f"  versions {current['version_count']}   observations {current['observation_count']}")
    if values:
        line("  source fields:")
        for v in values:
            extra = ""
            if v["source_precision"]:
                extra = (
                    f"  [precision {v['source_precision']}"
                    f", tz {v['source_tz_text'] or '-'}"
                    f", machine {v['source_machine_value'] or '-'}]"
                )
            known = "" if v["known_label"] else "  (label not previously known)"
            line(
                f"    {v['source_label'] or v['field_key']!s:<26}"
                f" {str(v['value_text'] or '')[:70]}"
                f"  <{v['field_state']}>{extra}{known}"
            )
    line("  urls:")
    for u in urls:
        line(f"    [{u['role']}] {u['url']}  (seen {u['seen_count']}x)")
    if links:
        line("  links in description:")
        for link in links:
            reason = f"  reason: {link['exclusion_reason']}" if link["exclusion_reason"] else ""
            line(
                f"    [{link['classification']}/{link['collection_decision']}]"
                f" {str(link['url_resolved'])[:90]}{reason}"
            )
    if version and version["description_text"]:
        text = str(version["description_text"])
        line(f"  description ({len(text)} chars, html kind {version['description_html_kind']}):")
        shown = text if args.full else text[:1200]
        for row in shown.splitlines():
            line(f"    {row}")
        if not args.full and len(text) > 1200:
            line(f"    ... ({len(text) - 1200} more characters; use --full)")
    return EXIT_OK


# ------------------------------------------------------------------- history


def cmd_history(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    db = _open_ro(cfg)
    try:
        rows = db.query(
            "SELECT * FROM v_posting_history WHERE external_job_id = ? "
            "ORDER BY observed_at_utc DESC LIMIT ?",
            (str(args.job_id), args.limit),
        )
        events = db.query(
            "SELECT e.event_kind, e.rules_version, e.interval_start_utc, "
            "e.interval_end_utc, e.slot_local_date, e.evidence_json "
            "FROM presence_events e JOIN postings p ON p.posting_id = e.posting_id "
            "WHERE p.external_job_id = ? ORDER BY e.interval_end_utc DESC LIMIT ?",
            (str(args.job_id), args.limit),
        )
        absence = None
        row = db.one(
            "SELECT posting_id FROM postings WHERE external_job_id = ?", (str(args.job_id),)
        )
        if row:
            from .collect.events import repeatedly_unlisted
            from .collect.repo import Repository

            absence = repeatedly_unlisted(Repository(db), int(row["posting_id"]))
    finally:
        db.close()

    if not rows and not events:
        line(f"no history for job {args.job_id!r}")
        return EXIT_USAGE
    if args.json:
        emit(
            {
                "job_id": args.job_id,
                "observations": rows,
                "events": events,
                "absence_evidence": absence,
                "generated_at_utc": utc_str(),
            },
            True,
        )
        return EXIT_OK

    line(f"history for job {args.job_id}")
    line(f"{'observed (ET)':<21} {'availability':<26} {'identity':<15} {'why':<19} content")
    for r in rows:
        fp = str(r["content_fingerprint"] or "")[:12] or "-"
        line(
            f"{local_str(str(r['observed_at_utc']))!s:<21}"
            f" {r['availability_state']!s:<26}"
            f" {r['identity_state']!s:<15}"
            f" {r['checked_because']!s:<19} {fp}"
        )
    if events:
        line()
        line("derived events (rules version shown; all rebuildable from evidence):")
        for e in events:
            span = (
                f"{local_str(e['interval_start_utc']) or '?'}"
                f" .. {local_str(str(e['interval_end_utc']))}"
            )
            line(f"  {e['event_kind']!s:<18} {span}   [rules {e['rules_version']}]")
    if absence:
        line()
        line(
            f"absence evidence: {absence['count']} distinct qualifying daily observation(s)"
            f"; meets the two-day reporting rule: {absence['meets_two_day_rule']}"
        )
        if absence["intervening_uncovered_dates"]:
            line(
                f"  calendar dates with no qualified scan (gaps, not absences): "
                f"{', '.join(absence['intervening_uncovered_dates'])}"
            )
    return EXIT_OK


# ---------------------------------------------------------------------- diff


def cmd_diff(args: argparse.Namespace) -> int:
    cfg = _config_from(args)
    db = _open_ro(cfg)
    try:
        versions = db.query(
            "SELECT v.* FROM posting_versions v JOIN postings p ON p.posting_id = v.posting_id "
            "WHERE p.external_job_id = ? AND v.contract_version = "
            "(SELECT MAX(contract_version) FROM posting_versions v2 "
            " WHERE v2.posting_id = v.posting_id) "
            "ORDER BY v.first_seen_at_utc",
            (str(args.job_id),),
        )
    finally:
        db.close()
    if len(versions) < 2:
        payload = {
            "job_id": args.job_id,
            "versions": len(versions),
            "changed": False,
            "detail": "fewer than two distinct content versions have been observed",
        }
        if args.json:
            emit(payload, True)
        else:
            line(payload["detail"])
        return EXIT_OK

    def pick(spec: str | None, default: int) -> dict[str, Any]:
        if spec is None:
            return versions[default]
        for v in versions:
            if str(v["posting_version_id"]) == str(spec):
                return v
        raise KeyError(spec)

    try:
        older = pick(args.from_version, -2)
        newer = pick(args.to_version, -1)
    except KeyError as exc:
        line(f"no version {exc.args[0]!r} for job {args.job_id}")
        return EXIT_USAGE

    a_text = str(older["description_text"] or "").splitlines()
    b_text = str(newer["description_text"] or "").splitlines()
    diff = list(
        difflib.unified_diff(
            a_text,
            b_text,
            fromfile=f"v{older['posting_version_id']} @ {older['first_seen_at_utc']}",
            tofile=f"v{newer['posting_version_id']} @ {newer['first_seen_at_utc']}",
            lineterm="",
        )
    )
    changed = {
        "title": older["title"] != newer["title"],
        "description_text": older["description_text_fingerprint"]
        != newer["description_text_fingerprint"],
        "description_html": older["description_html_fingerprint"]
        != newer["description_html_fingerprint"],
        "metadata": older["metadata_fingerprint"] != newer["metadata_fingerprint"],
    }
    if args.json:
        emit(
            {
                "job_id": args.job_id,
                "from": {
                    "posting_version_id": older["posting_version_id"],
                    "first_seen_at_utc": older["first_seen_at_utc"],
                },
                "to": {
                    "posting_version_id": newer["posting_version_id"],
                    "first_seen_at_utc": newer["first_seen_at_utc"],
                },
                "contract_version": newer["contract_version"],
                "changed": changed,
                "unified_diff": diff,
                "note": "versions are only compared within one comparison contract; a "
                "parser upgrade cannot appear here as a source edit",
            },
            True,
        )
        return EXIT_OK
    line(
        f"job {args.job_id}: version {older['posting_version_id']}"
        f" ({local_str(str(older['first_seen_at_utc']))})"
        f" -> {newer['posting_version_id']} ({local_str(str(newer['first_seen_at_utc']))})"
    )
    line("changed: " + ", ".join(k for k, v in changed.items() if v) or "changed: (nothing)")
    line()
    for row in diff:
        line(row)
    return EXIT_OK


# ----------------------------------------------------------------- reprocess


def cmd_reprocess(args: argparse.Namespace) -> int:
    from .reprocess import reprocess_details, reprocess_listings

    cfg = _config_from(args)
    cfg.layout.ensure()
    db = open_db(cfg.layout.db_path)
    try:
        reports = []
        if args.what in ("details", "all"):
            reports.append(
                reprocess_details(
                    db, job_id=args.job_id, limit=args.limit, relink=not args.no_relink
                ).as_dict()
            )
        if args.what in ("listings", "all"):
            reports.append(reprocess_listings(db, limit=args.limit).as_dict())
    finally:
        db.close()
    if args.json:
        emit({"reports": reports}, True)
    else:
        for report in reports:
            line(
                f"{report['parser']}: {report['artifacts_considered']} artifacts, "
                f"{report['extractions_created']} new extractions, "
                f"{report['extractions_reused']} reused, "
                f"{report['versions_created']} new content versions"
            )
            line(f"  {report['note']}")
            for failure in report["failures"][:10]:
                line(f"  ! {failure}")
    return EXIT_OK


# -------------------------------------------------------------------- backup


def cmd_backup(args: argparse.Namespace) -> int:
    from .ops.backup import BackupManager

    cfg = _config_from(args)
    cfg.layout.ensure()
    db = open_db(cfg.layout.db_path)
    try:
        manager = BackupManager(cfg)
        result = manager.create(db, kind=args.kind)
        status = manager.status(db)
    finally:
        db.close()
    payload = {
        "state": result.state,
        "path": str(result.path) if result.path else None,
        "detail": result.detail,
        "manifest": result.manifest,
        "pruned": result.pruned,
        "status": status,
    }
    if args.json:
        emit(payload, True)
    else:
        line(f"backup: {result.state}  {result.detail}")
        if result.path:
            line(f"  file     : {result.path}")
        if result.manifest:
            line(f"  sha256   : {result.manifest['sha256']}")
            line(f"  bytes    : {result.manifest['file_bytes']}")
        if result.pruned:
            line(f"  pruned   : {', '.join(result.pruned)}")
        line(f"  off-host : {status['offhost']['state']} — {status['offhost']['detail']}")
    return EXIT_OK if result.ok else EXIT_PROTECTION


# -------------------------------------------------------------------- verify


def cmd_verify(args: argparse.Namespace) -> int:
    from .archive import ArchiveStore
    from .ops.backup import BackupManager

    cfg = _config_from(args)
    results: dict[str, Any] = {"checked_at_utc": utc_str()}
    db = _open_ro(cfg)
    try:
        results["integrity_check"] = db.integrity_check()
        results["foreign_key_check"] = len(db.foreign_key_check())
        store = ArchiveStore(db)
        results["payloads"] = store.verify_all(limit=args.limit)
        manager = BackupManager(cfg)
        latest = manager.latest(db)
    finally:
        db.close()

    restore: dict[str, Any] | None = None
    if args.restore and latest:
        with tempfile.TemporaryDirectory(prefix="rowanjobs-restore-") as tmp:
            check = BackupManager(cfg).restore_check(Path(str(latest["path"])), Path(tmp))
        restore = {
            "source": check.source,
            "ok": check.ok,
            "detail": check.detail,
            "checks": check.checks,
        }
        from .ops.atomic import write_json

        write_json(
            cfg.layout.restore_check_path,
            {"checked_at_utc": utc_str(), "checked_at_local": local_str(utc_str()), **restore},
        )
    elif args.restore:
        restore = {"ok": False, "detail": "no verified backup exists to restore"}
    results["restore"] = restore

    ok = (
        results["integrity_check"] == ["ok"]
        and results["foreign_key_check"] == 0
        and results["payloads"]["ok"]
        and (restore is None or restore["ok"])
    )
    results["ok"] = ok
    if args.json:
        emit(results, True)
    else:
        line(f"integrity_check     : {', '.join(results['integrity_check'][:3])}")
        line(f"foreign_key_check   : {results['foreign_key_check']} violations")
        line(
            f"payload hashes      : {results['payloads']['checked']} checked, "
            f"{len(results['payloads']['failures'])} failed"
        )
        if restore:
            line(
                f"restore verification: {'OK' if restore['ok'] else 'FAILED'} — {restore['detail']}"
            )
            for check in restore.get("checks", []):
                mark = "ok  " if check["passed"] else "FAIL"
                line(f"  [{mark}] {check['name']:30} {check['detail']}")
        line("verify: OK" if ok else "verify: FAILED")
    return EXIT_OK if ok else EXIT_FAILED


# ------------------------------------------------------------------- restore


def cmd_restore(args: argparse.Namespace) -> int:
    from .ops.backup import BackupManager

    cfg = _config_from(args)
    manager = BackupManager(cfg)
    source = Path(args.source) if args.source else None
    if source is None:
        db = _open_ro(cfg)
        try:
            latest = manager.latest(db)
        finally:
            db.close()
        if latest is None:
            line("no verified backup available")
            return EXIT_USAGE
        source = Path(str(latest["path"]))
    destination = Path(args.destination)
    if destination.resolve() == cfg.layout.db_path.resolve():
        line("refusing to restore over the live archive; choose another destination")
        return EXIT_USAGE
    try:
        path = manager.restore_to(source, destination)
    except (OSError, FileExistsError) as exc:
        line(str(exc))
        return EXIT_USAGE
    check = manager.restore_check(source, destination.parent)
    payload = {
        "restored_to": str(path),
        "source": str(source),
        "verification": {"ok": check.ok, "detail": check.detail, "checks": check.checks},
    }
    if args.json:
        emit(payload, True)
    else:
        line(f"restored {source} -> {path}")
        for c in check.checks:
            line(f"  [{'ok  ' if c['passed'] else 'FAIL'}] {c['name']:30} {c['detail']}")
    return EXIT_OK if check.ok else EXIT_FAILED


# -------------------------------------------------------------------- export


def cmd_export(args: argparse.Namespace) -> int:
    from .export import DATASETS, export_to_path, write_csv_export, write_json_export

    cfg = _config_from(args)
    if args.dataset not in DATASETS:
        line(f"unknown dataset {args.dataset!r}; choose from {', '.join(DATASETS)}")
        return EXIT_USAGE
    db = _open_ro(cfg)
    try:
        if args.output:
            path, count = export_to_path(
                db,
                args.dataset,
                Path(args.output),
                args.format,
                job_id=args.job_id,
                limit=args.limit,
            )
            line(f"wrote {count} rows to {path}")
        else:
            writer = write_csv_export if args.format == "csv" else write_json_export
            count = writer(db, args.dataset, sys.stdout, job_id=args.job_id, limit=args.limit)
    finally:
        db.close()
    return EXIT_OK


# -------------------------------------------------------------------- deploy


def cmd_record_deployment(args: argparse.Namespace) -> int:
    from .ops.atomic import write_json
    from .ops.doctor import deployment_manifest

    cfg = _config_from(args)
    cfg.layout.ensure()
    root = Path(__file__).resolve().parents[2]
    revision, dirty = _git_revision(root)
    manifest = deployment_manifest(cfg, revision=revision, dirty=dirty)
    write_json(cfg.layout.deployment_manifest, manifest)
    db = open_db(cfg.layout.db_path)
    try:
        with db.write():
            db.execute(
                """
                INSERT INTO deployments(
                    recorded_at_utc, app_version, app_revision, git_dirty,
                    python_version, sqlite_runtime_json, host, os_user,
                    install_path, data_root, units_json, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    manifest["recorded_at_utc"],
                    manifest["app_version"],
                    manifest["app_revision"],
                    1 if manifest["git_dirty"] else 0,
                    manifest["python"],
                    json.dumps(manifest["sqlite_runtime"]),
                    manifest["host"],
                    manifest["os_user"],
                    manifest["install_path"],
                    manifest["data_root"],
                    json.dumps(manifest["units"]),
                    args.note,
                ),
            )
    finally:
        db.close()
    if args.json:
        emit(manifest, True)
    else:
        line(
            f"recorded deployment {manifest['app_version']} @ {manifest['app_revision']}"
            f" -> {cfg.layout.deployment_manifest}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rowanjobs",
        description="Longitudinal archive of Rowan University job advertisements.",
    )
    parser.add_argument("--version", action="version", version=f"rowanjobs {__version__}")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--data-root", help="override the data root (tests, restores)")
    parser.add_argument("--db", help="override the database path (tests, restores)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check the environment and configuration").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("migrate", help="apply outstanding schema migrations").set_defaults(
        func=cmd_migrate
    )

    collect = sub.add_parser("collect", help="run a collection")
    collect.add_argument(
        "--kind", choices=("daily", "retry", "manual", "verification"), default="manual"
    )
    collect.add_argument("--parent-run", type=int, help="parent run for a retry")
    collect.add_argument("--attempt", type=int, default=1)
    collect.add_argument("--max-details", type=int, help="bound detail retrievals")
    collect.add_argument(
        "--no-verification", action="store_true", help="skip the second listing traversal"
    )
    collect.add_argument("--no-backup", action="store_true")
    collect.set_defaults(func=cmd_collect)

    status = sub.add_parser("status", help="operational state")
    status.add_argument("--no-timer", action="store_true", help="skip systemd inspection")
    status.set_defaults(func=cmd_status)

    runs = sub.add_parser("runs", help="recent collection runs")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(func=cmd_runs)

    show = sub.add_parser("show", help="inspect one archived advertisement")
    show.add_argument("job_id")
    show.add_argument("--full", action="store_true", help="print the whole description")
    show.set_defaults(func=cmd_show)

    history = sub.add_parser("history", help="observation history for one advertisement")
    history.add_argument("job_id")
    history.add_argument("--limit", type=int, default=50)
    history.set_defaults(func=cmd_history)

    diff = sub.add_parser("diff", help="compare two archived content versions")
    diff.add_argument("job_id")
    diff.add_argument("--from-version", dest="from_version")
    diff.add_argument("--to-version", dest="to_version")
    diff.set_defaults(func=cmd_diff)

    reprocess = sub.add_parser(
        "reprocess", help="re-parse archived payloads; makes no network requests"
    )
    reprocess.add_argument("what", choices=("details", "listings", "all"), default="all", nargs="?")
    reprocess.add_argument("--job-id")
    reprocess.add_argument("--limit", type=int)
    reprocess.add_argument(
        "--no-relink",
        action="store_true",
        help="create extractions but leave observations pointing at the old ones",
    )
    reprocess.set_defaults(func=cmd_reprocess)

    backup = sub.add_parser("backup", help="create a verified snapshot")
    backup.add_argument(
        "--kind", default="manual", choices=("daily", "weekly", "monthly", "manual", "predeploy")
    )
    backup.set_defaults(func=cmd_backup)

    verify = sub.add_parser("verify", help="verify archive integrity and payload hashes")
    verify.add_argument("--limit", type=int, help="only check this many payloads")
    verify.add_argument(
        "--restore",
        action="store_true",
        help="also restore the latest backup into a temporary directory",
    )
    verify.set_defaults(func=cmd_verify)

    restore = sub.add_parser("restore", help="restore a snapshot to a new location")
    restore.add_argument("destination")
    restore.add_argument("--source", help="snapshot to restore (default: latest verified)")
    restore.set_defaults(func=cmd_restore)

    export = sub.add_parser("export", help="export a dataset")
    export.add_argument("dataset")
    export.add_argument("--format", choices=("json", "csv"), default="json")
    export.add_argument("--output", help="write to a file instead of stdout")
    export.add_argument("--job-id")
    export.add_argument("--limit", type=int)
    export.set_defaults(func=cmd_export)

    deploy = sub.add_parser("record-deployment", help="write the runtime deployment manifest")
    deploy.add_argument("--note")
    deploy.set_defaults(func=cmd_record_deployment)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        line("interrupted")
        return EXIT_FAILED
    except FileNotFoundError as exc:
        line(str(exc))
        return EXIT_USAGE
    except ValueError as exc:
        line(f"configuration or usage error: {exc}")
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

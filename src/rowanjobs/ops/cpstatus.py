"""The ``controlpanel.status.v1`` document behind ``rowanjobs health``.

ControlPanel *observes* RowanJobs. It never builds, activates or mutates it,
and RowanJobs never imports ControlPanel or depends on it being reachable --
collection works identically whether the console is running or not.

This is a projection of :func:`rowanjobs.ops.health.build_health`, which stays
the archive's own contract (``status --json``, ``runtime/health.json``). Nothing
is recomputed here and nothing is re-observed: every timestamp is the moment the
evidence was actually recorded, so asking for health can never make stale
evidence look fresh.

Two scoring decisions worth stating, because both were deliberate:

* **A missing off-host backup does not lower required health.** It is reported
  as its own component and named plainly, but the operator has decided local
  snapshots are sufficient, and a permanent yellow light teaches people to
  ignore lights.
* **Mail trouble is degraded, never failed.** A report that did not arrive is a
  real problem and is shown as one, but the harvest it describes still
  happened; letting it drive the overall verdict would make a healthy archive
  look broken.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import __version__
from ..db import Database
from ..timeutil import utc_str
from . import failures as failures_mod
from .health import build_health

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

SCHEMA_VERSION = "controlpanel.status.v1"
PROJECT = "rowanjobs"

HEALTHY, DEGRADED, FAILED, RUNNING, UNKNOWN = (
    "healthy",
    "degraded",
    "failed",
    "running",
    "unknown",
)

#: RowanJobs' own collection vocabulary -> ControlPanel's.
_COLLECTION_HEALTH = {
    "HEALTHY": HEALTHY,
    "RUNNING": RUNNING,
    "DEGRADED": DEGRADED,
    "STALE": FAILED,
    "FAILED": FAILED,
    "NEVER_RUN": UNKNOWN,
    "UNKNOWN": UNKNOWN,
}

_RANK = {HEALTHY: 0, RUNNING: 0, UNKNOWN: 1, DEGRADED: 2, FAILED: 3}


def _evidence(pairs: list[tuple[str, Any]]) -> list[dict[str, str]]:
    return [
        {"label": label, "value": "-" if value is None else str(value)} for label, value in pairs
    ]


def _component(
    component_id: str,
    name: str,
    health: str,
    summary: str,
    *,
    observed_at: str | None = None,
    evidence: list[tuple[str, Any]] | None = None,
    actions: list[str] | None = None,
) -> dict[str, Any]:
    component: dict[str, Any] = {
        "id": component_id,
        "name": name,
        "health": health,
        "summary": summary,
        "actions": actions or [],
        "evidence": _evidence(evidence or []),
    }
    if observed_at:
        # The time the evidence was recorded, never the time it was asked for.
        component["observed_at"] = observed_at
    return component


def _worst(components: list[dict[str, Any]], *, exclude: set[str]) -> str:
    considered = [c for c in components if c["id"] not in exclude]
    if not considered:
        return UNKNOWN
    return max((c["health"] for c in considered), key=lambda h: _RANK.get(h, 1))


def build_status(cfg: Config, db: Database, *, include_timer: bool = True) -> dict[str, Any]:
    """Project the health contract into one ControlPanel status document."""
    health = build_health(cfg, db, include_timer=include_timer)
    collection = health["collection"]
    counts = collection["counts"]
    archive = health["archive"]
    backup = health["backup"]
    notifications = health["notifications"]
    schedule = health.get("schedule") or {}
    startup = failures_mod.assess(cfg.layout, db)

    scheduled = collection.get("last_scheduled_attempt") or {}
    discovery = collection.get("last_qualified_discovery") or {}
    in_progress = collection.get("run_in_progress")
    window = collection.get("coverage_window") or {}
    failure_counts = collection.get("failures") or {}

    components: list[dict[str, Any]] = []

    # ---------------------------------------------------------- collection
    collection_health = _COLLECTION_HEALTH.get(str(collection["state"]), UNKNOWN)
    if in_progress:
        summary = (
            f"Run {in_progress['run_id']} ({in_progress['run_kind']}) is collecting, "
            f"started {in_progress['started_at_local']}"
        )
    elif scheduled:
        summary = (
            f"Run {scheduled['run_id']} {scheduled['run_kind']} for "
            f"{scheduled['slot_local_date']} finished {scheduled['outcome']} "
            f"at {scheduled['ended_at_local'] or 'an unrecorded time'}"
        )
    else:
        summary = "No scheduled collection has been recorded"
    components.append(
        _component(
            "daily-collection",
            "Daily collection",
            collection_health,
            summary,
            observed_at=scheduled.get("ended_at_utc") or scheduled.get("started_at_utc"),
            evidence=[
                ("Last scheduled run", scheduled.get("run_id")),
                ("Outcome", scheduled.get("outcome")),
                ("Started", scheduled.get("started_at_local")),
                ("Duration", scheduled.get("duration")),
                ("Qualified scans", scheduled.get("qualified_scans")),
                ("Days since a qualified discovery", window.get("days_since_last_qualified")),
            ],
            actions=["run", "refresh"],
        )
    )

    # ------------------------------------------------------ startup failure
    # The gap that made 2026-09-18 invisible: the unit died before it could
    # write a run row, so nothing in the archive knew anything had happened.
    if startup["unresolved_slots"]:
        slots = ", ".join(startup["unresolved_slots"])
        startup_health, startup_summary = (
            FAILED,
            f"{len(startup['unresolved'])} activation(s) failed before recording a run "
            f"(slots {slots}) and no collection has covered those slots since",
        )
    elif startup["recorded"]:
        startup_health, startup_summary = (
            HEALTHY,
            f"{startup['recorded']} past startup failure(s) recorded, all answered by a "
            "later collection for the same slot",
        )
    else:
        startup_health, startup_summary = HEALTHY, "No unit has failed before recording a run"
    latest_unresolved = startup["unresolved"][-1] if startup["unresolved"] else {}
    components.append(
        _component(
            "startup-integrity",
            "Scheduled activation",
            startup_health,
            startup_summary,
            observed_at=latest_unresolved.get("failed_at_utc"),
            evidence=[
                ("Unresolved slots", ", ".join(startup["unresolved_slots"]) or "none"),
                ("Records kept", startup["recorded"]),
                ("Last failing unit", latest_unresolved.get("unit")),
                ("Reason", latest_unresolved.get("config_error")),
            ],
        )
    )

    # ------------------------------------------------------------- listing
    listed = collection.get("final_qualified_listing_count")
    components.append(
        _component(
            "listing-coverage",
            "Listing traversal",
            HEALTHY if discovery else UNKNOWN,
            (
                f"{listed} advertisements on the last qualified traversal"
                if discovery
                else "No qualified traversal has been recorded"
            ),
            observed_at=discovery.get("ended_at_utc"),
            evidence=[
                ("Advertised now", listed),
                ("Source reported", discovery.get("source_reported_total")),
                ("Occurrences seen", discovery.get("source_occurrences_seen")),
                ("Duplicates ignored", discovery.get("duplicate_occurrences")),
                ("Union encountered", collection.get("union_encountered_last_run")),
            ],
        )
    )

    # -------------------------------------------------------- descriptions
    tracked = counts.get("postings_total")
    captured = _captured_now(db)
    missing = None if tracked is None else tracked - captured["ever_captured"]
    description_health = HEALTHY if not missing else DEGRADED
    components.append(
        _component(
            "description-coverage",
            "Description coverage",
            description_health,
            (
                f"{captured['ever_captured']} of {tracked} advertisements ever tracked have an "
                f"archived description; {captured['checked']} were re-read on the last check"
            ),
            observed_at=collection.get("last_reconciled_content_capture", {}).get("at_utc"),
            evidence=[
                ("Advertisements tracked (lifetime)", tracked),
                ("With an archived description", captured["ever_captured"]),
                ("Never captured", missing),
                ("Freshly checked", captured["checked"]),
                ("Carried forward (delisted)", captured["carried_forward"]),
                ("Carried forward, uncertain", captured["carried_forward_uncertain"]),
            ],
        )
    )

    # ---------------------------------------------------------- exceptions
    unresolved_gaps = collection.get("coverage_gaps") or []
    exception_total = (
        int(failure_counts.get("retrieval") or 0)
        + int(failure_counts.get("identity_mismatch") or 0)
        + len(unresolved_gaps)
    )
    components.append(
        _component(
            "capture-exceptions",
            "Capture exceptions",
            DEGRADED if exception_total else HEALTHY,
            (
                f"{exception_total} unresolved capture problem(s)"
                if exception_total
                else "No unresolved retrieval, identity or coverage problems"
            ),
            evidence=[
                ("Retrieval failures", failure_counts.get("retrieval")),
                (
                    "Access-control challenges (lifetime)",
                    failure_counts.get("access_control_responses"),
                ),
                ("Identity mismatches", failure_counts.get("identity_mismatch")),
                ("Unresolved coverage gaps", len(unresolved_gaps)),
                (
                    "Extraction failed / partial",
                    f"{failure_counts.get('extraction_failed')} / "
                    f"{failure_counts.get('extraction_partial')}",
                ),
            ],
        )
    )

    # ---------------------------------------------------- linked documents
    # Scored on the most recent run, not on all time. A document that 404'd
    # once in September is a fact worth keeping, not a reason to show a yellow
    # light for the rest of the archive's life -- a console that is always
    # slightly unwell is one nobody reads.
    resource_exceptions = int(failure_counts.get("resource_exceptions") or 0)
    current_resource = _current_resource_exceptions(db)
    components.append(
        _component(
            "linked-documents",
            "Linked documents",
            DEGRADED if current_resource["unresolved"] else HEALTHY,
            (
                f"{current_resource['unresolved']} linked job document(s) could not be "
                f"checked on the last collection"
                if current_resource["unresolved"]
                else (
                    f"{current_resource['captured']} retrieved on the last collection"
                    + (
                        f"; {current_resource['gone_at_source']} the source answered "
                        "not-found, recorded as evidence"
                        if current_resource["gone_at_source"]
                        else ""
                    )
                )
            ),
            observed_at=current_resource["observed_at"],
            evidence=[
                ("Retrieved on the last run", current_resource["captured"]),
                ("Not found at source (recorded)", current_resource["gone_at_source"]),
                ("Could not be checked", current_resource["unresolved"]),
                ("Exceptions (lifetime)", resource_exceptions),
                ("Links classified (lifetime)", counts.get("resource_links")),
            ],
        )
    )

    # -------------------------------------------------------- notifications
    notify_state = str(notifications.get("state"))
    notify_health = {
        "VERIFIED": HEALTHY,
        "CONFIGURED": HEALTHY,
        "UNCONFIGURED": UNKNOWN,
        "DEGRADED": DEGRADED,
        # A bounced report is a real problem, but it is not a failed harvest.
        "FAILED": DEGRADED,
    }.get(notify_state, UNKNOWN)
    last_accepted = notifications.get("last_accepted") or {}
    components.append(
        _component(
            "run-reporting",
            "Run reporting",
            notify_health,
            str(notifications.get("detail")),
            observed_at=last_accepted.get("accepted_at_utc"),
            evidence=[
                ("State", notify_state),
                ("Recipient", notifications.get("recipient")),
                ("Last accepted", last_accepted.get("accepted_at_local")),
                ("For run", last_accepted.get("run_id")),
                ("Message id", last_accepted.get("message_id")),
                ("Awaiting retry", notifications.get("failed")),
                ("Runs with no report", len(notifications.get("unreported_runs") or [])),
            ],
            actions=["refresh"],
        )
    )

    # ------------------------------------------------------------- archive
    components.append(
        _component(
            "archive-integrity",
            "Archive integrity",
            HEALTHY if str(archive.get("state")) == "VERIFIED" else DEGRADED,
            (
                f"{archive.get('journal_mode')} journal on "
                f"SQLite {archive['sqlite_runtime'].get('sqlite_version')}, "
                f"{_mib(archive.get('database_bytes'))} archive"
            ),
            evidence=[
                ("Journal mode", archive.get("journal_mode")),
                ("SQLite", archive["sqlite_runtime"].get("sqlite_version")),
                ("WAL safe", archive["sqlite_runtime"].get("wal_safe")),
                ("Database", _mib(archive.get("database_bytes"))),
                ("Payloads stored", _mib(archive.get("payload_bytes_compressed"))),
                ("Compression", archive.get("compression_ratio")),
                ("Disk free", f"{archive['disk'].get('free_pct')}%"),
            ],
        )
    )

    # -------------------------------------------------------------- backup
    local_backup = backup.get("local") or {}
    components.append(
        _component(
            "local-backup",
            "Local snapshots",
            HEALTHY if str(local_backup.get("state")) == "VERIFIED" else DEGRADED,
            str(local_backup.get("detail")),
            evidence=[
                ("State", local_backup.get("state")),
                ("Snapshots", local_backup.get("snapshot_count")),
                ("Total", _mib(local_backup.get("total_bytes"))),
            ],
        )
    )
    offhost = backup.get("offhost") or {}
    components.append(
        _component(
            # Excluded from the overall verdict by operator decision: local
            # snapshots are considered sufficient here. Reported, not scored.
            "offhost-backup",
            "Off-host protection",
            UNKNOWN if str(offhost.get("state")) == "UNCONFIGURED" else HEALTHY,
            str(offhost.get("detail")),
            evidence=[("State", offhost.get("state")), ("Target", offhost.get("target"))],
        )
    )

    # ------------------------------------------------------------ schedule
    units = schedule.get("units") or {}
    daily_unit = units.get("rowanjobs.timer") or {}
    retry_unit = units.get("rowanjobs-retry.timer") or {}
    notify_unit = units.get("rowanjobs-notify.timer") or {}
    if schedule:
        installed = bool(daily_unit.get("installed"))
        components.append(
            _component(
                "timers",
                "Unattended schedule",
                HEALTHY if installed and daily_unit.get("active_state") == "active" else DEGRADED,
                (
                    f"Next daily collection {daily_unit.get('next_activation_local')}"
                    if installed
                    else "The daily timer is not installed"
                ),
                evidence=[
                    ("Next daily", daily_unit.get("next_activation_local")),
                    ("Next recovery window", retry_unit.get("next_activation_local")),
                    ("Next mail retry", notify_unit.get("next_activation_local")),
                    ("Lingering", (schedule.get("lingering") or {}).get("enabled")),
                ],
                actions=["enable", "disable", "refresh"],
            )
        )

    overall_health = _worst(components, exclude={"offhost-backup"})
    degraded_names = [
        c["name"]
        for c in components
        if c["id"] != "offhost-backup" and c["health"] in (DEGRADED, FAILED)
    ]
    overall_summary = _summarise(
        overall_health, collection, scheduled, listed, startup, degraded_names
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "project": PROJECT,
        "display_name": "RowanJobs",
        "observed_at": health["generated_at_utc"],
        "overall": {"health": overall_health, "summary": overall_summary},
        "components": components,
        "metrics": {
            "advertisements_listed": listed,
            "advertisements_tracked_lifetime": tracked,
            "descriptions_archived_lifetime": captured["ever_captured"],
            "descriptions_checked_last_run": captured["checked"],
            "content_versions_lifetime": counts.get("posting_versions"),
            "observations_lifetime": counts.get("observations"),
            "runs_lifetime": counts.get("runs"),
            "artifacts_lifetime": counts.get("artifacts"),
            "covered_days": window.get("covered_days"),
            "missed_days": window.get("missed_days"),
            "days_since_last_qualified": window.get("days_since_last_qualified"),
            "resource_exceptions_lifetime": resource_exceptions,
            "unresolved_startup_failures": len(startup["unresolved"]),
            "reports_awaiting_retry": notifications.get("failed"),
            "archive_bytes": archive.get("database_bytes"),
        },
        "recent_runs": _recent_runs(db),
        "errors": [],
        "schedule": {
            "next_daily_local": daily_unit.get("next_activation_local"),
            "next_retry_local": retry_unit.get("next_activation_local"),
            "next_notify_local": notify_unit.get("next_activation_local"),
            "timezone": health["operational_timezone"],
        },
        "versions": {
            "app": __version__,
            "health_schema": health["health_schema_version"],
            **health["versions"],
        },
        "notes": [
            "Collection runs independently of ControlPanel; this document is observed, "
            "never required.",
            "A host that is down cannot report that it is down. Absence of a daily "
            "report is itself the signal.",
            "Off-host protection is reported but deliberately excluded from the overall "
            "verdict, by operator decision.",
        ],
    }


def _mib(value: Any) -> str | None:
    if not isinstance(value, int | float):
        return None
    return f"{value / (1024 * 1024):.1f} MiB"


def _captured_now(db: Database) -> dict[str, int]:
    """Description coverage, read from the archive rather than recomputed."""
    freshness = {
        str(r["content_freshness"]): int(r["n"])
        for r in db.query(
            "SELECT content_freshness, COUNT(*) AS n FROM v_posting_current "
            "GROUP BY content_freshness"
        )
    }
    ever = int(
        db.scalar(
            "SELECT COUNT(DISTINCT posting_id) FROM posting_observations "
            "WHERE availability_state = 'content_captured'"
        )
        or 0
    )
    return {
        "ever_captured": ever,
        "checked": freshness.get("checked", 0),
        "carried_forward": freshness.get("carried-forward", 0),
        "carried_forward_uncertain": freshness.get("carried-forward-uncertain", 0),
        "never_captured": freshness.get("never-captured", 0),
    }


#: A definite answer from the source that the document is not there. This is a
#: successful observation, not a failure to observe -- the same distinction the
#: archive draws between a terminal availability state and an uncertain one.
_GONE_AT_SOURCE = ("HTTP 404", "HTTP 410")


def _current_resource_exceptions(db: Database) -> dict[str, Any]:
    """Linked-document outcomes for the most recent run that looked at any.

    Splits "the source told us it is not there" from "we could not find out".
    An employer who links a PDF that does not exist is a fact this archive
    records faithfully on every run; it is not an operator task, and scoring it
    as a fault would leave the console permanently yellow over something nobody
    can fix.
    """
    row = db.one(
        "SELECT run_id FROM resource_observations ORDER BY resource_observation_id DESC LIMIT 1"
    )
    if row is None:
        return {
            "unresolved": 0,
            "gone_at_source": 0,
            "captured": 0,
            "run_id": None,
            "observed_at": None,
        }
    run_id = int(row["run_id"])
    rows = db.query(
        "SELECT outcome, outcome_detail FROM resource_observations WHERE run_id = ?",
        (run_id,),
    )
    captured = gone = unresolved = 0
    for record in rows:
        outcome = str(record["outcome"])
        detail = str(record["outcome_detail"] or "")
        if outcome in ("captured", "not_modified"):
            captured += 1
        elif any(detail.startswith(marker) for marker in _GONE_AT_SOURCE):
            gone += 1
        else:
            unresolved += 1
    ended = db.one("SELECT ended_at_utc FROM collection_runs WHERE run_id = ?", (run_id,))
    return {
        "unresolved": unresolved,
        "gone_at_source": gone,
        "captured": captured,
        "run_id": run_id,
        "observed_at": ended["ended_at_utc"] if ended else None,
    }


def _recent_runs(db: Database, limit: int = 10) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT run_id, run_kind, outcome, started_at_utc, ended_at_utc, counts_json "
        "FROM collection_runs ORDER BY run_id DESC LIMIT ?",
        (limit,),
    )
    import json as _json

    runs: list[dict[str, Any]] = []
    for row in rows:
        try:
            counts = _json.loads(str(row["counts_json"] or "{}"))
        except ValueError:  # pragma: no cover - defensive
            counts = {}
        events = counts.get("events") or {}
        outcome = str(row["outcome"] or "")
        runs.append(
            {
                "started_at": row["started_at_utc"],
                "finished_at": row["ended_at_utc"],
                "success": outcome == "success" if outcome else None,
                "summary": (
                    f"{row['run_kind']} run {row['run_id']}: {outcome or 'in progress'}"
                    + (
                        f", {counts.get('final_qualified_listing_count')} advertisements"
                        if counts.get("final_qualified_listing_count") is not None
                        else ""
                    )
                ),
                "metrics": {
                    "advertisements": counts.get("final_qualified_listing_count"),
                    "descriptions_captured": counts.get("detail_captured"),
                    "descriptions_attempted": counts.get("detail_attempted"),
                    "versions_created": counts.get("versions_created"),
                    "newly_observed": events.get("first_observed"),
                    "no_longer_listed": events.get("absent_qualified"),
                    "content_changed": events.get("content_changed"),
                },
            }
        )
    return runs


def _summarise(
    health: str,
    collection: dict[str, Any],
    scheduled: dict[str, Any],
    listed: Any,
    startup: dict[str, Any],
    degraded_names: list[str],
) -> str:
    if startup["unresolved_slots"]:
        return (
            "A scheduled activation failed before recording a run for "
            f"{', '.join(startup['unresolved_slots'])}"
        )
    if collection.get("run_in_progress"):
        return "A collection is running now"
    state = str(collection["state"])
    if state == "HEALTHY":
        base = (
            f"{listed} advertisements archived by run {scheduled.get('run_id')} on "
            f"{scheduled.get('slot_local_date')}"
        )
        if health == DEGRADED and degraded_names:
            # Name what is actually wrong. Appending an unrelated line -- the
            # last successful report, say -- reads as if that were the fault.
            return f"{base}; {', '.join(degraded_names)} needs attention"
        return base
    if state == "STALE":
        window = collection.get("coverage_window") or {}
        return (
            f"No qualified collection for {window.get('days_since_last_qualified')} "
            "scheduled day(s)"
        )
    if state == "NEVER_RUN":
        return "No collection has run yet"
    return f"The last scheduled collection is {state.lower()}"


def write_status(cfg: Config, db: Database, *, include_timer: bool = True) -> dict[str, Any]:
    """Build the document and stamp it. Reads only; never writes the archive."""
    document = build_status(cfg, db, include_timer=include_timer)
    document.setdefault("observed_at", utc_str())
    return document

"""The per-run status report.

Built entirely from stored evidence for one ``run_id``, so a report can be
composed long after the run and still describe what actually happened. The
report never re-reads the website and never recomputes a fact it can look up.

Two things it is careful about:

* **A baseline is not news.** The first qualified collection discovers every
  advertisement at once; presenting 133 as "newly published" would be a lie
  about the employer's hiring. Newly-observed counts are only reported for runs
  that had a baseline to compare against.
* **A delayed report says so.** The collection time is the run's time; the send
  time is its own. A catch-up is labelled, never dressed up as punctual.
"""

from __future__ import annotations

import html as htmllib
import json
from dataclasses import dataclass
from typing import Any

from .. import __version__
from ..constants import TERMINAL_AVAILABILITY, UNCERTAIN_AVAILABILITY
from ..db import Database
from ..timeutil import duration_str, local_str, utc_str

OUTCOME_WORDS = {
    "success": "Completed",
    "partial": "Completed with exceptions",
    "failed": "Failed",
    "aborted": "Interrupted",
    "lock_contention": "Skipped (another collector was running)",
}

RUN_KIND_WORDS = {
    "daily": "Scheduled daily collection",
    "retry": "Same-day recovery attempt",
    "manual": "Manual collection",
    "verification": "Verification collection",
}


@dataclass(slots=True)
class RunReport:
    run_id: int
    subject: str
    text_body: str
    html_body: str
    outcome: str
    facts: dict[str, Any]


def _row(db: Database, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    return db.one(sql, params)


def _loads(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(str(value))
    except ValueError:  # pragma: no cover - defensive
        return None


def gather(db: Database, run_id: int, *, controlpanel_url: str | None = None) -> dict[str, Any]:
    """Everything the report states, read from the archive."""
    run = _row(db, "SELECT * FROM v_run_health WHERE run_id = ?", (run_id,))
    if run is None:
        raise KeyError(f"no run {run_id}")
    raw = _row(
        db,
        "SELECT counts_json, coverage_json, errors_json, run_uuid, app_revision, "
        "is_baseline, scheduled_slot_local_date FROM collection_runs WHERE run_id = ?",
        (run_id,),
    )
    assert raw is not None
    counts = _loads(raw["counts_json"]) or {}
    coverage = _loads(raw["coverage_json"]) or {}
    errors = _loads(raw["errors_json"]) or []

    scans = db.query(
        "SELECT s.scan_id, s.scan_role, s.pages_requested, s.entries_seen, "
        "s.unique_ids_seen, s.duplicate_occurrences, s.source_reported_total, "
        "s.termination_reason, COALESCE(a.qualified, 0) AS qualified, a.reason "
        "FROM listing_scans s LEFT JOIN listing_scan_assessments a ON a.scan_id = s.scan_id "
        "WHERE s.run_id = ? ORDER BY s.scan_ordinal",
        (run_id,),
    )
    resources = db.query(
        "SELECT outcome, outcome_detail, media_type, byte_length, url_resolved "
        "FROM resource_observations WHERE run_id = ? ORDER BY resource_observation_id",
        (run_id,),
    )
    gaps = db.query(
        "SELECT kind, detail, COUNT(*) AS n FROM coverage_gaps WHERE run_id = ? "
        "GROUP BY kind, detail",
        (run_id,),
    )
    failures = db.query(
        "SELECT expected_external_job_id, availability_state, availability_detail "
        "FROM posting_observations WHERE run_id = ? AND availability_state != 'content_captured' "
        "ORDER BY observation_id LIMIT 25",
        (run_id,),
    )
    # Why the run captured fewer descriptions than it queued. An advertisement
    # that disappeared between the traversal and the retrieval is not a failure
    # to retrieve it, and the report must not imply that it is.
    availability = db.query(
        "SELECT availability_state, COUNT(*) AS n FROM posting_observations "
        "WHERE run_id = ? AND availability_state != 'content_captured' "
        "GROUP BY availability_state",
        (run_id,),
    )
    no_longer_available = sum(
        int(r["n"]) for r in availability if str(r["availability_state"]) in TERMINAL_AVAILABILITY
    )
    uncertain_retrievals = sum(
        int(r["n"]) for r in availability if str(r["availability_state"]) in UNCERTAIN_AVAILABILITY
    )

    events = counts.get("events") or {}

    # Newly observed advertisements only mean something once a baseline exists.
    is_baseline = bool(raw["is_baseline"])
    newly_observed = None if is_baseline else events.get("first_observed")

    # Lifetime totals, clearly separate from this run's numbers.
    lifetime = {
        "postings": int(db.scalar("SELECT COUNT(*) FROM postings") or 0),
        "observations": int(db.scalar("SELECT COUNT(*) FROM posting_observations") or 0),
        "versions": int(db.scalar("SELECT COUNT(*) FROM posting_versions") or 0),
        "runs": int(db.scalar("SELECT COUNT(*) FROM collection_runs") or 0),
    }

    qualified = [s for s in scans if int(s["qualified"]) == 1]
    final_scan = qualified[-1] if qualified else None

    return {
        "run_id": run_id,
        "run_uuid": raw["run_uuid"],
        "run_kind": str(run["run_kind"]),
        "run_kind_label": RUN_KIND_WORDS.get(str(run["run_kind"]), str(run["run_kind"])),
        "attempt_no": int(run["attempt_no"]),
        "parent_run_id": run["parent_run_id"],
        "outcome": str(run["outcome"] or "unknown"),
        "outcome_label": OUTCOME_WORDS.get(str(run["outcome"]), str(run["outcome"])),
        "outcome_detail": run["outcome_detail"],
        "is_baseline": is_baseline,
        "collection_date": raw["scheduled_slot_local_date"],
        "started_at_utc": str(run["started_at_utc"]),
        "started_at_local": local_str(str(run["started_at_utc"])),
        "ended_at_utc": run["ended_at_utc"],
        "ended_at_local": local_str(run["ended_at_utc"]) if run["ended_at_utc"] else None,
        "duration": duration_str(str(run["started_at_utc"]), run["ended_at_utc"]),
        "listing_count": counts.get("final_qualified_listing_count"),
        "union_encountered": counts.get("union_encountered"),
        "source_reported_total": final_scan["source_reported_total"] if final_scan else None,
        "listing_complete": bool(coverage.get("absence_analysis_supported")),
        "discovery_qualified": coverage.get("discovery_qualified"),
        "verification_qualified": coverage.get("verification_qualified"),
        "set_comparison": coverage.get("set_comparison") or {},
        "detail_attempted": counts.get("detail_attempted"),
        "detail_captured": counts.get("detail_captured"),
        "detail_failed": counts.get("detail_failed"),
        "detail_uncertain": counts.get("detail_uncertain"),
        "versions_created": counts.get("versions_created"),
        "content_changed": events.get("content_changed"),
        "newly_observed": newly_observed,
        "no_longer_listed": events.get("absent_qualified"),
        "reappeared": events.get("reappeared"),
        "scans": scans,
        "resources": resources,
        "resource_exceptions": [r for r in resources if str(r["outcome"]) != "captured"],
        "coverage_gaps": gaps,
        "capture_failures": failures,
        "no_longer_available": no_longer_available,
        "uncertain_retrievals": uncertain_retrievals,
        "errors": errors,
        "budget": counts.get("budget") or {},
        "queue": counts.get("queue") or {},
        "lifetime": lifetime,
        "app_version": __version__,
        "app_revision": raw["app_revision"],
        "controlpanel_url": controlpanel_url,
    }


def _action_required(f: dict[str, Any]) -> str | None:
    """What a person actually has to do, or nothing."""
    if f["outcome"] == "failed":
        return (
            "The collection failed. Nothing was added for this date, and it will stay "
            "a coverage gap unless a recovery run succeeds today."
        )
    if f["outcome"] == "aborted":
        return "The run was interrupted. Evidence already collected was kept."
    problems: list[str] = []
    if not f["listing_complete"]:
        problems.append(
            "the listing traversal did not qualify, so nothing can be concluded "
            "about advertisements that appear to be missing"
        )
    if f["detail_failed"]:
        problems.append(f"{f['detail_failed']} advertisement page(s) could not be retrieved")
    if f["detail_uncertain"]:
        problems.append(f"{f['detail_uncertain']} retrieval(s) were inconclusive")
    if f["queue"].get("pending"):
        problems.append(f"{f['queue']['pending']} queued retrieval(s) were not attempted")
    if problems:
        return "; ".join(problems).capitalize() + "."
    return None


def _schedule_line(schedule: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not schedule:
        return None, None
    units = schedule.get("units") or {}
    daily = units.get("rowanjobs.timer") or {}
    retry = units.get("rowanjobs-retry.timer") or {}
    return daily.get("next_activation_local"), retry.get("next_activation_local")


def build(
    db: Database,
    run_id: int,
    *,
    delayed: bool = False,
    schedule: dict[str, Any] | None = None,
    controlpanel_url: str | None = None,
) -> RunReport:
    f = gather(db, run_id, controlpanel_url=controlpanel_url)
    next_daily, next_retry = _schedule_line(schedule)
    f["next_daily"] = next_daily
    f["next_retry"] = next_retry
    f["delayed"] = delayed
    action = _action_required(f)
    f["action_required"] = action

    marker = {
        "success": "OK",
        "partial": "EXCEPTIONS",
        "failed": "FAILED",
        "aborted": "INTERRUPTED",
        "lock_contention": "SKIPPED",
    }.get(f["outcome"], "?")
    count = f["listing_count"]
    subject = (
        f"RowanJobs {marker} — {f['collection_date']} — "
        f"{count if count is not None else 'no'} advertisements"
    )
    if delayed:
        subject += " (delayed report)"

    return RunReport(
        run_id=run_id,
        subject=subject,
        text_body=render_text(f),
        html_body=render_html(f),
        outcome=f["outcome"],
        facts=f,
    )


# ------------------------------------------------------------------ rendering


def _captured_line(f: dict[str, Any]) -> str:
    """Say why a shortfall happened, not just that there was one.

    "3 short" reads like three failures. Three advertisements that were taken
    down between the traversal and the retrieval are not failures, and the
    report says which kind it is looking at.
    """
    captured, attempted = f["detail_captured"], f["detail_attempted"]
    if captured is None or attempted is None:
        return "not recorded"
    if captured == attempted:
        return f"{captured} of {attempted} expected"
    reasons: list[str] = []
    if f["no_longer_available"]:
        reasons.append(f"{f['no_longer_available']} no longer published")
    if f["uncertain_retrievals"]:
        reasons.append(f"{f['uncertain_retrievals']} could not be checked")
    why = ", ".join(reasons) if reasons else "unaccounted for"
    return f"{captured} of {attempted} expected  ({attempted - captured} short: {why})"


def _listing_line(f: dict[str, Any]) -> str:
    if not f["scans"]:
        return "no listing traversal completed"
    parts = []
    for s in f["scans"]:
        state = "qualified" if int(s["qualified"]) else f"NOT qualified ({s['reason']})"
        parts.append(
            f"{s['scan_role']}: {s['pages_requested']} pages, "
            f"{s['unique_ids_seen']} advertisements, {state}"
        )
    return "; ".join(parts)


def render_text(f: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    if f["delayed"]:
        add("DELAYED REPORT — this describes an earlier collection, sent late.")
        add("")
    add(f"RowanJobs — {f['outcome_label']}")
    add(f"{f['run_kind_label']} for {f['collection_date']}")
    if f["is_baseline"]:
        add("This was the baseline collection: every advertisement was discovered at")
        add("once, so none of them are newly published.")
    add("")
    add(f"Started   : {f['started_at_local']}")
    add(f"Ended     : {f['ended_at_local'] or 'still running'}")
    add(f"Duration  : {f['duration'] or '-'}")
    add(f"Run       : {f['run_id']} ({f['run_uuid']})")
    if f["outcome_detail"]:
        add(f"Detail    : {f['outcome_detail']}")
    add("")
    add("LISTING")
    add(f"  Completeness : {_listing_line(f)}")
    add(
        f"  Advertised now: {f['listing_count'] if f['listing_count'] is not None else 'unknown'}"
        f" (source reported {f['source_reported_total']})"
    )
    add(f"  Union seen   : {f['union_encountered']}")
    sets = f["set_comparison"]
    if sets.get("performed"):
        add(f"  Two passes   : {'identical' if sets.get('sets_match') else 'DIFFERED'}")
    add("")
    add("DESCRIPTIONS")
    add(f"  Captured     : {_captured_line(f)}")
    add(f"  New content versions: {f['versions_created']}")
    if f["newly_observed"] is not None:
        add(f"  Newly observed advertisements: {f['newly_observed']}")
        add(f"  No longer listed             : {f['no_longer_listed']}")
        add(f"  Content changed              : {f['content_changed']}")
    else:
        add("  Change counts are not meaningful for a baseline collection.")
    add("")
    add("LINKED DOCUMENTS")
    if not f["resources"]:
        add("  No job-specific documents were linked from the advertisements.")
    else:
        captured = len(f["resources"]) - len(f["resource_exceptions"])
        add(f"  Retrieved    : {captured} of {len(f['resources'])}")
        for r in f["resource_exceptions"]:
            add(f"  EXCEPTION    : {r['outcome']} — {r['url_resolved']}")
            if r["outcome_detail"]:
                add(f"                 {r['outcome_detail']}")
    if f["capture_failures"]:
        add("")
        add("RETRIEVAL EXCEPTIONS")
        for r in f["capture_failures"]:
            add(
                f"  {r['expected_external_job_id']}: {r['availability_state']}"
                f" — {str(r['availability_detail'] or '')[:100]}"
            )
    if f["coverage_gaps"]:
        add("")
        add("COVERAGE GAPS")
        for g in f["coverage_gaps"]:
            add(f"  {g['kind']} x{g['n']}: {str(g['detail'])[:120]}")
    add("")
    add("ACTION REQUIRED")
    add(f"  {f['action_required'] or 'None.'}")
    add("")
    add("NEXT")
    add(f"  Daily collection : {f['next_daily'] or 'not reported'}")
    add(f"  Recovery window  : {f['next_retry'] or 'not reported'}")
    add("")
    add("ARCHIVE TO DATE")
    lt = f["lifetime"]
    add(
        f"  {lt['postings']} advertisements tracked, {lt['observations']} observations, "
        f"{lt['versions']} content versions, across {lt['runs']} runs."
    )
    if f["controlpanel_url"]:
        add("")
        add(f"ControlPanel: {f['controlpanel_url']}")
    add("")
    add(
        f"RowanJobs {f['app_version']}"
        + (f" ({str(f['app_revision'])[:12]})" if f["app_revision"] else "")
    )
    return "\n".join(lines) + "\n"


_COLOUR = {
    "success": "#1b7f3b",
    "partial": "#a86400",
    "failed": "#b3261e",
    "aborted": "#a86400",
    "lock_contention": "#5f6368",
}


def _esc(value: Any) -> str:
    return htmllib.escape("" if value is None else str(value))


def _rows(pairs: list[tuple[str, Any]]) -> str:
    out = []
    for label, value in pairs:
        out.append(
            '<tr><td style="padding:3px 14px 3px 0;color:#5f6368;white-space:nowrap">'
            f"{_esc(label)}</td>"
            f'<td style="padding:3px 0;color:#202124">{_esc(value)}</td></tr>'
        )
    return "".join(out)


def render_html(f: dict[str, Any]) -> str:
    colour = _COLOUR.get(f["outcome"], "#5f6368")
    parts: list[str] = []
    add = parts.append

    add(
        '<!DOCTYPE html><html><body style="margin:0;padding:18px;'
        "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        'font-size:14px;line-height:1.45;color:#202124;background:#ffffff">'
    )
    add('<div style="max-width:660px;margin:0 auto">')

    if f["delayed"]:
        add(
            '<div style="padding:9px 12px;margin-bottom:14px;border-radius:5px;'
            'background:#fff4e5;border:1px solid #f0c67a;color:#7a4b00">'
            "<strong>Delayed report.</strong> This describes an earlier collection and "
            "was sent late. The collection times below are the real ones.</div>"
        )

    add(
        f'<div style="border-left:4px solid {colour};padding:2px 0 2px 12px;margin-bottom:16px">'
        f'<div style="font-size:18px;font-weight:600;color:{colour}">'
        f"{_esc(f['outcome_label'])}</div>"
        f'<div style="color:#5f6368">{_esc(f["run_kind_label"])} for '
        f"<strong>{_esc(f['collection_date'])}</strong></div></div>"
    )

    if f["outcome_detail"]:
        add(f'<p style="margin:0 0 14px;color:#7a4b00">{_esc(f["outcome_detail"])}</p>')

    if f["is_baseline"]:
        add(
            '<p style="margin:0 0 14px;padding:9px 12px;border-radius:5px;background:#eef3fb;'
            'color:#1a3a6b">This was the baseline collection. Every advertisement was '
            "discovered at once, so none of them are newly published.</p>"
        )

    add('<table style="border-collapse:collapse;margin-bottom:16px">')
    add(
        _rows(
            [
                ("Started", f["started_at_local"]),
                ("Ended", f["ended_at_local"] or "still running"),
                ("Duration", f["duration"] or "-"),
                ("Run", f"{f['run_id']}  ({f['run_uuid']})"),
            ]
        )
    )
    add("</table>")

    def section(title: str, body: str) -> None:
        add(
            f'<div style="margin:0 0 16px"><div style="font-size:12px;font-weight:700;'
            f"letter-spacing:.06em;text-transform:uppercase;color:#5f6368;"
            f'border-bottom:1px solid #e3e5e8;padding-bottom:4px;margin-bottom:8px">'
            f"{_esc(title)}</div>{body}</div>"
        )

    listing_rows = [
        ("Advertised now", f"{f['listing_count']}  (source reported {f['source_reported_total']})"),
        ("Union encountered", f["union_encountered"]),
        ("Completeness", _listing_line(f)),
    ]
    sets = f["set_comparison"]
    if sets.get("performed"):
        listing_rows.append(
            (
                "Two passes",
                "identical identifier sets"
                if sets.get("sets_match")
                else "DIFFERED between passes",
            )
        )
    section("Listing", f'<table style="border-collapse:collapse">{_rows(listing_rows)}</table>')

    desc_rows: list[tuple[str, Any]] = [
        ("Descriptions captured", _captured_line(f)),
        ("New content versions", f["versions_created"]),
    ]
    if f["newly_observed"] is not None:
        desc_rows += [
            ("Newly observed", f["newly_observed"]),
            ("No longer listed", f["no_longer_listed"]),
            ("Content changed", f["content_changed"]),
        ]
    section("Descriptions", f'<table style="border-collapse:collapse">{_rows(desc_rows)}</table>')

    if not f["resources"]:
        doc_body = (
            '<div style="color:#5f6368">No job-specific documents were linked '
            "from the advertisements.</div>"
        )
    else:
        captured = len(f["resources"]) - len(f["resource_exceptions"])
        doc_body = (
            f'<table style="border-collapse:collapse">'
            f"{_rows([('Retrieved', f'{captured} of {len(f["resources"])}')])}</table>"
        )
        for r in f["resource_exceptions"]:
            doc_body += (
                '<div style="margin-top:7px;padding:8px 10px;border-radius:5px;'
                'background:#fdecea;border:1px solid #f2b8b5;color:#7a1e18">'
                f"<strong>{_esc(r['outcome'])}</strong> — "
                f'<span style="word-break:break-all">{_esc(r["url_resolved"])}</span>'
                + (
                    f'<br><span style="color:#5f6368">{_esc(r["outcome_detail"])}</span>'
                    if r["outcome_detail"]
                    else ""
                )
                + "</div>"
            )
    section("Linked documents", doc_body)

    if f["capture_failures"]:
        rows = "".join(
            f'<div style="padding:3px 0"><code>{_esc(r["expected_external_job_id"])}</code> '
            f"{_esc(r['availability_state'])} — "
            f'<span style="color:#5f6368">{_esc(str(r["availability_detail"] or "")[:120])}'
            "</span></div>"
            for r in f["capture_failures"]
        )
        section("Retrieval exceptions", rows)

    if f["coverage_gaps"]:
        rows = "".join(
            f'<div style="padding:3px 0"><strong>{_esc(g["kind"])}</strong> &times;{g["n"]}'
            f'<br><span style="color:#5f6368">{_esc(str(g["detail"])[:200])}</span></div>'
            for g in f["coverage_gaps"]
        )
        section("Coverage gaps", rows)

    action = f["action_required"]
    if action:
        section(
            "Action required",
            f'<div style="padding:9px 12px;border-radius:5px;background:#fdecea;'
            f'border:1px solid #f2b8b5;color:#7a1e18">{_esc(action)}</div>',
        )
    else:
        section("Action required", '<div style="color:#1b7f3b">None.</div>')

    section(
        "Next",
        f'<table style="border-collapse:collapse">{
            _rows(
                [
                    ("Daily collection", f["next_daily"] or "not reported"),
                    ("Recovery window", f["next_retry"] or "not reported"),
                ]
            )
        }</table>',
    )

    lt = f["lifetime"]
    section(
        "Archive to date",
        f'<div style="color:#5f6368">{lt["postings"]} advertisements tracked, '
        f"{lt['observations']} observations, {lt['versions']} content versions, "
        f"across {lt['runs']} runs.</div>",
    )

    if f["controlpanel_url"]:
        add(
            f'<p style="margin:0 0 10px"><a href="{_esc(f["controlpanel_url"])}" '
            f'style="color:#1a73e8">Open RowanJobs in ControlPanel</a></p>'
        )

    add(
        '<div style="margin-top:18px;padding-top:10px;border-top:1px solid #e3e5e8;'
        'color:#80868b;font-size:12px">'
        f"RowanJobs {_esc(f['app_version'])}"
        + (f" ({_esc(str(f['app_revision'])[:12])})" if f["app_revision"] else "")
        + f" &middot; generated {_esc(local_str(utc_str()))}</div>"
    )
    add("</div></body></html>")
    return "".join(parts)

"""Migration 2: derived projections.

These are views, so they cannot drift from the evidence. Every one of them
exposes the observation time and the coverage quality that backs it -- a caller
can always tell "checked today" apart from "last known, carried forward".
"""

from __future__ import annotations

from ..connection import Database
from ._exec import exec_script

SQL = """
-- Most recent qualified discovery scan per run.
CREATE VIEW v_qualified_scans AS
SELECT s.*, a.qualified, a.rules_version, a.reason AS qualification_reason,
       a.checks_json, r.scheduled_slot_local_date, r.run_kind
FROM listing_scans s
JOIN listing_scan_assessments a ON a.scan_id = s.scan_id
JOIN collection_runs r ON r.run_id = s.run_id
WHERE a.qualified = 1;

-- The latest listing appearance of each posting in any qualified scan.
CREATE VIEW v_last_listed AS
SELECT le.posting_id,
       MAX(le.observed_at_utc) AS last_listed_at_utc
FROM listing_entries le
JOIN v_qualified_scans qs ON qs.scan_id = le.scan_id
WHERE le.posting_id IS NOT NULL
GROUP BY le.posting_id;

-- The latest observation that actually captured advertisement content.
CREATE VIEW v_last_captured AS
SELECT o.posting_id,
       MAX(o.observed_at_utc) AS last_captured_at_utc
FROM posting_observations o
WHERE o.availability_state = 'content_captured'
  AND o.identity_state = 'match'
  AND o.posting_id IS NOT NULL
GROUP BY o.posting_id;

-- The latest observation of any kind, including failures.
CREATE VIEW v_last_observation AS
SELECT o.posting_id,
       MAX(o.observed_at_utc) AS last_observed_at_utc
FROM posting_observations o
WHERE o.posting_id IS NOT NULL
GROUP BY o.posting_id;

-- Current state. Freshness and certainty are explicit columns, not implied.
CREATE VIEW v_posting_current AS
SELECT
    p.posting_id,
    p.external_job_id,
    p.source_namespace,
    p.first_discovered_at_utc,
    p.discovery_basis,
    ll.last_listed_at_utc,
    lc.last_captured_at_utc,
    lo.last_observed_at_utc,
    latest.posting_version_id      AS current_version_id,
    latest.title                   AS current_title,
    latest.content_fingerprint     AS current_content_fingerprint,
    latest.observed_at_utc         AS current_content_observed_at_utc,
    latest_any.availability_state  AS last_availability_state,
    latest_any.identity_state      AS last_identity_state,
    latest_any.observed_at_utc     AS last_check_at_utc,
    CASE
        WHEN latest_any.availability_state IN ('access_control_challenge','retrieval_failed')
            THEN 'carried-forward-uncertain'
        WHEN latest.observed_at_utc IS NULL THEN 'never-captured'
        WHEN latest_any.observed_at_utc = latest.observed_at_utc THEN 'checked'
        ELSE 'carried-forward'
    END AS content_freshness,
    (SELECT COUNT(*) FROM posting_versions pv WHERE pv.posting_id = p.posting_id)
        AS version_count,
    (SELECT COUNT(*) FROM posting_observations po WHERE po.posting_id = p.posting_id)
        AS observation_count
FROM postings p
LEFT JOIN v_last_listed ll ON ll.posting_id = p.posting_id
LEFT JOIN v_last_captured lc ON lc.posting_id = p.posting_id
LEFT JOIN v_last_observation lo ON lo.posting_id = p.posting_id
LEFT JOIN (
    SELECT o.posting_id, o.posting_version_id, o.observed_at_utc, v.title,
           v.content_fingerprint
    FROM posting_observations o
    JOIN posting_versions v ON v.posting_version_id = o.posting_version_id
    WHERE o.observed_at_utc = (
        SELECT MAX(o2.observed_at_utc) FROM posting_observations o2
        WHERE o2.posting_id = o.posting_id AND o2.posting_version_id IS NOT NULL)
) latest ON latest.posting_id = p.posting_id
LEFT JOIN (
    SELECT o.posting_id, o.availability_state, o.identity_state, o.observed_at_utc
    FROM posting_observations o
    WHERE o.observed_at_utc = (
        SELECT MAX(o2.observed_at_utc) FROM posting_observations o2
        WHERE o2.posting_id = o.posting_id)
) latest_any ON latest_any.posting_id = p.posting_id;

-- Full observation history, joined to the content version it referenced.
CREATE VIEW v_posting_history AS
SELECT
    o.observation_id,
    o.posting_id,
    p.external_job_id,
    o.run_id,
    r.run_kind,
    r.scheduled_slot_local_date,
    o.observed_at_utc,
    o.identity_state,
    o.availability_state,
    o.availability_detail,
    o.redirect_class,
    o.checked_because,
    o.posting_version_id,
    v.content_fingerprint,
    v.description_text_fingerprint,
    v.title,
    f.http_status,
    f.final_url,
    f.access_control_signal
FROM posting_observations o
JOIN postings p ON p.posting_id = o.posting_id
JOIN collection_runs r ON r.run_id = o.run_id
LEFT JOIN posting_versions v ON v.posting_version_id = o.posting_version_id
LEFT JOIN fetches f ON f.fetch_id = o.fetch_id;

-- Listing presence per qualified scan, so absence is always scan-scoped.
CREATE VIEW v_listing_presence AS
SELECT
    qs.scan_id,
    qs.run_id,
    qs.scheduled_slot_local_date,
    qs.scan_role,
    qs.started_at_utc AS scan_started_at_utc,
    qs.ended_at_utc   AS scan_ended_at_utc,
    p.posting_id,
    p.external_job_id,
    CASE WHEN le.posting_id IS NULL THEN 0 ELSE 1 END AS listed
FROM v_qualified_scans qs
CROSS JOIN postings p
LEFT JOIN (
    SELECT DISTINCT scan_id, posting_id FROM listing_entries WHERE posting_id IS NOT NULL
) le ON le.scan_id = qs.scan_id AND le.posting_id = p.posting_id;

-- Collection health, one row per run.
CREATE VIEW v_run_health AS
SELECT
    r.run_id, r.run_uuid, r.run_kind, r.attempt_no, r.parent_run_id,
    r.scheduled_slot_local_date, r.scheduled_slot_utc,
    r.started_at_utc, r.ended_at_utc, r.outcome, r.outcome_detail,
    r.is_baseline, r.app_version, r.app_revision,
    (SELECT COUNT(*) FROM listing_scans s WHERE s.run_id = r.run_id) AS scans,
    (SELECT COUNT(*) FROM v_qualified_scans q WHERE q.run_id = r.run_id) AS qualified_scans,
    (SELECT COUNT(*) FROM fetches f WHERE f.run_id = r.run_id) AS fetches,
    (SELECT COUNT(*) FROM fetches f WHERE f.run_id = r.run_id
        AND f.response_state != 'complete') AS failed_fetches,
    (SELECT COUNT(*) FROM fetches f WHERE f.run_id = r.run_id
        AND f.access_control_signal IS NOT NULL) AS access_control_responses,
    (SELECT COUNT(*) FROM posting_observations o WHERE o.run_id = r.run_id) AS observations,
    (SELECT COUNT(*) FROM posting_observations o WHERE o.run_id = r.run_id
        AND o.availability_state = 'content_captured') AS captured,
    (SELECT COUNT(*) FROM resource_observations ro WHERE ro.run_id = r.run_id) AS resources,
    (SELECT COUNT(*) FROM coverage_gaps g WHERE g.run_id = r.run_id) AS coverage_gaps
FROM collection_runs r;

-- Basic content-change history, derived but always evidence-backed.
CREATE VIEW v_content_changes AS
SELECT e.presence_event_id, e.posting_id, p.external_job_id,
       e.interval_start_utc, e.interval_end_utc, e.rules_version,
       e.from_posting_version_id, e.to_posting_version_id,
       fv.content_fingerprint AS from_fingerprint,
       tv.content_fingerprint AS to_fingerprint,
       e.evidence_json
FROM presence_events e
JOIN postings p ON p.posting_id = e.posting_id
LEFT JOIN posting_versions fv ON fv.posting_version_id = e.from_posting_version_id
LEFT JOIN posting_versions tv ON tv.posting_version_id = e.to_posting_version_id
WHERE e.event_kind = 'content_changed';
"""


def upgrade(db: Database) -> None:
    exec_script(db, SQL)

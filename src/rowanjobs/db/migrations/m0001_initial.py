"""Migration 1: the evidence model.

Design rules encoded below:

* Evidence tables (``fetches``, ``artifacts``, ``listing_entries``,
  ``posting_observations``, ``extractions``, ``posting_versions``) are
  append-only in ordinary ingestion. Only operational tables (``work_queue``,
  ``recheck_policy``) and run summaries are mutable.
* Derived tables (``presence_events``, ``coverage_gaps``) always carry the rules
  version and the evidence ids that produced them, so they are rebuildable.
* Structured fields sit next to a complete extensible extraction record. The
  JSON is never the only query interface.
"""

from __future__ import annotations

from ..connection import Database
from ._exec import exec_script

SQL = """
------------------------------------------------------------------ identity

CREATE TABLE sources (
    source_id       INTEGER PRIMARY KEY,
    namespace       TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    adapter         TEXT NOT NULL,
    created_at_utc  TEXT NOT NULL
);

CREATE TABLE source_configs (
    source_config_id      INTEGER PRIMARY KEY,
    source_id             INTEGER NOT NULL REFERENCES sources(source_id),
    config_hash           TEXT NOT NULL,
    config_json           TEXT NOT NULL,
    scope_label           TEXT NOT NULL,
    locale                TEXT NOT NULL,
    start_urls_json       TEXT NOT NULL,
    retrieval_policy_json TEXT NOT NULL,
    comparability_group   TEXT NOT NULL,
    app_version           TEXT NOT NULL,
    created_at_utc        TEXT NOT NULL,
    UNIQUE(source_id, config_hash)
);

--------------------------------------------------------------------- runs

CREATE TABLE collection_runs (
    run_id                   INTEGER PRIMARY KEY,
    run_uuid                 TEXT NOT NULL UNIQUE,
    source_id                INTEGER NOT NULL REFERENCES sources(source_id),
    source_config_id         INTEGER NOT NULL REFERENCES source_configs(source_config_id),
    run_kind                 TEXT NOT NULL
        CHECK (run_kind IN ('daily','retry','manual','verification')),
    scheduled_slot_utc       TEXT,
    scheduled_slot_local_date TEXT,
    parent_run_id            INTEGER REFERENCES collection_runs(run_id),
    attempt_no               INTEGER NOT NULL DEFAULT 1,
    app_version              TEXT NOT NULL,
    app_revision             TEXT,
    host                     TEXT NOT NULL,
    os_user                  TEXT NOT NULL,
    pid                      INTEGER,
    sqlite_runtime_json      TEXT NOT NULL,
    started_at_utc           TEXT NOT NULL,
    ended_at_utc             TEXT,
    heartbeat_at_utc         TEXT,
    outcome                  TEXT
        CHECK (outcome IS NULL OR outcome IN
               ('success','partial','failed','aborted','lock_contention')),
    outcome_detail           TEXT,
    counts_json              TEXT,
    errors_json              TEXT,
    coverage_json            TEXT,
    -- Baseline runs must not label pre-existing advertisements as newly posted.
    is_baseline              INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_runs_slot ON collection_runs(scheduled_slot_local_date, run_kind);
CREATE INDEX idx_runs_started ON collection_runs(started_at_utc);
CREATE INDEX idx_runs_parent ON collection_runs(parent_run_id);

---------------------------------------------------------------- artifacts

CREATE TABLE artifacts (
    artifact_id             INTEGER PRIMARY KEY,
    -- SHA-256 of the archived UNCOMPRESSED bytes. Changing the compression
    -- method must never change content identity.
    sha256                  TEXT NOT NULL UNIQUE,
    byte_length             INTEGER NOT NULL,
    compression             TEXT NOT NULL CHECK (compression IN ('zlib','none')),
    compressed_bytes        INTEGER NOT NULL,
    blob                    BLOB NOT NULL,
    capture_state           TEXT NOT NULL CHECK (capture_state IN ('complete','partial','empty')),
    capture_exception       TEXT,
    -- What these bytes actually are. 'http-decoded-body' means the client
    -- removed a Content-Encoding; they are NOT original wire bytes.
    representation          TEXT NOT NULL,
    content_encoding_removed TEXT,
    declared_content_length INTEGER,
    media_type              TEXT,
    charset_declared        TEXT,
    first_seen_at_utc       TEXT NOT NULL,
    first_run_id            INTEGER REFERENCES collection_runs(run_id)
);
CREATE INDEX idx_artifacts_seen ON artifacts(first_seen_at_utc);

------------------------------------------------------------------ fetches

CREATE TABLE fetches (
    fetch_id                INTEGER PRIMARY KEY,
    run_id                  INTEGER REFERENCES collection_runs(run_id),
    purpose                 TEXT NOT NULL
        CHECK (purpose IN ('listing_page','posting_detail','resource','probe')),
    requested_url           TEXT NOT NULL,
    final_url               TEXT,
    method                  TEXT NOT NULL DEFAULT 'GET',
    transport               TEXT NOT NULL CHECK (transport IN ('httpx','browser')),
    attempt_no              INTEGER NOT NULL DEFAULT 1,
    started_at_utc          TEXT NOT NULL,
    ended_at_utc            TEXT,
    duration_ms             INTEGER,
    http_status             INTEGER,
    http_version            TEXT,
    response_state          TEXT NOT NULL
        CHECK (response_state IN ('complete','partial','no_response')),
    redirect_count          INTEGER NOT NULL DEFAULT 0,
    redirect_chain_json     TEXT,
    request_headers_json    TEXT,
    response_headers_json   TEXT,
    content_encoding        TEXT,
    received_bytes          INTEGER,
    artifact_id             INTEGER REFERENCES artifacts(artifact_id),
    -- Populated when the source answered with an access control mechanism.
    -- This is collection uncertainty, never evidence of absence.
    access_control_signal   TEXT,
    retry_after             TEXT,
    failure_kind            TEXT,
    failure_detail          TEXT
);
CREATE INDEX idx_fetches_run ON fetches(run_id, purpose);
CREATE INDEX idx_fetches_url ON fetches(requested_url, started_at_utc);
CREATE INDEX idx_fetches_started ON fetches(started_at_utc);

-------------------------------------------------------------- extractions

CREATE TABLE extractions (
    extraction_id       INTEGER PRIMARY KEY,
    artifact_id         INTEGER NOT NULL REFERENCES artifacts(artifact_id),
    parser_name         TEXT NOT NULL,
    parser_version      TEXT NOT NULL,
    contract_version    TEXT NOT NULL,
    text_contract_version TEXT NOT NULL,
    extracted_at_utc    TEXT NOT NULL,
    run_id              INTEGER REFERENCES collection_runs(run_id),
    status              TEXT NOT NULL CHECK (status IN ('ok','partial','failed')),
    output_json         TEXT,
    warnings_json       TEXT,
    failure_detail      TEXT,
    decode_encoding     TEXT,
    decode_strategy     TEXT,
    decode_error_count  INTEGER NOT NULL DEFAULT 0,
    -- An extraction is a pure function of (artifact, parser, contracts), so the
    -- same combination is stored once. A parser upgrade produces a new row and
    -- never rewrites the old interpretation.
    UNIQUE(artifact_id, parser_name, parser_version, contract_version, text_contract_version)
);
CREATE INDEX idx_extractions_artifact ON extractions(artifact_id);
CREATE INDEX idx_extractions_run ON extractions(run_id);

----------------------------------------------------------------- postings

CREATE TABLE postings (
    posting_id                INTEGER PRIMARY KEY,
    source_id                 INTEGER NOT NULL REFERENCES sources(source_id),
    source_namespace          TEXT NOT NULL,
    external_job_id           TEXT NOT NULL,
    first_discovered_at_utc   TEXT NOT NULL,
    first_discovered_run_id   INTEGER REFERENCES collection_runs(run_id),
    -- 'baseline'        : present at the first collection; NOT newly posted.
    -- 'observed-new'    : first appeared after a qualified baseline existed.
    discovery_basis           TEXT NOT NULL
        CHECK (discovery_basis IN ('baseline','observed-new')),
    UNIQUE(source_namespace, external_job_id)
);

CREATE TABLE posting_urls (
    posting_url_id    INTEGER PRIMARY KEY,
    posting_id        INTEGER NOT NULL REFERENCES postings(posting_id),
    url               TEXT NOT NULL,
    role              TEXT NOT NULL,
    provenance        TEXT NOT NULL,
    first_seen_at_utc TEXT NOT NULL,
    last_seen_at_utc  TEXT NOT NULL,
    seen_count        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(posting_id, url, role)
);
CREATE INDEX idx_posting_urls_url ON posting_urls(url);

------------------------------------------------------------ listing scans

CREATE TABLE listing_scans (
    scan_id                 INTEGER PRIMARY KEY,
    run_id                  INTEGER NOT NULL REFERENCES collection_runs(run_id),
    scan_ordinal            INTEGER NOT NULL,
    scan_role               TEXT NOT NULL
        CHECK (scan_role IN ('discovery','verification','reconciliation')),
    started_at_utc          TEXT NOT NULL,
    ended_at_utc            TEXT,
    pages_requested         INTEGER NOT NULL DEFAULT 0,
    pages_ok                INTEGER NOT NULL DEFAULT 0,
    pages_failed            INTEGER NOT NULL DEFAULT 0,
    last_page_number        INTEGER,
    termination_reason      TEXT,
    entries_seen            INTEGER NOT NULL DEFAULT 0,
    unique_ids_seen         INTEGER NOT NULL DEFAULT 0,
    unresolved_candidates   INTEGER NOT NULL DEFAULT 0,
    duplicate_occurrences   INTEGER NOT NULL DEFAULT 0,
    source_reported_total   INTEGER,
    encountered_ids_json    TEXT,
    UNIQUE(run_id, scan_ordinal)
);

-- Versioned completeness assessment. Absence analysis reads this, not the scan.
CREATE TABLE listing_scan_assessments (
    assessment_id   INTEGER PRIMARY KEY,
    scan_id         INTEGER NOT NULL REFERENCES listing_scans(scan_id),
    rules_version   TEXT NOT NULL,
    assessed_at_utc TEXT NOT NULL,
    qualified       INTEGER NOT NULL CHECK (qualified IN (0,1)),
    reason          TEXT,
    checks_json     TEXT NOT NULL,
    UNIQUE(scan_id, rules_version)
);

CREATE TABLE listing_pages (
    listing_page_id          INTEGER PRIMARY KEY,
    scan_id                  INTEGER NOT NULL REFERENCES listing_scans(scan_id),
    page_number              INTEGER NOT NULL,
    page_items               INTEGER,
    url                      TEXT NOT NULL,
    fetch_id                 INTEGER REFERENCES fetches(fetch_id),
    extraction_id            INTEGER REFERENCES extractions(extraction_id),
    structure_recognized     INTEGER,
    empty_result_validated   INTEGER,
    entry_count              INTEGER,
    unique_id_count          INTEGER,
    more_link_url            TEXT,
    more_link_remaining      INTEGER,
    page_signature           TEXT,
    observed_at_utc          TEXT NOT NULL,
    UNIQUE(scan_id, page_number)
);

CREATE TABLE listing_entries (
    listing_entry_id      INTEGER PRIMARY KEY,
    listing_page_id       INTEGER NOT NULL REFERENCES listing_pages(listing_page_id),
    scan_id               INTEGER NOT NULL REFERENCES listing_scans(scan_id),
    run_id                INTEGER NOT NULL REFERENCES collection_runs(run_id),
    extraction_id         INTEGER REFERENCES extractions(extraction_id),
    section               TEXT NOT NULL,
    position_in_section   INTEGER NOT NULL,
    page_number           INTEGER NOT NULL,
    href_raw              TEXT,
    href_resolved         TEXT,
    external_job_id       TEXT,
    posting_id            INTEGER REFERENCES postings(posting_id),
    resolution_state      TEXT NOT NULL
        CHECK (resolution_state IN ('resolved','unresolved_id','unrecognized_row')),
    resolution_detail     TEXT,
    title_text            TEXT,
    summary_text          TEXT,
    summary_html          TEXT,
    displayed_metadata_json TEXT,
    observed_at_utc       TEXT NOT NULL
);
CREATE INDEX idx_listing_entries_scan ON listing_entries(scan_id, section);
CREATE INDEX idx_listing_entries_posting ON listing_entries(posting_id, observed_at_utc);
CREATE INDEX idx_listing_entries_extjob ON listing_entries(external_job_id);

--------------------------------------------------------- posting versions

CREATE TABLE posting_versions (
    posting_version_id        INTEGER PRIMARY KEY,
    posting_id                INTEGER NOT NULL REFERENCES postings(posting_id),
    contract_version          TEXT NOT NULL,
    text_contract_version     TEXT NOT NULL,
    content_fingerprint       TEXT NOT NULL,
    description_text_fingerprint TEXT NOT NULL,
    description_html_fingerprint TEXT NOT NULL,
    metadata_fingerprint      TEXT NOT NULL,
    title                     TEXT,
    description_html          TEXT,
    -- 'source-substring' : byte-for-byte slice of the archived document.
    -- 'reserialized'     : rebuilt by the parser; NOT byte-identical.
    description_html_kind     TEXT NOT NULL
        CHECK (description_html_kind IN ('source-substring','reserialized','absent')),
    description_text          TEXT,
    first_seen_at_utc         TEXT NOT NULL,
    first_extraction_id       INTEGER REFERENCES extractions(extraction_id),
    first_run_id              INTEGER REFERENCES collection_runs(run_id),
    UNIQUE(posting_id, contract_version, content_fingerprint)
);
CREATE INDEX idx_versions_posting ON posting_versions(posting_id, first_seen_at_utc);

CREATE TABLE version_values (
    version_value_id   INTEGER PRIMARY KEY,
    posting_version_id INTEGER NOT NULL REFERENCES posting_versions(posting_version_id),
    field_key          TEXT NOT NULL,
    source_label       TEXT,
    ordinal            INTEGER NOT NULL DEFAULT 0,
    value_text         TEXT,
    value_html         TEXT,
    field_state        TEXT NOT NULL
        CHECK (field_state IN ('present','blank','absent','unresolved')),
    origin             TEXT NOT NULL,
    known_label        INTEGER NOT NULL DEFAULT 1,
    -- Normalisation is stored separately and never replaces the source value.
    normalized_json    TEXT,
    date_parse_state   TEXT
        CHECK (date_parse_state IS NULL OR date_parse_state IN
               ('parsed','unparsed','invalid','absent')),
    source_precision   TEXT,
    source_tz_text     TEXT,
    source_machine_value TEXT,
    parsed_utc         TEXT,
    parsed_local_date  TEXT,
    UNIQUE(posting_version_id, field_key, ordinal, origin)
);
CREATE INDEX idx_version_values_key ON version_values(field_key, value_text);

------------------------------------------------------------- observations

CREATE TABLE posting_observations (
    observation_id           INTEGER PRIMARY KEY,
    run_id                   INTEGER NOT NULL REFERENCES collection_runs(run_id),
    posting_id               INTEGER REFERENCES postings(posting_id),
    expected_external_job_id TEXT NOT NULL,
    requested_url            TEXT NOT NULL,
    fetch_id                 INTEGER REFERENCES fetches(fetch_id),
    observed_at_utc          TEXT NOT NULL,
    observed_external_job_id TEXT,
    identity_state           TEXT NOT NULL
        CHECK (identity_state IN ('match','mismatch','absent_on_page','not_observed')),
    availability_state       TEXT NOT NULL,
    availability_detail      TEXT,
    redirect_class           TEXT,
    extraction_id            INTEGER REFERENCES extractions(extraction_id),
    posting_version_id       INTEGER REFERENCES posting_versions(posting_version_id),
    checked_because          TEXT NOT NULL,
    conflicts_json           TEXT
);
-- Deliberately NOT unique on (posting_id, date): several observations per day
-- are legitimate and must all be preserved.
CREATE INDEX idx_obs_posting_time ON posting_observations(posting_id, observed_at_utc);
CREATE INDEX idx_obs_run ON posting_observations(run_id);
CREATE INDEX idx_obs_expected ON posting_observations(expected_external_job_id, observed_at_utc);

-------------------------------------------------------------- resources

CREATE TABLE resource_links (
    resource_link_id    INTEGER PRIMARY KEY,
    posting_version_id  INTEGER REFERENCES posting_versions(posting_version_id),
    extraction_id       INTEGER REFERENCES extractions(extraction_id),
    parent_kind         TEXT NOT NULL,
    url_raw             TEXT NOT NULL,
    url_resolved        TEXT,
    link_text           TEXT,
    rel                 TEXT,
    position            INTEGER,
    classification      TEXT NOT NULL,
    collection_decision TEXT NOT NULL CHECK (collection_decision IN ('fetch','exclude')),
    exclusion_reason    TEXT,
    first_seen_at_utc   TEXT NOT NULL,
    UNIQUE(posting_version_id, url_raw, position)
);
CREATE INDEX idx_resource_links_class ON resource_links(classification, collection_decision);

CREATE TABLE resource_observations (
    resource_observation_id INTEGER PRIMARY KEY,
    run_id                  INTEGER NOT NULL REFERENCES collection_runs(run_id),
    url_resolved            TEXT NOT NULL,
    fetch_id                INTEGER REFERENCES fetches(fetch_id),
    -- The resource has its own retrieval time, independent of its parent page.
    observed_at_utc         TEXT NOT NULL,
    outcome                 TEXT NOT NULL,
    outcome_detail          TEXT,
    artifact_id             INTEGER REFERENCES artifacts(artifact_id),
    content_sha256          TEXT,
    media_type              TEXT,
    byte_length             INTEGER,
    text_extraction_state   TEXT NOT NULL DEFAULT 'not_attempted',
    text_content            TEXT
);
CREATE INDEX idx_resource_obs_url ON resource_observations(url_resolved, observed_at_utc);

-- One retrieval can serve several parents in a run; the association keeps the
-- true retrieval timestamp intact instead of duplicating the download.
CREATE TABLE resource_associations (
    resource_association_id INTEGER PRIMARY KEY,
    resource_observation_id INTEGER NOT NULL REFERENCES resource_observations(resource_observation_id),
    observation_id          INTEGER NOT NULL REFERENCES posting_observations(observation_id),
    resource_link_id        INTEGER REFERENCES resource_links(resource_link_id),
    created_at_utc          TEXT NOT NULL,
    UNIQUE(resource_observation_id, observation_id)
);

------------------------------------------------------------- operational

CREATE TABLE work_queue (
    work_id        INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES collection_runs(run_id),
    kind           TEXT NOT NULL CHECK (kind IN ('posting_detail','resource')),
    work_key       TEXT NOT NULL,
    payload_json   TEXT,
    priority       INTEGER NOT NULL DEFAULT 100,
    state          TEXT NOT NULL
        CHECK (state IN ('pending','in_progress','done','failed','abandoned')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 3,
    claim_token    TEXT,
    claimed_at_utc TEXT,
    updated_at_utc TEXT NOT NULL,
    last_error     TEXT,
    UNIQUE(run_id, kind, work_key)
);
CREATE INDEX idx_work_state ON work_queue(run_id, state, priority);

CREATE TABLE recheck_policy (
    posting_id                       INTEGER PRIMARY KEY REFERENCES postings(posting_id),
    tier                             TEXT NOT NULL CHECK (tier IN ('daily','weekly')),
    consecutive_terminal_observations INTEGER NOT NULL DEFAULT 0,
    last_checked_at_utc              TEXT,
    next_due_at_utc                  TEXT,
    reason                           TEXT,
    updated_at_utc                   TEXT NOT NULL
);
CREATE INDEX idx_recheck_due ON recheck_policy(tier, next_due_at_utc);

CREATE TABLE deployments (
    deployment_id   INTEGER PRIMARY KEY,
    recorded_at_utc TEXT NOT NULL,
    app_version     TEXT NOT NULL,
    app_revision    TEXT,
    git_dirty       INTEGER,
    python_version  TEXT NOT NULL,
    sqlite_runtime_json TEXT NOT NULL,
    host            TEXT NOT NULL,
    os_user         TEXT NOT NULL,
    install_path    TEXT NOT NULL,
    data_root       TEXT NOT NULL,
    units_json      TEXT,
    notes           TEXT
);

CREATE TABLE backups (
    backup_id          INTEGER PRIMARY KEY,
    created_at_utc     TEXT NOT NULL,
    kind               TEXT NOT NULL,
    path               TEXT NOT NULL,
    manifest_path      TEXT,
    snapshot_at_utc    TEXT NOT NULL,
    schema_version     INTEGER NOT NULL,
    app_version        TEXT NOT NULL,
    last_run_id        INTEGER REFERENCES collection_runs(run_id),
    artifact_count     INTEGER,
    posting_count      INTEGER,
    file_bytes         INTEGER NOT NULL,
    sha256             TEXT NOT NULL,
    verification_state TEXT NOT NULL,
    verification_detail TEXT,
    verified_at_utc    TEXT,
    offhost_state      TEXT NOT NULL DEFAULT 'UNCONFIGURED',
    offhost_target     TEXT,
    offhost_detail     TEXT,
    pruned_at_utc      TEXT
);
CREATE INDEX idx_backups_created ON backups(created_at_utc);

------------------------------------------------------------------ derived

-- Rebuildable. Always carries the rules version and the supporting evidence.
CREATE TABLE presence_events (
    presence_event_id     INTEGER PRIMARY KEY,
    posting_id            INTEGER NOT NULL REFERENCES postings(posting_id),
    event_kind            TEXT NOT NULL,
    rules_version         TEXT NOT NULL,
    run_id                INTEGER REFERENCES collection_runs(run_id),
    scan_id               INTEGER REFERENCES listing_scans(scan_id),
    observation_id        INTEGER REFERENCES posting_observations(observation_id),
    from_posting_version_id INTEGER REFERENCES posting_versions(posting_version_id),
    to_posting_version_id   INTEGER REFERENCES posting_versions(posting_version_id),
    -- The event happened somewhere inside this interval. We never manufacture
    -- an exact event time.
    interval_start_utc    TEXT,
    interval_end_utc      TEXT NOT NULL,
    slot_local_date       TEXT,
    comparability_group   TEXT NOT NULL,
    evidence_json         TEXT NOT NULL,
    created_at_utc        TEXT NOT NULL
);
CREATE INDEX idx_presence_posting ON presence_events(posting_id, interval_end_utc);
CREATE INDEX idx_presence_kind ON presence_events(event_kind, interval_end_utc);
CREATE UNIQUE INDEX idx_presence_dedup
    ON presence_events(posting_id, event_kind, rules_version, interval_end_utc, COALESCE(run_id,-1));

CREATE TABLE coverage_gaps (
    coverage_gap_id  INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL,
    scope            TEXT NOT NULL,
    run_id           INTEGER REFERENCES collection_runs(run_id),
    scan_id          INTEGER REFERENCES listing_scans(scan_id),
    posting_id       INTEGER REFERENCES postings(posting_id),
    slot_local_date  TEXT,
    detected_at_utc  TEXT NOT NULL,
    window_start_utc TEXT,
    window_end_utc   TEXT,
    detail           TEXT NOT NULL,
    evidence_json    TEXT,
    resolved_at_utc  TEXT
);
CREATE INDEX idx_gaps_detected ON coverage_gaps(detected_at_utc);
CREATE INDEX idx_gaps_kind ON coverage_gaps(kind, resolved_at_utc);
"""


def upgrade(db: Database) -> None:
    exec_script(db, SQL)

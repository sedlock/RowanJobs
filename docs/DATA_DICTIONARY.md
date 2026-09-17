# Data dictionary

Every table and view created by the migrations in
`src/rowanjobs/db/migrations/`: `m0001_initial.py` (schema v1, the evidence
model), `m0002_views.py` (schema v2, the projections),
`m0003_comparison_lineage.py` (v3, `posting_versions.parser_version`),
`m0004_availability_guard.py` (v4, the `availability_state` trigger) and
`m0005_resource_links_per_extraction.py` (v5, `resource_links` rebuilt per
extraction).

Enumerated values come from `src/rowanjobs/constants.py`; where the migration
also encodes them as a `CHECK` constraint that is noted, because the two must
stay in step.

Conventions used throughout:

- Every `*_at_utc` / `*_utc` column is an ISO-8601 UTC string with second
  precision and a `Z` suffix, e.g. `2026-09-16T21:41:08Z`
  (`src/rowanjobs/timeutil.py::utc_str`). Naive datetimes are rejected.
- Every `*_local_date` column is an `America/New_York` calendar date
  (`YYYY-MM-DD`). This is the daily-confirmation key.
- `*_json` columns hold compact JSON produced by
  `src/rowanjobs/collect/repo.py::_json`. The JSON is always *additional* to the
  structured columns beside it, never the only query interface.
- Booleans are `INTEGER` 0/1.

## Table classification

| Class | Tables | Rule |
|---|---|---|
| **Append-only evidence** | `artifacts`, `fetches`, `extractions`, `listing_scans`*, `listing_pages`, `listing_entries`, `listing_scan_assessments`, `posting_observations`, `posting_versions`, `version_values`, `resource_links`, `resource_observations`, `resource_associations` | Ordinary ingestion only inserts. Never `UPDATE` or `DELETE` these from the collection path. |
| **Append-only identity** | `postings`, `posting_urls`, `sources`, `source_configs` | Rows are never removed or re-identified. `posting_urls` advances `last_seen_at_utc`/`seen_count`; `postings` rows are immutable once created. |
| **Mutable operational state** | `work_queue`, `recheck_policy`, `collection_runs`, `backups`, `deployments` | Updated in place by design: a run is opened, heartbeated and closed; queue items change state; a pruned backup is marked, and so on. |
| **Derived, rebuildable** | `presence_events`, `coverage_gaps` | Always carry `rules_version` and the evidence ids that produced them. Can be dropped and rebuilt from the evidence without contacting the source. |
| **Projections** | all `v_*` views | Cannot drift from the evidence, because they are views. |

\* `listing_scans` is opened at the start of a traversal and closed with its
totals when the traversal ends (`Repository.finish_scan`); the row itself is
never replaced or reinterpreted afterwards.

---

# Tables

## `schema_migrations`

Created by `src/rowanjobs/db/migrations/__init__.py::_bootstrap`, outside the
numbered migrations.

| Column | Meaning |
|---|---|
| `version` | Migration number, primary key |
| `name` | Migration name (`initial`, `views`, `comparison_lineage`, `availability_guard`, `resource_links_per_extraction`) |
| `applied_at_utc` | When it was applied |

## `sources`

One row per collected source. RowanJobs v1 has exactly one.

| Column | Meaning |
|---|---|
| `source_id` | Primary key |
| `namespace` | Stable source namespace, unique. `rowan.pageup` (`SOURCE_NAMESPACE`) |
| `display_name` | Human label, e.g. "Rowan University career site (PageUp)" |
| `base_url` | Site root, from `collection.base_url` |
| `adapter` | Adapter family in use; `pageup_v1` |
| `created_at_utc` | When the source was first registered |

## `source_configs`

The *effective collection policy* for a run, hashed so a past run can be
reproduced. Local filesystem paths are deliberately excluded from the hash, so
the same policy hashes identically on a restore host
(`src/rowanjobs/config.py::effective_dict`).

| Column | Meaning |
|---|---|
| `source_config_id` | Primary key |
| `source_id` | → `sources` |
| `config_hash` | SHA-256 of the canonical effective config JSON; unique per source |
| `config_json` | The effective config: `network`, `browser`, `collection`, `schedule` sections |
| `scope_label` | What was in scope, from `collection.scope_label` (default `all-unfiltered`) |
| `locale` | Site locale segment, default `en-us` |
| `start_urls_json` | The traversal start URLs |
| `retrieval_policy_json` | The full network policy that governed live traffic |
| `comparability_group` | Absence history is only comparable within one group (default `v1-unfiltered-en-us`). Changing the collected scope must change this. |
| `app_version` | `rowanjobs.__version__` at the time |
| `created_at_utc` | First use of this configuration |

## `collection_runs`

One row per collection attempt. Mutable: opened at start, heartbeated, closed.

| Column | Meaning / permitted values |
|---|---|
| `run_id` | Primary key |
| `run_uuid` | Externally stable identifier, unique |
| `source_id`, `source_config_id` | → `sources`, `source_configs` |
| `run_kind` | `daily`, `retry`, `manual`, `verification` (`RUN_KINDS`, `CHECK`) |
| `scheduled_slot_utc` | The UTC instant of the slot this run belongs to |
| `scheduled_slot_local_date` | The `America/New_York` date of that slot. **A retry inherits its parent's slot**, so same-day retries cannot manufacture extra daily observations (`timeutil.slot_for`) |
| `parent_run_id` | → `collection_runs`, set for a retry |
| `attempt_no` | 1 for the scheduled run; increments for retries |
| `app_version`, `app_revision` | Application version and git revision (may be null) |
| `host`, `os_user`, `pid` | Where and as whom it ran |
| `sqlite_runtime_json` | The SQLite runtime evidence for this run (`db/runtime.py::RuntimeInfo`) |
| `started_at_utc`, `ended_at_utc` | Run boundaries; `ended_at_utc` null means in progress |
| `heartbeat_at_utc` | Advanced periodically during a long harvest |
| `outcome` | `success`, `partial`, `failed`, `aborted`, `lock_contention` (`RUN_OUTCOMES`, `CHECK`); null while running. `partial` covers any coverage exception, **including queued detail retrievals left `pending`/`in_progress`** — a bounded (`--max-details`) or interrupted pass did not cover what it discovered, so it never reports `success` (`collect/runner.py::_outcome`) |
| `outcome_detail` | Prose reason, e.g. which coverage exceptions occurred |
| `counts_json` | Per-run counters: listing scans/pages, detail attempted/captured/failed/uncertain, versions created, events, budget stats, queue summary |
| `errors_json` | Structured errors encountered |
| `coverage_json` | Coverage quality: qualification per scan, the set comparison, `absence_analysis_supported`, rules version |
| `is_baseline` | 1 when no qualified scan existed before this run. **Baseline runs must not label pre-existing advertisements as newly posted.** |

Indexes: `(scheduled_slot_local_date, run_kind)`, `started_at_utc`,
`parent_run_id`.

## `artifacts`

Content-addressed payload storage (`src/rowanjobs/archive/store.py`).

| Column | Meaning / permitted values |
|---|---|
| `artifact_id` | Primary key |
| `sha256` | SHA-256 of the archived **uncompressed** bytes, unique. Changing the compression method never changes content identity. |
| `byte_length` | Uncompressed length |
| `compression` | `zlib` or `none` (`CHECK`); payloads under 256 bytes are stored uncompressed |
| `compressed_bytes` | Stored length |
| `blob` | The payload itself |
| `capture_state` | `complete`, `partial`, `empty` (`CAPTURE_STATES`, `CHECK`). `partial` means the response exceeded the byte ceiling and the stored bytes are a prefix. |
| `capture_exception` | Why the capture was not complete |
| `representation` | `http-decoded-body`, `http-wire-body`, `browser-dom-serialized` (`REPRESENTATIONS`). `http-decoded-body` means a `Content-Encoding` was removed by the HTTP client — these are **not** original wire bytes. |
| `content_encoding_removed` | The encoding that was stripped, if any |
| `declared_content_length` | The `Content-Length` header value, if present |
| `media_type` | Media type from `Content-Type`, without parameters |
| `charset_declared` | Charset parameter from `Content-Type`, if any |
| `first_seen_at_utc` | When these exact bytes were first archived |
| `first_run_id` | → `collection_runs`; the run that first archived them |

## `fetches`

One row per retrieval **attempt**, including the ones that produced nothing.
A single logical retrieval can therefore produce several rows: a challenge that
was later worked around, or a timeout a retry recovered from, is inserted as its
own row (oldest first) before the attempt that succeeded, distinguished by
`attempt_no` (`src/rowanjobs/collect/repo.py::record_fetch`,
`FetchResult.superseded_attempts`). The archive never claims a clean single
request where the source actually pushed back.

| Column | Meaning / permitted values |
|---|---|
| `fetch_id` | Primary key |
| `run_id` | → `collection_runs` |
| `purpose` | `listing_page`, `posting_detail`, `resource`, `probe` (`FETCH_PURPOSES`, `CHECK`) |
| `requested_url` | What was asked for |
| `final_url` | Where it ended up after redirects |
| `method` | Always `GET` |
| `transport` | `httpx` or `browser` (`CHECK`) |
| `attempt_no` | Retry attempt number within this fetch |
| `started_at_utc`, `ended_at_utc`, `duration_ms` | Timing of this attempt |
| `http_status` | HTTP status, null if no response arrived |
| `http_version` | e.g. `HTTP/2` |
| `response_state` | `complete`, `partial`, `no_response` (`RESPONSE_STATES`, `CHECK`) |
| `redirect_count`, `redirect_chain_json` | Every hop, with its status, `Location` and timestamp. Redirects are followed manually so each hop is guarded and preserved. |
| `request_headers_json`, `response_headers_json` | Headers verbatim, except that credential-bearing headers are stored as `<redacted>` (`net/client.py::SENSITIVE_HEADERS`) |
| `content_encoding` | The `Content-Encoding` header as received |
| `received_bytes` | Bytes actually read |
| `artifact_id` | → `artifacts`; null when no payload was produced |
| `access_control_signal` | Non-null when the source answered with an access-control mechanism, e.g. `aws-waf-challenge`, `rate-limited`, `cf-challenge`, `captcha`. **This is collection uncertainty, never evidence of absence.** |
| `retry_after` | `Retry-After` header value, honoured by the request budget |
| `failure_kind` | `timeout_connect`, `timeout_read`, `connect`, `dns`, `tls`, `transport`, `http_error`, `access_control`, `blocked_destination`, `budget_exhausted`, `too_many_redirects` |
| `failure_detail` | Prose detail |

Indexes: `(run_id, purpose)`, `(requested_url, started_at_utc)`,
`started_at_utc`.

## `extractions`

One row per (artifact × parser × contracts). An extraction is a pure function of
those inputs, so the same combination is stored once; a parser upgrade produces a
**new** row and never rewrites the old interpretation.

| Column | Meaning / permitted values |
|---|---|
| `extraction_id` | Primary key |
| `artifact_id` | → `artifacts` |
| `parser_name` | `pageup_listing` or `pageup_detail` |
| `parser_version` | `PARSER_VERSION` at extraction time |
| `contract_version` | `CONTRACT_VERSION` at extraction time |
| `text_contract_version` | `TEXT_CONTRACT_VERSION` at extraction time |
| `extracted_at_utc` | When the parse ran. **Not** when the page was retrieved. |
| `run_id` | → `collection_runs`; null for offline reprocessing |
| `status` | `ok`, `partial`, `failed` (`CHECK`) |
| `output_json` | The full extraction output |
| `warnings_json` | Non-fatal observations, e.g. an unknown source label preserved |
| `failure_detail` | Why the parse failed |
| `decode_encoding` | Encoding used to turn bytes into text |
| `decode_strategy` | `strict` or `replace` |
| `decode_error_count` | Number of replacement characters when `replace` was used; 0 otherwise |

Unique on `(artifact_id, parser_name, parser_version, contract_version,
text_contract_version)`.

## `postings`

The identity of one advertisement.

| Column | Meaning / permitted values |
|---|---|
| `posting_id` | Primary key |
| `source_id` | → `sources` |
| `source_namespace` | Denormalised namespace (`rowan.pageup`) |
| `external_job_id` | The identifier the **source** publishes (PageUp job number) |
| `first_discovered_at_utc` | When we first saw it |
| `first_discovered_run_id` | → `collection_runs` |
| `discovery_basis` | `baseline` (present at the first qualified collection — **not** newly posted) or `observed-new` (first appeared after a qualified baseline existed) (`CHECK`) |

**Unique on `(source_namespace, external_job_id)`. This pair is the only thing
that identifies a posting** — never the title, never description similarity.

## `posting_urls`

Every URL a posting has been reached by.

| Column | Meaning |
|---|---|
| `posting_url_id` | Primary key |
| `posting_id` | → `postings` |
| `url` | Absolute URL |
| `role` | `canonical-detail` (the final URL of a successful detail retrieval) or `listing-link` (an href seen in a listing row) |
| `provenance` | `detail_page` or `listing_entry` |
| `first_seen_at_utc`, `last_seen_at_utc` | Observation window for this URL |
| `seen_count` | How many times it has been seen |

Unique on `(posting_id, url, role)`.

## `listing_scans`

One complete pagination traversal.

| Column | Meaning / permitted values |
|---|---|
| `scan_id` | Primary key |
| `run_id` | → `collection_runs` |
| `scan_ordinal` | 1, 2, 3 within the run; unique with `run_id` |
| `scan_role` | `discovery`, `verification`, `reconciliation` (`SCAN_ROLES`, `CHECK`) |
| `started_at_utc`, `ended_at_utc` | Traversal boundaries |
| `pages_requested`, `pages_ok`, `pages_failed` | Page counters |
| `last_page_number` | Number of pages in the traversal |
| `termination_reason` | `no_more_link`, `empty_validated_page`, `max_pages`, `loop_detected`, `fetch_failure`, `structure_unrecognized` (`SCAN_TERMINATION`). Only the first two are legitimate (`qualify.LEGITIMATE_TERMINATION`). |
| `entries_seen` | Row occurrences across *all* sections, including the duplicated `recent-jobs` section |
| `unique_ids_seen` | Distinct advertisements in the authoritative section. This is the inventory figure. |
| `unresolved_candidates` | Rows that did not resolve to a source job identifier |
| `duplicate_occurrences` | `entries_seen − unique_ids_seen`; expected to be non-zero because the page repeats itself |
| `source_reported_total` | The total implied by the source's own `more-link` count: remaining-after-page-1 **plus** the rows on page 1 |
| `encountered_ids_json` | Sorted list of the identifiers seen |

## `listing_scan_assessments`

The versioned completeness verdict. **Absence analysis reads this, not the
scan.**

| Column | Meaning |
|---|---|
| `assessment_id` | Primary key |
| `scan_id` | → `listing_scans` |
| `rules_version` | `QUALIFICATION_RULES_VERSION` under which this verdict was reached |
| `assessed_at_utc` | When the verdict was formed |
| `qualified` | 0/1 (`CHECK`) |
| `reason` | Which checks failed, when not qualified |
| `checks_json` | Every individual check with `name`, `passed` (true/false/null for not-applicable), `detail` and `evidence`. For `no_access_control_response` the evidence separates `pages_unrecovered` (which fail the check) from `pages_challenged_then_recovered` (which do not, because coverage is intact) |

Unique on `(scan_id, rules_version)` — a rules change adds a second verdict
beside the first rather than overwriting it.

## `listing_pages`

One retrieved listing page within a scan.

| Column | Meaning |
|---|---|
| `listing_page_id` | Primary key |
| `scan_id` | → `listing_scans`; unique with `page_number` |
| `page_number` | 1-based |
| `page_items` | The `page-items` value requested |
| `url` | The URL requested for this page |
| `fetch_id`, `extraction_id` | → `fetches`, `extractions` |
| `structure_recognized` | 1 when the PageUp listing structure was found; null when there was no extraction |
| `empty_result_validated` | 1 when a zero-row page carried the *intact* empty-result template (heading, column headers, both tbodies) — as opposed to an error page that happened to yield no rows |
| `entry_count` | Row occurrences on this page across sections |
| `unique_id_count` | Distinct advertisements in the authoritative section on this page |
| `more_link_url` | The next-page URL the source itself offered |
| `more_link_remaining` | The source's count: advertisements remaining **after** this page, not the total |
| `page_signature` | SHA-256 over this page's ordered identifiers plus its more-link URL; used to detect a pagination loop |
| `observed_at_utc` | When the page was recorded |

## `listing_entries`

Every row occurrence on a listing page, in every section.

| Column | Meaning / permitted values |
|---|---|
| `listing_entry_id` | Primary key |
| `listing_page_id`, `scan_id`, `run_id`, `extraction_id` | Provenance |
| `section` | `search-results` (authoritative) or `recent-jobs` (the duplicated "Current Opportunities" repeat) |
| `position_in_section` | 1-based order within the section |
| `page_number` | The page it appeared on |
| `href_raw`, `href_resolved` | The link as published and as resolved |
| `external_job_id` | Identifier parsed from the href |
| `posting_id` | → `postings`; null when the row did not resolve |
| `resolution_state` | `resolved`, `unresolved_id` (a job link whose href did not match the expected pattern), `unrecognized_row` (content but no job link) (`CHECK`) |
| `resolution_detail` | Why it did not resolve |
| `title_text` | The link text as displayed |
| `summary_text`, `summary_html` | The listing's own short summary, when the row has one. **This is the listing summary, not the advertisement body** — see `docs/EXTRACTION_CONTRACT.md`. |
| `displayed_metadata_json` | What the listing row itself showed: `location`, `close_date` (text plus the `<time datetime>` machine value), and any unknown labelled span |
| `observed_at_utc` | When this occurrence was seen |

Indexes: `(scan_id, section)`, `(posting_id, observed_at_utc)`,
`external_job_id`.

## `posting_versions`

One distinct state of an advertisement's content, under one **comparison
lineage** — the triple `(parser_version, contract_version,
text_contract_version)`. Identical content re-observed tomorrow reuses today's
row — so a history of A → B → A is **three observations across two versions**.

| Column | Meaning / permitted values |
|---|---|
| `posting_version_id` | Primary key |
| `posting_id` | → `postings` |
| `parser_version` | `PARSER_VERSION` that produced this reading (added by migration 3; backfilled on existing rows). Part of the comparison lineage |
| `contract_version` | `CONTRACT_VERSION` that defined "different" here |
| `text_contract_version` | `TEXT_CONTRACT_VERSION` that produced `description_text` |
| `content_fingerprint` | The composite fingerprint: the comparison lineage, the title, and the three fingerprints below (`extract/fingerprint.py::content_fingerprint`). Because the lineage is hashed in, a version produced under a different parser or contract can never collide with this one |
| `description_text_fingerprint` | Fingerprint of the readable text |
| `description_html_fingerprint` | Fingerprint of the description markup |
| `metadata_fingerprint` | Fingerprint of the ordered, labelled source fields |
| `title` | The advertisement heading, verbatim |
| `description_html` | The advertisement body markup |
| `description_html_kind` | `source-substring` (a byte-for-byte slice of the archived document, and only after the slice has been rendered and compared with the parsed element — `pageup_detail.py::_slice_matches_node`), `reserialized` (rebuilt by the parser; semantically equivalent but **not** byte-identical), or `absent` (`CHECK`) |
| `description_text` | The body rendered under the verbatim text contract |
| `first_seen_at_utc` | When this content state was first observed |
| `first_extraction_id`, `first_run_id` | Provenance of the first sighting |

Unique on `(posting_id, contract_version, content_fingerprint)`, and
`idx_versions_lineage` indexes `(posting_id, parser_version, contract_version,
text_contract_version, first_seen_at_utc)`. Because the lineage is folded into
`content_fingerprint`, a parser or text-contract bump produces a **parallel line
of versions** rather than an apparent edit of the existing one: the change
detector, `rowanjobs diff` and the uniqueness constraint all agree about which
versions are comparable (`src/rowanjobs/collect/events.py::_record_content_events`).

## `version_values`

The labelled source fields belonging to one content version. Normalisation is
stored **beside** the source value and never replaces it.

| Column | Meaning / permitted values |
|---|---|
| `version_value_id` | Primary key |
| `posting_version_id` | → `posting_versions` |
| `field_key` | Canonical key (`job_no`, `work_type`, `location`, `categories`, `advertised`, `applications_close`, …). Unknown labels get a slugified key rather than being dropped. |
| `source_label` | The label exactly as the source printed it, e.g. `Applications close:` |
| `ordinal` | 0-based occurrence index when the same label appears more than once |
| `value_text` | The value as displayed, whitespace-normalised for readability |
| `value_html` | The value's markup |
| `field_state` | `present`, `blank` (label found, explicitly empty), `absent` (validated structure, label not present), `unresolved` (structure not validated, so we cannot say) (`FIELD_STATES`, `CHECK`) |
| `origin` | Where the value came from, e.g. `detail_labelled` |
| `known_label` | 1 when the adapter recognises the label; 0 means a *new source field* was preserved rather than lost |
| `normalized_json` | Derived interpretation, e.g. multivalue split on `;`. **Locations are never split on commas** — "Glassboro, New Jersey" is one place. |
| `date_parse_state` | `parsed`, `unparsed`, `invalid`, `absent` (`DATE_PARSE_STATES`, `CHECK`); null for non-date fields |
| `source_precision` | What the *display* committed to: `date`, `minute`, `second`, `unknown` (`DATE_PRECISIONS`). A display the parser does not recognise keeps `unknown` — it is never promoted to `minute` on the strength of the machine value |
| `source_tz_text` | The timezone wording printed next to the value, e.g. `Eastern Daylight Time` |
| `source_machine_value` | The `<time datetime="…">` attribute as published |
| `parsed_utc` | Best-effort UTC interpretation, clearly derived. **Null when the display was date-only**, so a date-only value is never reported as an instant, and **null when the display could not be read at all** (`date_parse_state='unparsed'`, `source_precision='unknown'`) — an unrecognised display such as "Ongoing" leaves the machine value uninterpreted rather than publishing an instant the source never showed (`src/rowanjobs/extract/dates.py`). |
| `parsed_local_date` | The `America/New_York` calendar date implied |

Unique on `(posting_version_id, field_key, ordinal, origin)`.

## `posting_observations`

One check of one advertisement's detail URL at one moment. This is the central
evidence table for presence over time.

| Column | Meaning / permitted values |
|---|---|
| `observation_id` | Primary key |
| `run_id` | → `collection_runs` |
| `posting_id` | → `postings`; null when the identity was not yet registered |
| `expected_external_job_id` | The identifier we asked for |
| `requested_url` | The URL we asked for |
| `fetch_id` | → `fetches` |
| `observed_at_utc` | **When the retrieval started** — the source-retrieval time, not the parse time |
| `observed_external_job_id` | The identifier the page actually displayed |
| `identity_state` | `match`, `mismatch`, `absent_on_page`, `not_observed` (`IDENTITY_STATES`, `CHECK`) |
| `availability_state` | See the table below (`AVAILABILITY_STATES`). Constrained in the database by `trg_observation_availability_insert` (migration 4), which mirrors `constants.AVAILABILITY_STATES` and aborts an insert carrying any other value; a test asserts the two stay in step |
| `availability_detail` | Prose detail |
| `redirect_class` | `none`, `to_listing`, `to_other_job`, `same_job_canonicalised`, `to_other` |
| `extraction_id` | → `extractions`; the reading this observation was made under |
| `posting_version_id` | → `posting_versions`; set only when content was captured |
| `checked_because` | `listed`, `historical-daily`, `historical-weekly`, `manual` (`CHECK_REASONS`) |
| `conflicts_json` | Disagreements preserved **unresolved**: job number in the label vs. in the span, a field presented more than once with different values, an identity mismatch, and `closure_signal_with_content` — a page that showed both a closure-like notice and a complete advertisement body, where the content is kept and the notice preserved rather than the description being thrown away. Nothing here is reconciled into one "correct" value. |

Deliberately **not** unique on `(posting_id, date)`: several observations per day
are legitimate and all are preserved. Indexes: `(posting_id, observed_at_utc)`,
`run_id`, `(expected_external_job_id, observed_at_utc)`.

`extraction_id` and `posting_version_id` are the only *evidence* columns any
tool rewrites, and only outside ingestion: `rowanjobs reprocess --relink`
(opt-in, off by default) repoints them at a newer reading and thereby loses the
record of which interpretation the observation was originally made under. See
`docs/OBSERVATION_SEMANTICS.md` §11.

### `availability_state` values

| Value | Meaning | Class |
|---|---|---|
| `content_captured` | A real advertisement body was retrieved | Substantive (`SUBSTANTIVE_AVAILABILITY`) |
| `explicit_closure` | The source displayed a closed/unavailable template | Terminal (`TERMINAL_AVAILABILITY`) |
| `not_found` | The source returned a definite not-found | Terminal |
| `redirected_to_listing` | The detail URL bounced to the general listing | Terminal |
| `redirected_to_other_job` | The detail URL bounced to a different advertisement | Conflict; the destination's content is **not** assigned to the expected posting |
| `identity_mismatch` | The body parsed but the displayed job id disagreed | Conflict, as above |
| `access_control_challenge` | WAF/bot challenge | **Uncertainty** (`UNCERTAIN_AVAILABILITY`) — never absence |
| `retrieval_failed` | Network or HTTP failure | **Uncertainty** — never absence |

`TERMINAL_AVAILABILITY` states are the only ones that advance the
demotion streak towards weekly rechecking. Uncertainty never does.

`explicit_closure` requires a page with **no advertisement body** plus a closure
signal; a recognised page carrying no body and no such signal is `not_found`.
A closure notice *alongside* a captured body is neither: the observation is
`content_captured` and the contradiction is recorded in `conflicts_json`
(`src/rowanjobs/collect/details.py`).

## `resource_links`

Links found in an advertisement, with the decision **this extraction** made
about each. Rows are written on every content capture, not only when a version
is created (`Repository.record_resource_links`): a link's classification belongs
to the reading that produced it, so a widened collection scope is visible
immediately instead of waiting for the advertisement's content to change.
Extractions are deduplicated by `(artifact, parser, contracts)`, so an unchanged
page re-observed tomorrow reuses the same extraction and adds no rows.

| Column | Meaning / permitted values |
|---|---|
| `resource_link_id` | Primary key |
| `posting_version_id` | → `posting_versions`; the version this capture produced |
| `extraction_id` | → `extractions`; the reading that classified the link, and the row's identity |
| `parent_kind` | `posting_description` (inside the body) or `job_content` (elsewhere in the job container) |
| `url_raw`, `url_resolved` | As published and as resolved against the page URL |
| `link_text` | Anchor text, truncated to 300 characters |
| `rel` | The `rel` attribute, if any |
| `position` | Order of appearance |
| `classification` | `job_document`, `apply_workflow`, `internal_nav`, `external`, `mailto`, `anchor`, `unknown` (`RESOURCE_CLASSIFICATIONS`) |
| `collection_decision` | `fetch` or `exclude` (`CHECK`) |
| `exclusion_reason` | Why it was excluded — recorded so a later question about what was *not* followed is answerable from the archive |
| `first_seen_at_utc` | First sighting |

Unique on `(extraction_id, url_raw, position)` since migration 5, which rebuilt
the table (existing rows kept their ids and their original decisions). In v1 a
link is fetched only when this extraction classified it `job_document` **and**
it is hosted on Rowan's own domain — `rowan.edu` or any `*.rowan.edu` host
(`extract/pageup_detail.py::classify_link`). A `job_document` on a third-party
host is still recorded, with `collection_decision='exclude'` and the reason
stored. Application workflow links are **never** followed.

`DetailCollector._collect_resources` retrieves exactly the links this extraction
marked `fetch` and looks the `resource_link_id` up by `(extraction_id,
url_raw)`, so the stored classification and what was actually retrieved cannot
disagree.

## `resource_observations`

One retrieval of one linked document. It carries its **own** retrieval time,
independent of its parent page.

| Column | Meaning / permitted values |
|---|---|
| `resource_observation_id` | Primary key |
| `run_id` | → `collection_runs` |
| `url_resolved` | What was fetched |
| `fetch_id` | → `fetches` |
| `observed_at_utc` | When the resource itself was retrieved |
| `outcome` | `captured`, `too_large`, `unsupported`, `failed`, `blocked_destination`, `not_modified` (`RESOURCE_OUTCOMES`) |
| `outcome_detail` | Prose detail |
| `artifact_id` | → `artifacts` |
| `content_sha256` | Hash of the retrieved bytes |
| `media_type`, `byte_length` | As received |
| `text_extraction_state` | `not_attempted` (default), `ok`, `failed`, `unavailable`. Text extraction is secondary; the bytes are always preserved. |
| `text_content` | Extracted text for textual media types only |

## `resource_associations`

Which posting observation a resource retrieval served. One retrieval can serve
several parents inside a run, so the association keeps the true retrieval
timestamp intact instead of duplicating the download.

| Column | Meaning |
|---|---|
| `resource_association_id` | Primary key |
| `resource_observation_id` | → `resource_observations` |
| `observation_id` | → `posting_observations` |
| `resource_link_id` | → `resource_links` |
| `created_at_utc` | When the association was made |

Unique on `(resource_observation_id, observation_id)`.

## `work_queue` *(mutable)*

Per-run work items, so an interrupted harvest resumes instead of restarting.

| Column | Meaning / permitted values |
|---|---|
| `work_id` | Primary key |
| `run_id` | → `collection_runs` |
| `kind` | `posting_detail` or `resource` (`WORK_KINDS`, `CHECK`) |
| `work_key` | The external job id (or resource key) |
| `payload_json` | URL, posting id, `checked_because` |
| `priority` | Lower runs first: 10 newly discovered, 50 listed, 80 historical-daily, 90 historical-weekly |
| `state` | `pending`, `in_progress`, `done`, `failed`, `abandoned` (`WORK_STATES`, `CHECK`) |
| `attempts`, `max_attempts` | Attempt accounting |
| `claim_token`, `claimed_at_utc` | Who currently holds the item |
| `updated_at_utc` | Last state change |
| `last_error` | Most recent failure detail |

Unique on `(run_id, kind, work_key)`.

## `recheck_policy` *(mutable)*

How often a no-longer-listed posting is re-checked.

| Column | Meaning / permitted values |
|---|---|
| `posting_id` | Primary key, → `postings` |
| `tier` | `daily` or `weekly` (`CHECK`) |
| `consecutive_terminal_observations` | Streak of terminal outcomes. Reset by a listing appearance or a content capture; **not advanced by uncertainty** |
| `last_checked_at_utc` | Last check. Also the ordering key for historical rechecks: `_queue_historical` takes unlisted postings **least-recently-checked first**, so the tail of the list is reached instead of the same first `max_historical_rechecks_per_run` every run. `_update_recheck_policy` walks a run's observations chronologically, so this value cannot move backwards |
| `next_due_at_utc` | When a weekly-tier posting is due again |
| `reason` | Why it is in this tier, in prose |
| `updated_at_utc` | Last update |

## `deployments` *(mutable/append)*

Written by `rowanjobs record-deployment`.

| Column | Meaning |
|---|---|
| `deployment_id` | Primary key |
| `recorded_at_utc` | When recorded |
| `app_version`, `app_revision`, `git_dirty` | What was deployed |
| `python_version` | Interpreter version |
| `sqlite_runtime_json` | The SQLite runtime evidence |
| `host`, `os_user` | Where |
| `install_path`, `data_root` | Paths in use |
| `units_json` | The systemd units expected to exist |
| `notes` | Free text from `--note` |

## `backups` *(mutable)*

| Column | Meaning / permitted values |
|---|---|
| `backup_id` | Primary key |
| `created_at_utc` | Row creation |
| `kind` | `daily`, `weekly`, `monthly`, `manual`, `predeploy` (`BACKUP_KINDS`) |
| `path` | The snapshot file |
| `manifest_path` | The sidecar manifest JSON |
| `snapshot_at_utc` | When the snapshot was taken |
| `schema_version` | Schema version inside the snapshot |
| `app_version` | Application version that took it |
| `last_run_id` | The highest run id present in the snapshot |
| `artifact_count`, `posting_count` | Contents at snapshot time |
| `file_bytes`, `sha256` | Size and checksum of the file |
| `verification_state` | `OK` (verified before promotion) or `SKIPPED` (`verify_after_backup = false`). A snapshot that fails verification is never promoted and never recorded here. **Only `OK` counts:** rotation may not drop an older snapshot on the strength of a `SKIPPED` one, and `latest()` — the restore source — selects `OK` only (`src/rowanjobs/ops/backup.py`) |
| `verification_detail`, `verified_at_utc` | Verification outcome |
| `offhost_state` | `UNCONFIGURED` (default), `VERIFIED`, `FAILED` — compare `PROTECTION_STATES` |
| `offhost_target`, `offhost_detail` | Destination and result |
| `pruned_at_utc` | Set when rotation removed the file |

## `presence_events` *(derived, rebuildable)*

| Column | Meaning / permitted values |
|---|---|
| `presence_event_id` | Primary key |
| `posting_id` | → `postings` |
| `event_kind` | `first_observed`, `listed`, `absent_qualified`, `reappeared`, `content_changed`, `coverage_gap` (`PRESENCE_EVENT_KINDS`) |
| `rules_version` | `EVENT_RULES_VERSION` that produced this row |
| `run_id`, `scan_id`, `observation_id` | The evidence behind it |
| `from_posting_version_id`, `to_posting_version_id` | For `content_changed` |
| `interval_start_utc`, `interval_end_utc` | **The event happened somewhere inside this interval.** An exact event time is never manufactured; `interval_end_utc` is required, `interval_start_utc` may be null when there is no earlier bound. |
| `slot_local_date` | The scheduled slot's `America/New_York` date — the key the two-day absence rule counts |
| `comparability_group` | From the source config; absence history is only comparable within one group |
| `evidence_json` | Why this event was emitted, including the qualification rules version and explanatory notes. For `content_changed` it carries the full comparison lineage (`parser_version`, `contract_version`, `text_contract_version`) the two versions shared |
| `created_at_utc` | When derived |

Unique index on `(posting_id, event_kind, rules_version, interval_end_utc,
COALESCE(run_id,-1))`, so re-deriving a run is idempotent.

## `coverage_gaps` *(derived, rebuildable)*

Recorded whenever the collector cannot honestly claim complete coverage.

| Column | Meaning |
|---|---|
| `coverage_gap_id` | Primary key |
| `kind` | e.g. `no_qualified_discovery`, `listing_set_unreconciled`, `historical_recheck_deferred`, `listing_disagreement_within_run` (an advertisement listed by one qualified traversal but missing from the final one — recorded *instead of* an `absent_qualified` event, because absence may not contradict this run's own evidence) |
| `scope` | `listing`, `detail`, … |
| `run_id`, `scan_id`, `posting_id` | What it applies to |
| `slot_local_date` | The affected slot date |
| `detected_at_utc` | When it was detected |
| `window_start_utc`, `window_end_utc` | The uncovered window, when one is known |
| `detail` | Prose explanation |
| `evidence_json` | Supporting data, e.g. the set comparison |
| `resolved_at_utc` | Set if the gap is later closed; unresolved gaps are what `status` reports |

---

# Views

All defined in `src/rowanjobs/db/migrations/m0002_views.py`. Because they are
views they cannot drift from the evidence, and every one of them exposes the
observation time and the coverage quality behind it.

## `v_qualified_scans`

`listing_scans` joined to a **qualified** assessment and to its run. Columns:
all of `listing_scans`, plus `qualified`, `rules_version`,
`qualification_reason`, `checks_json`, `scheduled_slot_local_date`, `run_kind`.
This is the only scan set that may support an absence claim.

## `v_last_listed`

`posting_id`, `last_listed_at_utc` — the latest appearance of each posting in
any **qualified** scan.

## `v_last_seen_any_scan`

`posting_id`, `last_seen_any_scan_at_utc` — the latest appearance in **any**
scan, qualified or not. Kept separate from `v_last_listed` so a reader can tell
"we saw it" apart from "we saw it in a scan good enough to reason about absence".

## `v_last_captured`

`posting_id`, `last_captured_at_utc` — the latest observation with
`availability_state='content_captured'` and `identity_state='match'`.

## `v_last_observation`

`posting_id`, `last_observed_at_utc` — the latest observation of any kind,
including failures.

## `v_posting_current`

Current state per posting, with freshness and certainty as explicit columns.

| Column | Meaning |
|---|---|
| `posting_id`, `external_job_id`, `source_namespace` | Identity |
| `first_discovered_at_utc`, `discovery_basis` | Origin |
| `last_listed_at_utc` | Last qualified-scan listing appearance |
| `last_seen_any_scan_at_utc` | Last appearance in any scan |
| `last_captured_at_utc` | Last successful content capture |
| `last_observed_at_utc` | Last observation of any kind |
| `current_version_id`, `current_title`, `current_content_fingerprint` | The most recent content version referenced by an observation |
| `current_content_observed_at_utc` | When that content was observed |
| `last_availability_state`, `last_identity_state`, `last_check_at_utc` | The most recent check, whatever it produced |
| `content_freshness` | `checked`, `carried-forward`, `carried-forward-uncertain`, `never-captured` — see below |
| `version_count`, `observation_count` | History size |

The `current_*` columns come from the most recent observation that produced a
content version **and** whose `identity_state` is `match` — the same requirement
`v_last_captured` applies — so the two views cannot disagree about whether
content was ever captured.

**`content_freshness` is the contract that prevents stored content from being
reported as freshly retrieved:**

| Value | Meaning |
|---|---|
| `never-captured` | No content has ever been captured for this posting. **Tested first**, because "carried forward" presupposes there is content to carry |
| `carried-forward-uncertain` | Content exists, and the most recent check was `access_control_challenge` or `retrieval_failed`; we do not know the current state |
| `checked` | The most recent observation is the one that produced this content |
| `carried-forward` | The content predates the most recent check (which produced no content) |

The `CASE` in `m0002_views.py` is evaluated in that order.

## `v_posting_history`

The full observation history joined to the content version it referenced and to
its fetch. Columns: `observation_id`, `posting_id`, `external_job_id`, `run_id`,
`run_kind`, `scheduled_slot_local_date`, `observed_at_utc`, `identity_state`,
`availability_state`, `availability_detail`, `redirect_class`,
`checked_because`, `posting_version_id`, `content_fingerprint`,
`description_text_fingerprint`, `title`, `http_status`, `final_url`,
`access_control_signal`.

## `v_listing_presence`

Listing presence per **qualified** scan: the cross product of qualified scans and
postings, with `listed` 0/1. Absence is therefore always scan-scoped — you cannot
ask "was it absent?" without naming the scan that says so. Columns: `scan_id`,
`run_id`, `scheduled_slot_local_date`, `scan_role`, `scan_started_at_utc`,
`scan_ended_at_utc`, `posting_id`, `external_job_id`, `listed`.

## `v_run_health`

One row per run: the `collection_runs` columns plus correlated counts of `scans`,
`qualified_scans`, `fetches`, `failed_fetches`, `access_control_responses`,
`observations`, `captured`, `resources` and `coverage_gaps`. Backs
`rowanjobs runs` and the health JSON.

## `v_content_changes`

`content_changed` presence events joined to both version fingerprints. Columns:
`presence_event_id`, `posting_id`, `external_job_id`, `interval_start_utc`,
`interval_end_utc`, `rules_version`, `from_posting_version_id`,
`to_posting_version_id`, `from_fingerprint`, `to_fingerprint`, `evidence_json`.
The interval is an interval, not an instant.

---

# Relationships

```
sources ──< source_configs
   └──< postings ──< posting_urls
            ├──< posting_versions ──< version_values
            │           └──< resource_links
            ├──< posting_observations ──> posting_versions
            │           └──< resource_associations >── resource_observations
            ├──< listing_entries
            ├──< presence_events
            └──── recheck_policy (1:1)

collection_runs ──< listing_scans ──< listing_pages ──< listing_entries
       │                  └──< listing_scan_assessments
       ├──< fetches ──> artifacts
       ├──< posting_observations
       ├──< resource_observations
       ├──< work_queue
       └──< coverage_gaps

artifacts ──< extractions ──> (listing_pages | listing_entries |
                               posting_observations | posting_versions |
                               resource_links)
```

Read it as: a run performs scans; a scan retrieves pages; a page yields row
occurrences; a row occurrence names a posting. Separately, a run observes each
posting's detail URL; an observation may produce a content version; a content
version owns its labelled field values. Classified links hang off the
**extraction** that read them (and carry the version they were captured
alongside). Every fetch points at the archived bytes, and every extraction points
at the artifact it interpreted plus the parser and contract versions that
interpreted it.

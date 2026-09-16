# Operations

Day-to-day running of RowanJobs on the collection host.

---

## The schedule

One collection per day at **06:15 America/New_York**.

| Unit | Calendar | Purpose |
|---|---|---|
| `rowanjobs.timer` | `*-*-* 06:15:00 America/New_York` | The daily collection. `Persistent=true`, `AccuracySec=1min`, `RandomizedDelaySec=90`. |
| `rowanjobs.service` | — | `rowanjobs --json collect --kind daily`, `Type=oneshot`, `TimeoutStartSec=3h` |
| `rowanjobs-retry.timer` | `*-*-* 09:15:00` and `*-*-* 13:15:00 America/New_York` | Two retry *windows*. `Persistent=false`. |
| `rowanjobs-retry.service` | — | `rowanjobs --json retry` |

Templates live in `ops/systemd/` with `__VENV__`, `__CONFIG__` and
`__DATA_ROOT__` placeholders to substitute at install time; they are installed as
**user** units and require lingering to be enabled for the collecting user
(`loginctl enable-linger`), otherwise they will not run without an active login.
`rowanjobs doctor` checks for this.

The configured slot comes from `[schedule]` in the config file; the calendar
expression is derived by `src/rowanjobs/ops/schedule.py::calendar_expression` and
validated against `systemd-analyze calendar`. If you change `[schedule]`, you
must regenerate and reinstall the timer units — nothing does that automatically,
and `status` will show the mismatch between the configured expression and the
timer's actual `TimersCalendar`.

**`Persistent=true` on the daily timer means a missed activation triggers a
*current* collection after recovery. It does not, and cannot, reconstruct the
snapshot that was missed: that day stays a coverage gap.**

## Retry policy

`rowanjobs retry` (`src/rowanjobs/cli.py::cmd_retry`) is deliberately
conservative. It:

1. Computes today's slot date from `[schedule]`.
2. Reads the `daily` and `retry` runs recorded against that slot.
3. Exits 0 with "no scheduled collection recorded for slot …" if there is none —
   a retry never *starts* a day's collection.
4. Exits 0 if any run for the slot already succeeded.
5. Exits 2 if the slot has used its `max_retries_per_slot` (default 2) attempts
   and is still unresolved, leaving it for the next scheduled collection.
6. Otherwise runs a collection with `run_kind='retry'`, `parent_run_id` set to
   the day's daily run, and `attempt_no` incremented.

**A retry belongs to its parent's scheduled slot.** It never creates an extra
daily slot, and because presence events key on `slot_local_date`, same-day
retries can never add up to a second daily absence confirmation
(`docs/OBSERVATION_SEMANTICS.md` §4).

Within a single run there is a second, lower-level retry layer: transient HTTP
failures (connect/read timeout, DNS, TLS, 5xx, 429) are retried up to
`network.max_retries` with exponential backoff, honouring `Retry-After`. Failed
detail items are returned to the work queue until `max_attempts` (2 for detail
work) is exhausted.

---

## Reading `rowanjobs status`

```sh
rowanjobs status
```

Reads the archive **read-only** and prints, in order:

- `collection` — the overall state and the last attempt, with its outcome and
  duration.
- `last qualified discovery` — when the most recent traversal that *qualified*
  ended, its final count, what the source itself reported, and how many duplicate
  row occurrences were ignored. **`none` here means no absence conclusion can be
  drawn from any run yet.**
- `last content capture` — when an advertisement body was last successfully
  retrieved, and how many captured observations exist in total.
- posting / version / observation counts, split by `discovery_basis`.
- `failures` — retrieval, access-control, extraction (failed/partial), resource
  exceptions, identity mismatches. These are **lifetime** totals across the whole
  archive, not just the last run.
- `coverage gaps` — unresolved gaps grouped by kind, with the most recent.
- `archive` — database size, journal mode, SQLite version, payload compression
  ratio, artifact deduplication (artifacts vs. payload-bearing fetches), free
  disk.
- `backup (local)` and `backup (offhost)` — reported **separately** because they
  fail independently.
- `restore check` — the last full restore-and-query verification.
- `notifications` — `CONFIGURED` or `UNCONFIGURED`.
- `schedule` — the configured calendar expression, the next activation systemd
  actually reports, and whether lingering is enabled.
- `note:` lines — standing caveats, including the host-down one below.

`--no-timer` skips systemd inspection (useful when running as a different user or
inside a container).

### `status --json`

Both forms work; `--json` is accepted before or after the subcommand
(`src/rowanjobs/cli.py::_common_options`):

```sh
rowanjobs --json status
rowanjobs status --json
```

The systemd units use the pre-subcommand form
(`rowanjobs --json collect --kind daily`).

The same payload is written atomically to `<data_root>/runtime/health.json` after
every collection (`src/rowanjobs/ops/health.py::write_health`,
`ops/atomic.py`), so an external observer can read the last known state without
running the CLI at all.

---

## Exit codes

Defined in `src/rowanjobs/cli.py`; the health-derived subset in
`src/rowanjobs/ops/health.py::exit_code_for`.

| Code | Name | Meaning | Action |
|---|---|---|---|
| `0` | OK | Success, or an ordinary no-op (nothing to retry, nothing to do) | None |
| `1` | FAILED | Collection failed: discovery could not be completed, or the run raised | Investigate; the next scheduled run will try again |
| `2` | DEGRADED | Useful evidence collected, but with a coverage exception: no traversal qualified, listing sets did not reconcile, detail retrievals failed or were inconclusive, or the source applied an access-control challenge | Read `errors` / `coverage` in the run output; absence conclusions are suppressed for that run |
| `3` | PROTECTION | Collection was fine but protection is degraded: the backup failed, or there is no recent verified snapshot | Fix the backup before the next run; ingestion is unaffected |
| `4` | LOCKED | Another collector held the lock; **nothing was attempted** | Usually benign (an overlapping manual run). Not a source failure and not an empty collection |
| `5` | USAGE | Usage or configuration error | Fix the command line or the config file |

`SuccessExitStatus=0 2 3` in both service units: 2 and 3 mean useful evidence was
still collected, so systemd should not mark the unit failed. Only 1, 4 and 5 are
real failures from the timer's point of view.

`exit_code_for` maps health to a code: collection state `FAILED` → 1;
`DEGRADED` or `UNKNOWN` → 2; otherwise, local backup state in
`FAILED`/`DEGRADED`/`UNPROTECTED` or off-host state `FAILED` → 3; else 0. Note
that an `UNCONFIGURED` off-host state does **not** produce exit 3 — it is
reported honestly but is not treated as a failure of something that was never
set up.

---

## Where the logs go

The collector does not write its own log files. It emits structured JSON on
stdout (`rowanjobs --json collect`), and the service units set
`StandardOutput=journal`, `StandardError=journal`, with
`SyslogIdentifier=rowanjobs` and `rowanjobs-retry`. So:

```sh
journalctl --user -u rowanjobs.service -n 200 --no-pager
journalctl --user -u rowanjobs.service --since "2026-09-16" --no-pager
journalctl --user -t rowanjobs -f
journalctl --user -u rowanjobs-retry.service -n 100 --no-pager
systemctl --user list-timers 'rowanjobs*'
```

`<data_root>/logs/` exists in the layout and is reserved, but the collector
currently writes nothing into it; journald is the log of record. The durable
machine-readable state is `<data_root>/runtime/health.json` plus the
`collection_runs` rows themselves (`counts_json`, `errors_json`,
`coverage_json`), which outlive any journal rotation.

---

## Troubleshooting

### The source applied an access-control challenge

**Symptom:** exit 2; `errors` contains `{"kind": "access_control", ...}`;
`status` shows a non-zero access-control failure count; observations for that run
carry `availability_state='access_control_challenge'`; the listing scan failed
the `no_access_control_response` check and did not qualify.

**What it is:** AWS WAF answering `HTTP 202` with `x-amzn-waf-action: challenge`
and an empty body. Observed during the source audit after roughly seven requests
in about ninety seconds; it cleared after about eleven minutes of quiet
(`docs/SOURCE_ADAPTER_AUDIT.md`).

**What the collector already did:** widened its inter-request interval by 1.6×
per challenge, waited 45 s → 90 s → 180 s, optionally attempted the ordinary
browser challenge path, and stopped requesting after
`max_consecutive_challenges` rather than hammering the source.

**What to do:**

1. **Do not** read the challenged observations as removals. They are
   uncertainty, and the code already refuses to treat them otherwise.
2. Wait. The scheduled retry windows at 09:15 and 13:15 exist for exactly this.
3. If it recurs daily, raise `network.min_interval_seconds` (e.g. to 3.0) and/or
   lower `collection.max_historical_rechecks_per_run`. Fewer, slower requests.
4. If the browser step is wanted and unavailable, install the extra:
   `uv sync --all-extras && uv run playwright install chromium`. It is optional;
   collection remains correct without it.
5. Never respond by rotating user agents, proxies or IPs. The audit showed the
   challenge applies regardless of user agent, and evading it is outside this
   project's access policy (`docs/SECURITY.md`).

### Lock contention

**Symptom:** exit 4, outcome `lock_contention`, message naming the holding pid,
host and acquisition time.

**What it is:** another collector already holds
`<data_root>/runtime/collector.lock`. **Nothing was attempted** — this is neither
a source failure nor a collection that found nothing.

**What to do:**

```sh
cat ~/.local/share/rowanjobs/runtime/collector.lock     # pid / host / since
systemctl --user status rowanjobs.service
```

If a manual run overlapped the timer, wait for it to finish. The lock is an OS
`flock`, so if the holding process has died the kernel has already released it —
there is no stale lock to clear, and you should never delete the lock file to
"fix" contention.

### A run that did not qualify

**Symptom:** exit 2; `errors` contains `{"kind": "scan_not_qualified", ...}`;
`coverage.absence_analysis_supported` is `false`; a `no_qualified_discovery` or
`listing_set_unreconciled` coverage gap was recorded.

**What it means:** every positive observation from that run is kept and is
trustworthy. Only *absence* conclusions are suppressed, for that run.

**What to do:** find out which check failed.

```sh
rowanjobs --json status | python3 -m json.tool | less
sqlite3 ~/.local/share/rowanjobs/rowanjobs.db \
  "SELECT s.scan_id, s.scan_role, a.qualified, a.reason, a.checks_json
     FROM listing_scans s JOIN listing_scan_assessments a USING(scan_id)
    WHERE s.run_id = (SELECT MAX(run_id) FROM collection_runs);"
```

Common causes and responses:

| Failing check | Likely cause | Response |
|---|---|---|
| `no_access_control_response` | WAF challenge | See above |
| `all_pages_retrieved` / `all_pages_http_200` | Transient network or site trouble | Let the retry window handle it |
| `legitimate_termination` = `max_pages` | The inventory grew past `max_listing_pages`, or pagination misbehaved | Raise the bound only after checking the source really has that many pages |
| `structure_recognized` | **The source's markup changed** | Stop and investigate. Compare a fresh capture against `tests/fixtures/pageup/`, update the adapter, and bump `PARSER_VERSION` |
| `source_count_reconciles` | The source's own count disagrees with what we collected | Check whether the count semantics changed; remember it is *remaining after this page*, not a total |
| `no_pagination_loop` | The site repeated a page | Usually transient; if persistent, the pagination contract changed |
| `empty_result_validated` | A zero-result scan without the intact empty-result template | Treat as an error page, not as "there are no jobs" |

Do **not** relax a qualification check to make a run go green. If a check no
longer expresses the right thing, change it deliberately and bump
`QUALIFICATION_RULES_VERSION`, which records a new verdict beside the old one.

### Disk pressure

`status` prints free space and percentage; `doctor` fails its `disk_space` check
below 1 GiB free. The archive grows with every distinct payload, but identical
pages deduplicate by content hash, and payloads are zlib-compressed at level 9 —
`status` shows the achieved compression ratio and the artifact-to-payload-fetch
ratio.

If space gets tight:

1. Check the backups directory first: `du -sh ~/.local/share/rowanjobs/backups`.
   Rotation keeps 7 daily / 4 weekly / 12 monthly snapshots by default, each a
   full copy of the archive. Lowering `keep_daily` is the safest lever; rotation
   never drops the last verified copy.
2. `<data_root>/source-audit/` holds manual audit captures, not production
   history. They can be moved elsewhere.
3. `<data_root>/exports/` is CLI output and is regenerable.
4. **Never** delete rows from `artifacts` or any other evidence table to reclaim
   space. That destroys history irreversibly (`CLAUDE.md`, "Never" rule 1). If
   the archive genuinely outgrows the volume, move the data root to a larger one
   and point `ROWANJOBS_DATA_ROOT` (or `data_root`) at it.

### Recovery from an interrupted run

An interrupted collection needs **no manual repair**. On the next start, with the
lock held:

1. `Repository.abandon_stale_runs()` closes any run with a null `ended_at_utc` as
   `outcome='aborted'`, with the detail *"run did not finish; the process ended
   before it could be closed. Evidence already written is preserved."* Its
   pending and in-progress queue items become `abandoned`.
2. `Repository.reclaim_orphans(run_id)` returns items left `in_progress` to
   `pending`.
3. The new run proceeds normally.

Everything already written stays written: every `repo` method owns a short
`BEGIN IMMEDIATE` transaction and no transaction is ever held across network I/O
(`docs/ARCHITECTURE.md`). The `aborted` run's fetches, artifacts, observations
and versions all remain valid evidence.

If the interruption was a hard power loss, run the integrity check before
trusting the archive:

```sh
rowanjobs verify              # integrity_check, foreign_key_check, payload hashes
rowanjobs verify --restore    # also restores the latest snapshot to a temp dir
```

If `verify` reports failures, restore from the most recent verified snapshot to a
**separate** location and compare before replacing anything
(`docs/BACKUP_RESTORE.md`). The restore command refuses to write over the live
archive.

---

## The health JSON contract

`rowanjobs --json status` and `<data_root>/runtime/health.json` share one
structure, versioned by `HEALTH_SCHEMA_VERSION` (currently `"1"`), defined in
`src/rowanjobs/ops/health.py`.

Collection health, archive integrity, local backup health and off-host
protection are reported **separately**, because they fail independently and
collapsing them would hide exactly the failure an operator needs to see.

```
health_schema_version   "1"
application             "rowanjobs"
app_version             e.g. "1.0.0"
generated_at_utc        ISO-8601 Z
generated_at_local      America/New_York with zone label
operational_timezone    "America/New_York"

versions
  schema, schema_expected          applied vs. expected migration version
  parser                           PARSER_VERSION
  comparison_contract              CONTRACT_VERSION
  text_contract                    TEXT_CONTRACT_VERSION
  qualification_rules              QUALIFICATION_RULES_VERSION
  event_rules                      EVENT_RULES_VERSION

collection
  state                            RUNNING | NEVER_RUN | HEALTHY | DEGRADED |
                                   FAILED | UNKNOWN
  last_attempt                     run brief (see below), or null
  last_completed                   most recent success-or-partial run brief
  last_qualified_discovery         scan_id, run_id, role, ended_at_utc/local,
                                   slot_local_date,
                                   final_qualified_listing_count,
                                   source_occurrences_seen,
                                   duplicate_occurrences,
                                   source_reported_total     — or null
  last_reconciled_content_capture  at_utc, at_local, total_captured_observations
  run_in_progress                  run brief, or null
  final_qualified_listing_count    the authoritative inventory figure
  union_encountered_last_run       identifiers seen across all traversals
  counts                           postings_total, postings_baseline,
                                   postings_observed_new, posting_versions,
                                   observations, artifacts, fetches,
                                   resource_links, resource_observations,
                                   listing_scans, qualified_scans, runs
  failures                         retrieval, access_control_responses,
                                   extraction_failed, extraction_partial,
                                   resource_exceptions, identity_mismatch
  coverage_gaps[]                  {kind, count, latest_utc} for UNRESOLVED gaps

archive
  state                            VERIFIED | DEGRADED
  database_path, database_bytes
  journal_mode                     "wal" or "delete"
  wal_deviation                    null, or why WAL was refused
  sqlite_runtime                   provider, provider_version, sqlite_version,
                                   sqlite_source_id, using_amalgamation,
                                   wal_safe, wal_evidence
  payload_bytes_uncompressed, payload_bytes_compressed, compression_ratio
  artifact_deduplication           artifacts, fetches_with_payload
  disk                             path, total_bytes, used_bytes, free_bytes,
                                   free_pct

backup
  local                            state (VERIFIED | DEGRADED | UNPROTECTED |
                                   UNCONFIGURED), detail, snapshot_count,
                                   total_bytes, directory, latest{...},
                                   rotation{keep_daily, keep_weekly,
                                   keep_monthly}
  offhost                          state (UNCONFIGURED | VERIFIED | FAILED),
                                   detail, kind, target
  restore_verification             the last full restore check, or null

notifications                      state (UNCONFIGURED | CONFIGURED), detail,
                                   notify_on

schedule                           configured_expression, timezone,
                                   calendar_validation, lingering{enabled,
                                   detail}, units{...}, next_collection_local,
                                   scheduled                 — null with --no-timer

paths                              data_root, database, backups, logs, runtime,
                                   health, config

notes[]                            standing caveats
```

A **run brief** (`last_attempt`, `last_completed`, `run_in_progress`) contains:
`run_id`, `run_uuid`-less summary fields `run_kind`, `attempt_no`,
`parent_run_id`, `slot_local_date`, `is_baseline`, `started_at_utc/local`,
`ended_at_utc/local`, `duration`, `outcome`, `outcome_detail`,
`qualified_scans`, `fetches`, `failed_fetches`, `access_control_responses`,
`observations`, `captured`, `resources`, `coverage_gaps`.

### Reading it correctly

- `collection.state = HEALTHY` requires the last run to have succeeded **and** a
  qualified scan to exist. A successful run with no qualified scan is not
  healthy.
- `last_qualified_discovery = null` means **no absence conclusion is available
  from this archive at all**, whatever the other numbers say.
- `failures.*` are lifetime totals. For "what went wrong today", read
  `last_attempt` and the run's `errors_json`.
- `coverage_gaps` lists only **unresolved** gaps.
- `backup.offhost.state = UNCONFIGURED` is a statement of fact, not a failure:
  nothing is configured, so nothing is claimed. It is not the same as
  `VERIFIED`.

### Future ControlPanel adapter

`HEALTH_SCHEMA_VERSION` exists so another system can consume this payload
without reading RowanJobs' database. The intended shape of a future ControlPanel
adapter is: read `<data_root>/runtime/health.json` (or run
`rowanjobs --json status`), check `health_schema_version`, and surface
`collection.state`, `archive.state`, `backup.local.state` and
`backup.offhost.state` as four **separate** indicators, with
`collection.last_qualified_discovery` and `collection.coverage_gaps` as the
detail view.

**ControlPanel itself is not modified by this project.** No RowanJobs code
writes to it, imports from it, or depends on it; the daily collection has no
dependency on ControlPanel, GitHub, or any interactive process
(`ops/systemd/rowanjobs.service`). Bump `HEALTH_SCHEMA_VERSION` if the payload's
meaning changes, so a consumer can tell.

### Host-down detection

**Host-down detection requires an external observer. A local timer cannot report
while the host is unavailable.** `health.notes` carries this caveat on every
status call (naming the host: *"A local timer cannot report while entropy is
unavailable."*), because it is the one failure mode this design cannot cover
from the inside: if the machine is off, asleep, or
unreachable, there is nothing running to notice or to tell anyone. A stale
`health.json` timestamp is the only local trace, and reading it also requires
the host to be up.

Anything that must alert on "the collection host stopped reporting" has to be
something that is *not this host* — a heartbeat check from elsewhere, a
dead-man's-switch service, or a person looking at `generated_at_utc`. As of this
writing no such external observer is configured, and none is created by this
project.

---

## Routine checks

| When | Command | Looking for |
|---|---|---|
| After any config or code change | `rowanjobs doctor` | All checks ok; schema current; journal mode as expected |
| Daily (or on alert) | `rowanjobs status` | `HEALTHY`, a recent qualified discovery, no new coverage gaps |
| Weekly | `rowanjobs verify` | `integrity_check` ok, no FK violations, all payload hashes verify |
| Weekly (automatic) | restore verification | Driven by `backup.restore_check_interval_days`; result in `runtime/restore-verification.json` |
| After a deployment | `rowanjobs record-deployment --note "..."` | The manifest and `deployments` row are written |

# Architecture

RowanJobs is a single-process, single-source, once-a-day collector writing into
one SQLite database. There is no server, no queue broker, no background worker
and no network service. Everything the archive knows is in one file, and every
claim it makes is traceable to the bytes that justified it.

Two structural ideas carry most of the weight:

1. **Archive first, parse second.** The bytes that came off the wire are stored
   and hashed before any parser sees them. Parsing is then a pure function of
   (archived bytes, parser version, contracts) and can be re-run at any time,
   offline, without inventing a new observation.
2. **Evidence and interpretation are separate tables.** Evidence is append-only.
   Interpretation (presence events, coverage gaps, the projection views) always
   carries the rules version and the evidence ids behind it, and can be dropped
   and rebuilt.

---

## Module map

| Module | Responsibility |
|---|---|
| `src/rowanjobs/__init__.py` | The five versioned contracts, `SOURCE_NAMESPACE`, `__version__` |
| `src/rowanjobs/cli.py` | Command surface and exit codes; reporting commands open the DB read-only |
| `src/rowanjobs/config.py` | TOML loading, typed defaults, `config_hash()` of the *effective* policy |
| `src/rowanjobs/constants.py` | Every enumeration shared between the `CHECK` constraints and the code |
| `src/rowanjobs/paths.py` | `Layout`: data root, backups, logs, runtime, audit, exports; `ensure()` applies 0700 |
| `src/rowanjobs/timeutil.py` | UTC second-precision storage, America/New_York display, `slot_for()` |
| `src/rowanjobs/db/runtime.py` | Inspects the SQLite build actually loaded and decides whether WAL is safe |
| `src/rowanjobs/db/connection.py` | Per-connection pragmas, journal selection, `write()` transactions, read-only handles |
| `src/rowanjobs/db/migrations/` | Ordered, explicit migrations; `m0001_initial` (evidence model), `m0002_views` (projections) |
| `src/rowanjobs/archive/store.py` | Content-addressed payload storage inside the database, zlib-compressed, SHA-256 of the *uncompressed* bytes |
| `src/rowanjobs/net/guard.py` | `UrlPolicy`: scheme/host allowlist, address validation, SSRF refusal |
| `src/rowanjobs/net/budget.py` | The single `RequestBudget` every live request passes through; challenge back-off |
| `src/rowanjobs/net/client.py` | `SourceClient`: manual redirect handling, capped reads, access-control classification, header redaction |
| `src/rowanjobs/net/browser.py` | Optional `ChallengeSolver`: an ordinary Chromium visit to satisfy a WAF challenge |
| `src/rowanjobs/extract/decode.py` | Bytes → text with the decoding outcome recorded, never silently replaced |
| `src/rowanjobs/extract/text.py` | The verbatim markup → text contract (`docs/EXTRACTION_CONTRACT.md`) |
| `src/rowanjobs/extract/slicing.py` | Byte-exact extraction of an element's inner markup from the source document |
| `src/rowanjobs/extract/fingerprint.py` | The four content fingerprints and the comparison contract |
| `src/rowanjobs/extract/dates.py` | Source dates preserved four ways; precision never over-committed |
| `src/rowanjobs/extract/pageup_listing.py` | Listing adapter: sections, rows, `more-link`, empty-result validation, page signature |
| `src/rowanjobs/extract/pageup_detail.py` | Detail adapter: title, labelled fields, description, links, closure signal |
| `src/rowanjobs/collect/lock.py` | `flock`-based mutual exclusion; contention is a first-class outcome |
| `src/rowanjobs/collect/scanner.py` | One complete unfiltered pagination traversal |
| `src/rowanjobs/collect/qualify.py` | The versioned checks a scan must pass to support an absence claim |
| `src/rowanjobs/collect/details.py` | Detail retrieval, identity checking, resource capture |
| `src/rowanjobs/collect/events.py` | Derived presence and content-change events; the absence reporting rule |
| `src/rowanjobs/collect/repo.py` | All persistence; each method owns a short transaction; no network I/O |
| `src/rowanjobs/collect/runner.py` | The daily workflow, outcome classification, recheck policy |
| `src/rowanjobs/ops/backup.py` | Online-backup snapshots, verification, rotation, restore checks |
| `src/rowanjobs/ops/health.py` | The versioned health contract behind `status --json` and `runtime/health.json` |
| `src/rowanjobs/ops/doctor.py` | Pre-flight diagnosis and the deployment manifest |
| `src/rowanjobs/ops/schedule.py` | systemd timer inspection; reports what systemd says, not what the unit intends |
| `src/rowanjobs/ops/notify.py` | Optional command-based alerting; `UNCONFIGURED` rather than invented delivery |
| `src/rowanjobs/ops/atomic.py` | Write-temp / fsync / rename / fsync-dir for `health.json` and manifests |
| `src/rowanjobs/reprocess.py` | Offline re-parsing of archived payloads |
| `src/rowanjobs/export.py` | Datasets with provenance; CSV formula neutralisation |

---

## The daily workflow

`src/rowanjobs/collect/runner.py::Collector._collect` implements six numbered
steps; the module docstring states them, and the code carries the same numbers
as section comments.

1. **Complete discovery traversal, queueing detail retrievals.**
   `ListingScanner.scan(scan_ordinal=1, scan_role="discovery")` walks the
   unfiltered listing from page 1, following the source's own `more-link`, and
   stops only for a reason it can name. Each page is fetched, archived, parsed,
   and its rows recorded as `listing_entries`. The scan is then assessed by
   `collect/qualify.py` and the verdict stored in `listing_scan_assessments`.

2. **Newly discovered advertisements first.**
   `_queue_details` enqueues one `work_queue` item per listed advertisement.
   Advertisements first seen in *this* run get priority 10; already-known listed
   advertisements 50; historical (unlisted but previously known) URLs 80 daily
   or 90 weekly. During a baseline run nothing is treated as "new", so the
   priority stays at the listed level.

3. **Detail pages and their job-specific resources.**
   `_drain_details` claims queue items one at a time and hands them to
   `DetailCollector.collect`, which fetches, archives, decodes, parses, checks
   the displayed job number against the expected one, records a
   `posting_observation`, and — when content was captured — ensures a
   `posting_version` and fetches any `collection_decision='fetch'` resource
   links. A resource downloaded once in a run is reused for other parents via
   `resource_associations`, so its true retrieval time is not duplicated.

4. **Second complete listing traversal.**
   `scan(scan_ordinal=2, scan_role="verification")`, unless
   `collection.verification_scan` is false or `--no-verification` was given.

5. **Compare identifier *sets*, not counts.**
   `set(verification) - set(discovery)` and the reverse. Equal counts with
   different members is a real discrepancy that a count comparison would miss.
   Advertisements that appeared between the passes are collected immediately
   (`_collect_late_arrivals`).

6. **One bounded reconciliation traversal if they disagree.**
   `scan(scan_ordinal=3, scan_role="reconciliation")`. If the sets still do not
   reconcile — or the reconciliation scan itself does not qualify — the run
   records a `listing_set_unreconciled` coverage gap and **suppresses all
   absence-dependent conclusions for the run**. Every positive observation is
   kept.

Afterwards the run selects the **last qualified** traversal as the authoritative
listing set (`_final_qualified`), derives presence and content events
(`EventDeriver.derive_for_run`), updates the recheck policy, and classifies its
own outcome (`_outcome`): `failed` if discovery could not be completed,
`partial` if nothing qualified or if any coverage exception occurred, otherwise
`success`.

Two matching traversals are *consistency evidence*, not proof that the source
held still. The run records that caveat verbatim in `coverage_json`.

### Recheck tiering

An advertisement that leaves the listing is still checked. After
`terminal_observations_before_weekly` consecutive terminal observations
(`explicit_closure`, `not_found`, `redirected_to_listing`) it moves to the weekly
tier; reappearing in a qualified scan resets it to daily immediately.
**Uncertain** outcomes never advance the demotion streak
(`runner.py::_update_recheck_policy`).

---

## Evidence tables vs. derived projections

**Evidence — append-only in ordinary ingestion:**
`artifacts`, `fetches`, `extractions`, `listing_scans`, `listing_pages`,
`listing_entries`, `listing_scan_assessments`, `posting_observations`,
`posting_versions`, `version_values`, `resource_links`,
`resource_observations`, `resource_associations`, `postings`, `posting_urls`
(the last two carry `last_seen`/`seen_count` counters that advance, but rows are
never removed or re-identified).

**Derived — rebuildable from evidence, always stamped with a rules version:**
`presence_events`, `coverage_gaps`, and every `v_*` view in
`m0002_views.py`. The views cannot drift from the evidence because they are
views; the two derived tables carry `rules_version` plus the `run_id`,
`scan_id`, `observation_id` and version ids that produced each row.

**Operational / mutable state:** `work_queue`, `recheck_policy`,
`collection_runs` (a run row is opened, heartbeated and closed), `backups`
(`pruned_at_utc`), `deployments`, `sources`, `source_configs`.

The practical consequence: you can `DELETE FROM presence_events` and rebuild
them from the observations without touching the network. You cannot reconstruct
an observation, ever.

---

## Transaction and durability model

`src/rowanjobs/db/connection.py` applies the policy on every connection:

- `PRAGMA foreign_keys = ON` — per-connection and off by default in SQLite, so it
  is set explicitly and verified; failure to enable it raises.
- `PRAGMA synchronous = FULL`.
- `PRAGMA temp_store = MEMORY`, `journal_size_limit = 64 MiB`,
  `wal_autocheckpoint = 512`.
- A 15-second busy timeout, so a concurrent reader does not fail instantly.

### Journal mode

WAL is enabled **only** on a runtime verified to carry the SQLite WAL-reset
corruption fix, and **only** on a local filesystem.
`src/rowanjobs/db/runtime.py` reads the SQLite version actually loaded into the
process (via `apsw`, not the interpreter's bundled `sqlite3`) and compares it
against the documented fixed releases (3.51.3 and later, plus the 3.44.6 and
3.50.7 backports; see <https://www.sqlite.org/wal.html#walresetbug>). If the
build is inside the affected range, or the database sits on NFS/CIFS/SSHFS and
friends, the connection falls back to a rollback journal and records the reason
in `wal_deviation`, which surfaces in `status`, `doctor` and the health JSON.

Recorded in `EXECUTION_STATE.md`: the system Python's `sqlite3` on this host is
3.45.1 (affected); `pysqlite3-binary` bundles 3.51.1 (affected); `apsw` 3.53.4.0
bundles SQLite 3.53.4 with `SQLITE_SOURCE_ID 2026-07-24 19:02:57 bf7c7f30…8a88`,
matching the official release hash — so WAL is enabled on this verified runtime.
`pyproject.toml` pins `apsw>=3.53.4.0,<3.54` for exactly this reason.

### Write transactions

`Database.write()` opens `BEGIN IMMEDIATE` — the write lock is taken up front, so
two writers cannot both read, both try to upgrade, and deadlock. Nesting
`write()` raises deliberately: a savepoint would hide a long transaction, which
is the thing being guarded against.

**No write transaction is ever held across network I/O.** Every method in
`src/rowanjobs/collect/repo.py` owns its own short transaction and performs no
network calls; the collector fetches outside the lock and then persists. The
practical effect is that a collector killed mid-harvest loses nothing already
written: on the next start the lock is free, `abandon_stale_runs()` closes the
orphaned run as `aborted` with its evidence intact, `reclaim_orphans()` returns
`in_progress` queue items to `pending`, and the harvest resumes from the queue
instead of starting over.

### Archive first, parse second

`SourceClient.fetch` returns raw bytes and never decodes or interprets them
(`net/client.py`). `Repository.record_fetch` stores the payload through
`ArchiveStore.put_fetch` and writes the `fetches` row in the same short
transaction, *before* any parser runs. Payloads are keyed by the SHA-256 of the
**uncompressed** bytes, so changing the compression method later cannot change
content identity, and an unchanged page re-fetched tomorrow reuses today's
artifact instead of storing a second copy.

Responses larger than the configured ceiling are stored as an explicit
`capture_state='partial'` with a recorded `capture_exception` — never silently
truncated and called complete.

### Other durability details

- `runtime/health.json`, backup manifests and the restore-verification record are
  written temp-file → fsync → rename → fsync-directory (`ops/atomic.py`). A
  half-written health file would be worse than a stale one.
- Backups use SQLite's online backup API, not a file copy, then collapse the
  snapshot to a rollback journal so the single file is self-contained
  (`ops/backup.py::_snapshot`). See `docs/BACKUP_RESTORE.md`.
- The collector lock is an OS `flock`, not a pid file: the kernel releases it
  when the process dies, so a crash cannot leave a stale lock that blocks every
  future run.

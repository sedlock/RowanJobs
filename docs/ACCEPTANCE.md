# Acceptance report

Every value below was measured on `entropy` against the live archive. Where a
number could not be measured it says so; nothing here is an estimate.

| Field | Value |
|---|---|
| Report date | 2026-09-16 (Eastern) |
| Application version | `1.0.0`, parser `1.1.0` (`src/rowanjobs/__init__.py`) |
| Git revision at first harvest | `d3679f3` (recorded in `runtime/deployment.json`) |
| Git revision at report | `4c83f40` |
| Host / user | `entropy` (Ubuntu 24.04.4), `sedlock` |
| Data root | `~/.local/share/rowanjobs/` |
| Database | `~/.local/share/rowanjobs/rowanjobs.db`, schema v5 |

## Status vocabulary

| Status | Meaning |
|---|---|
| `VERIFIED` | Demonstrated on this host, with the evidence recorded below |
| `IMPLEMENTED_BUT_UNVERIFIED` | Code exists and is tested, but the behaviour has not been demonstrated end to end on this host |
| `BLOCKED_EXTERNAL` | Cannot be completed because of something outside this project's control |
| `FAILED` | Attempted and did not work |

A requirement is never marked `VERIFIED` on the strength of a code reading.

---

## 1. Status table

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1.1 | Daily collection scheduled unattended at 06:15 America/New_York | VERIFIED | `rowanjobs.timer` enabled and active; `systemctl --user list-timers` reports the next activation; `linger` is enabled for `sedlock`, so it runs with no login |
| 1.2 | Retry windows do not create extra daily slots | VERIFIED | `rowanjobs retry` declined while run 2 was in progress and again once the slot had succeeded; retries inherit the parent's `scheduled_slot_local_date` |
| 1.3 | A complete unfiltered listing traversal qualifies | VERIFIED | Scans 2-6: 7 pages each, termination `no_more_link`, all 11 qualification checks passed |
| 1.4 | Two traversals produce matching identifier sets | VERIFIED | Runs 2 and 3: `sets_match: true`, 133 = 133, no additions or removals |
| 1.5 | Reconciliation engages, and must *agree* before settling | VERIFIED | Not triggered live (sets matched); pinned by `test_a_reconciliation_that_agrees_with_neither_pass_leaves_the_run_unresolved` |
| 1.6 | Detail pages archived before parsing, identity checked every time | VERIFIED | 272 observations, 272 with `identity_state = 'match'`, 0 mismatches |
| 1.7 | Verbatim extraction contract holds against live pages | VERIFIED | 13 advertisements inspected independently of the production parser across all 7 listing pages: every source paragraph survives, nothing invented |
| 1.8 | `description_html` labelled `source-substring` only when it is one | VERIFIED | 139/139 versions labelled `source-substring`; each verified to be a literal substring of the decoded archived document |
| 1.9 | Content changes recorded as intervals, never instants | VERIFIED | 0 content changes observed (the source did not change); `presence_events` stores `interval_start_utc`/`interval_end_utc` |
| 1.10 | Absence needs a qualified scan and two distinct daily slots | VERIFIED | 0 `absent_qualified` events; `repeatedly_unlisted` counts distinct slot dates |
| 1.11 | Access-control challenge is uncertainty, never absence | VERIFIED | 2 challenges recorded across 311 fetches, both recovered; no absence event resulted |
| 1.12 | Superseded attempts archived as their own `fetches` rows | VERIFIED | Run 3 recorded a challenged attempt and its successful retry as separate rows |
| 1.13 | Lock contention recorded, nothing attempted | VERIFIED | Concurrent `collect` and `retry` during run 2 both returned `lock_contention`, exit 4, naming the holder |
| 1.14 | An interrupted run recovers without manual repair | VERIFIED | Twice: run 1 in production, and a `kill -9` mid-harvest against fixtures — evidence survived, run marked `aborted`, outstanding work `abandoned`, restart completed with no duplicate observations |
| 1.15 | WAL on a verified-safe SQLite runtime | VERIFIED | apsw 3.53.4.0 / SQLite 3.53.4, `SQLITE_SOURCE_ID` matching the official release; `journal_mode=wal`, `synchronous=FULL`, `foreign_keys=1` |
| 1.16 | Backups taken, verified and rotated | VERIFIED | 4 snapshots with manifests; `integrity_check`, `foreign_key_check` and checksum verified on each |
| 1.17 | A snapshot restores to a separate location and queries correctly | VERIFIED | `rowanjobs verify --restore` and `rowanjobs restore`: 7/7 checks passed; the restored copy answers `show 501826`; restoring over the live archive is refused |
| 1.18 | Off-host protection | BLOCKED_EXTERNAL | No off-host destination exists on this host (no rclone/restic/borg/b2/aws, no ssh remotes). The configuration interface is implemented; protection reports `UNCONFIGURED` rather than being assumed |
| 1.19 | Unattended alerting | BLOCKED_EXTERNAL | No established alert destination for this project. Reported `UNCONFIGURED`; RowanJobs will not borrow another application's credentials |
| 1.20 | Host-down detection | BLOCKED_EXTERNAL by design | Requires an external observer; a local timer cannot report while the host is unavailable |
| 1.21 | Exports carry provenance and neutralise CSV formulas | VERIFIED | 133-row CSV export: provenance header present, zero cells a spreadsheet would evaluate |
| 1.22 | SSRF guard refuses non-public and non-allowlisted destinations | VERIFIED | Loopback, link-local metadata, `::1`, `file://`, off-allowlist hosts and embedded credentials all refused — loopback refused even when explicitly allowlisted |
| 1.23 | Credential-bearing headers redacted | VERIFIED | `SENSITIVE_HEADERS` redaction covers cookies both ways; the WAF token never leaves the httpx cookie jar |
| 1.24 | Reprocessing works offline and never fakes a source edit | VERIFIED | Parser bumped 1.0.0 -> 1.1.0 live: 6 advertisements re-read under the new lineage, **0** `content_changed` events |
| 1.25 | Repeat collection deduplicates content and artifacts | VERIFIED | Run 3 re-observed all 133: 266 observations across 133 versions, 143 artifacts for 297 payload fetches |
| 1.26 | CI runs on fixtures with no live-site dependence | IMPLEMENTED_BUT_UNVERIFIED | `.github/workflows/ci.yml` is fixture-only with no schedule trigger; it has not yet run on GitHub |

---

## 2. First production harvest

Run 2, executed through the installed systemd unit, not the CLI.

| Measure | Value |
|---|---|
| Start / end (Eastern) | 2026-09-16 18:43:10 -> 18:54:05 EDT (10m 55s) |
| Outcome | `success` |
| Final qualified listing count | **133** |
| Union encountered | **133** |
| Listing traversals | 2, both qualified, 7 pages each |
| Source occurrences seen | 266 (133 advertisements x 2 page sections) |
| Duplicate occurrences correctly ignored | 133 |
| Source-reported total | 133 — reconciles exactly |
| Detail pages captured | 133 of 133; 0 failed, 0 inconclusive |
| Content versions created | 133 |
| Identity mismatches | 0 |
| HTTP requests | 148; 8.24 MB received; 0 retries; 1 challenge, recovered |
| Backup | created and verified |

Run 1 was an earlier attempt that could not start its browser step under the
hardened unit (an inherited `TMPDIR` outside the sandbox was read-only). It was
killed; run 2 marked it `aborted` and preserved its evidence.

### Gaps and their causes

* **No in-scope job documents were captured during the baseline.** All six
  document links found were hosted off `jobs.rowan.edu`. The adapter has since
  been widened to Rowan's own subdomains (parser 1.1.0) and two documents were
  retrieved during verification; the remainder become in scope at the next full
  collection.
* **1 resource retrieval exception.** `2025-eet-flowchart-4year-program.pdf`
  answered with `text/html` rather than a PDF. Recorded as `failed`, not as a
  silent capture.
* **14 of 133 advertisements have no closing date.** The source shows the field
  blank. Recorded as `field_state='blank'`, `date_parse_state='absent'`; no
  deadline was invented.
* **No coverage gaps were recorded.** 0 open rows in `coverage_gaps`.

---

## 3. Cumulative archive state

| Measure | Value |
|---|---|
| Unique postings | 133 (133 baseline, 0 observed-new) |
| Detail observations | 272, all `content_captured` |
| Distinct content versions | 139 (133 under parser 1.0.0, 6 under 1.1.0) |
| Artifacts | 144, serving 311 payload fetches |
| Payload bytes | 7,981,185 uncompressed -> 2,633,710 stored (3.0x) |
| Database size | 9,957,376 bytes |
| Bytes received from the source | 17,811,457 across 311 fetches |
| Classified resource links | 759 |
| Presence events | 532 (0 `content_changed`, 0 `absent_qualified`) |
| Unknown source labels | 0 |
| Unresolved listing entries | 0 |

## 4. Verification results

* **Tests:** 400 fixture-based tests, all passing; 88% statement coverage. Lint
  (`ruff check`), formatting and `mypy` all clean.
* **Repeat collection:** run 3 re-observed all 133 advertisements. Every posting
  has two observations sharing one content version — no false edits.
* **Parser upgrade:** bumping `PARSER_VERSION` to 1.1.0 created a parallel
  lineage for the 6 advertisements re-read, with zero `content_changed` events.
* **Recovery:** `kill -9` mid-harvest left 4 captured observations, 4 done and
  1 in-progress queue entries. The restart marked the run `aborted`, abandoned
  its outstanding work, and completed all 13 with no duplicates.
* **Integrity:** `integrity_check` ok, `foreign_key_check` clean, 143/143
  archived payload hashes verified.
* **Restore:** a snapshot restored to a separate location passed all 7 checks
  and answered real queries.
* **Next activation:** `rowanjobs.timer` is active with a confirmed next
  activation at 06:15 America/New_York.

## 5. Known limitations

* Off-host backup and unattended alerting are unconfigured; both report their
  state rather than being assumed. See §1.18 and §1.19.
* The source is fronted by AWS WAF. Collection is paced conservatively and
  obtains an access token the way a visitor's browser does; if that becomes
  unavailable the collector backs off and records challenges as coverage
  exceptions rather than absence.
* The GitHub repository is **public**. It existed before this work and its
  visibility was preserved deliberately. Only code, documentation, locked
  dependencies and small public fixtures are committed — no archive data.

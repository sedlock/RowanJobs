# Acceptance report

**Status: skeleton. Measured values are not yet available.**

Every number, count, timing and test result below is marked
`TBD — filled in after the first production harvest`. Nothing in this document
may be filled in from expectation, estimate or analogy: a value goes in only when
it has been measured on this host, against this archive, and the command that
produced it is recorded beside it.

| Field | Value |
|---|---|
| Report date | TBD — filled in after the first production harvest |
| Application version | `1.0.0` (`src/rowanjobs/__init__.py`) |
| Git revision | TBD — filled in after the first production harvest |
| Host | `entropy` (Ubuntu 24.04.4, kernel 7.0.0-31) — per `EXECUTION_STATE.md` |
| Data root | `~/.local/share/rowanjobs/` |
| Reported by | TBD — filled in after the first production harvest |

## Status vocabulary

| Status | Meaning |
|---|---|
| `VERIFIED` | Demonstrated on this host, with the evidence recorded below |
| `IMPLEMENTED_BUT_UNVERIFIED` | The code exists and is reviewed, but the behaviour has not been demonstrated end to end |
| `BLOCKED_EXTERNAL` | Cannot be completed because of something outside this project's control |
| `FAILED` | Attempted and did not work |

A requirement is never marked `VERIFIED` on the strength of a code reading.

---

## 1. Status table

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1.1 | Daily collection runs unattended at 06:15 America/New_York | TBD | TBD |
| 1.2 | Retry windows behave as designed and do not create extra daily slots | TBD | TBD |
| 1.3 | A complete unfiltered listing traversal qualifies | TBD | TBD |
| 1.4 | Two traversals produce matching identifier sets | TBD | TBD |
| 1.5 | Reconciliation traversal engages when sets disagree | TBD | TBD |
| 1.6 | Detail pages are archived before parsing, with identity checked | TBD | TBD |
| 1.7 | Verbatim extraction contract holds against live pages | TBD | TBD |
| 1.8 | `description_html` is labelled `source-substring` when it is one | TBD | TBD |
| 1.9 | Content changes are detected and recorded as intervals | TBD | TBD |
| 1.10 | Absence requires a qualified scan and two distinct daily slots | TBD | TBD |
| 1.11 | Access-control challenge is recorded as uncertainty, never absence | TBD | TBD |
| 1.12 | Lock contention is recorded as `lock_contention`, nothing attempted | TBD | TBD |
| 1.13 | An interrupted run recovers without manual repair | TBD | TBD |
| 1.14 | WAL enabled on a verified-safe SQLite runtime | TBD | TBD |
| 1.15 | Backups are taken, verified and rotated | TBD | TBD |
| 1.16 | A snapshot restores to a separate location and queries correctly | TBD | TBD |
| 1.17 | Off-host protection | `BLOCKED_EXTERNAL` — no off-host destination exists on this host; the configuration interface is present and protection is reported `UNCONFIGURED` rather than assumed (`EXECUTION_STATE.md`, `docs/BACKUP_RESTORE.md`) |
| 1.18 | Unattended alerting | `BLOCKED_EXTERNAL` — no established alert destination for this project; reported `UNCONFIGURED` (`src/rowanjobs/ops/notify.py`) |
| 1.19 | Host-down detection | `BLOCKED_EXTERNAL` by design — requires an external observer; a local timer cannot report while the host is unavailable |
| 1.20 | Exports carry provenance and neutralise CSV formulas | TBD | TBD |
| 1.21 | SSRF guard refuses non-public and non-allowlisted destinations | TBD | TBD |
| 1.22 | Credential-bearing headers are redacted in the archive and logs | TBD | TBD |
| 1.23 | File permissions: 0700 data root, 0600 database and backups | TBD | TBD |
| 1.24 | The production collector makes zero model calls | TBD | TBD |
| 1.25 | Lint, type check and test suite pass | TBD | TBD |

Add rows as further requirements are agreed; do not delete rows that ended
`FAILED` or `BLOCKED_EXTERNAL`.

---

## 2. Environment

| Item | Value |
|---|---|
| Python | TBD — filled in after the first production harvest (`rowanjobs doctor`) |
| SQLite provider and version | TBD — filled in after the first production harvest |
| `SQLITE_SOURCE_ID` | TBD — filled in after the first production harvest |
| `wal_safe` | TBD — filled in after the first production harvest |
| Journal mode in use | TBD — filled in after the first production harvest |
| Filesystem of the data root | TBD — filled in after the first production harvest |
| Free space at report time | TBD — filled in after the first production harvest |
| Lingering enabled | TBD — filled in after the first production harvest |

Command: `rowanjobs --json doctor`.

## 3. First production harvest

| Measurement | Value |
|---|---|
| Run id / uuid | TBD — filled in after the first production harvest |
| Run kind, slot local date | TBD — filled in after the first production harvest |
| Started / ended (local), duration | TBD — filled in after the first production harvest |
| Outcome and detail | TBD — filled in after the first production harvest |
| `is_baseline` | TBD — filled in after the first production harvest |
| Listing pages requested / ok / failed | TBD — filled in after the first production harvest |
| Termination reason (each traversal) | TBD — filled in after the first production harvest |
| Final qualified listing count | TBD — filled in after the first production harvest |
| Source-reported total | TBD — filled in after the first production harvest |
| Row occurrences seen / duplicates ignored | TBD — filled in after the first production harvest |
| Identifier sets matched between traversals | TBD — filled in after the first production harvest |
| Detail attempted / captured / failed / uncertain | TBD — filled in after the first production harvest |
| Content versions created | TBD — filled in after the first production harvest |
| Resources attempted / captured | TBD — filled in after the first production harvest |
| Live requests made / budget remaining | TBD — filled in after the first production harvest |
| Total sleep time (pacing) | TBD — filled in after the first production harvest |
| Access-control responses | TBD — filled in after the first production harvest |
| Coverage gaps recorded | TBD — filled in after the first production harvest |
| Archive size / payload compression ratio | TBD — filled in after the first production harvest |

Commands: `rowanjobs --json collect --kind daily`, `rowanjobs --json status`.

## 4. Manual inspection

A person must read a sample of archived advertisements against the live site and
confirm the archive is faithful. Record what was checked, not just that it was.

| Check | Result |
|---|---|
| Sample size and job ids inspected | TBD — filled in after the first production harvest |
| Titles match the live pages exactly | TBD — filled in after the first production harvest |
| Description text matches, including punctuation and non-breaking spaces | TBD — filled in after the first production harvest |
| List items appear one per line with no inserted bullets | TBD — filled in after the first production harvest |
| Table cells separated by TAB | TBD — filled in after the first production harvest |
| `description_html_kind` value distribution | TBD — filled in after the first production harvest |
| Labelled fields captured with their source labels | TBD — filled in after the first production harvest |
| Any `known_label = 0` fields (new source labels) | TBD — filled in after the first production harvest |
| `Advertised` recorded with precision `date` and null `parsed_utc` | TBD — filled in after the first production harvest |
| `Applications close` recorded with minute precision and its timezone wording | TBD — filled in after the first production harvest |
| Links classified correctly; apply workflow excluded | TBD — filled in after the first production harvest |
| Any conflicts recorded (`conflicts_json`) | TBD — filled in after the first production harvest |

Commands: `rowanjobs show <job_id> --full`, `rowanjobs --json show <job_id>`.

## 5. Verification run

| Check | Result |
|---|---|
| `PRAGMA integrity_check` | TBD — filled in after the first production harvest |
| `PRAGMA foreign_key_check` violations | TBD — filled in after the first production harvest |
| Payload hashes checked / failed | TBD — filled in after the first production harvest |
| Restore check performed | TBD — filled in after the first production harvest |
| Restore check per-check results | TBD — filled in after the first production harvest |
| Backup snapshot size and sha256 | TBD — filled in after the first production harvest |
| Rotation behaviour observed | TBD — filled in after the first production harvest |

Commands: `rowanjobs --json verify --restore`, `rowanjobs --json backup`.

## 6. Longitudinal behaviour

These cannot be verified on day one; they need at least two qualified daily
collections, and some need a real source change.

| Behaviour | Status | Evidence |
|---|---|---|
| Second qualified collection recorded against a distinct slot date | TBD | TBD |
| An unchanged advertisement reuses its existing content version | TBD | TBD |
| A genuinely edited advertisement produces a new version and a `content_changed` event with a sane interval | TBD | TBD |
| An advertisement leaving the listing produces `absent_qualified` on one slot, then a second on the next | TBD | TBD |
| `meets_two_day_rule` flips only after two distinct qualifying daily observations | TBD | TBD |
| A same-day retry does **not** add a second absence confirmation | TBD | TBD |
| Recheck demotion to weekly after the configured terminal streak | TBD | TBD |
| A reappearance under the same source id retains the original identity and history | TBD | TBD |
| `reprocess` after a contract bump creates a parallel version line without a spurious change event | TBD | TBD |

## 7. Test suite and CI

| Item | Result |
|---|---|
| `ruff check src tests` | TBD — filled in after the first production harvest |
| `ruff format --check src tests` | TBD — filled in after the first production harvest |
| `mypy` | TBD — filled in after the first production harvest |
| `pytest -q` — tests passed / failed / skipped | TBD — filled in after the first production harvest |
| Coverage | TBD — filled in after the first production harvest |
| CI run (GitHub Actions) | TBD — filled in after the first production harvest |

## 8. Deviations and known limitations

Carried forward from the source audit and the environment; update as they are
resolved.

| # | Item | Status |
|---|---|---|
| 8.1 | Behaviour of a non-existent job id is unknown — the audit probe was answered by the WAF challenge (`docs/SOURCE_ADAPTER_AUDIT.md`) | Open |
| 8.2 | Slug canonicalisation behaviour is unknown, for the same reason | Open |
| 8.3 | The source's closure-template wording has not been observed; `CLOSURE_PHRASES` is defensive | Open |
| 8.4 | No off-host backup destination exists on this host | `BLOCKED_EXTERNAL` |
| 8.5 | No unattended alert destination is established for this project | `BLOCKED_EXTERNAL` |
| 8.6 | Host-down detection needs an external observer | `BLOCKED_EXTERNAL` by design |
| 8.7 | The WAF threshold is a single observation, not a published policy | Open |
| 8.8 | *(add further deviations here as they are found)* | |

## 9. Sign-off

| Item | Value |
|---|---|
| Overall assessment | TBD — filled in after the first production harvest |
| Outstanding `FAILED` items | TBD — filled in after the first production harvest |
| Accepted by | TBD — filled in after the first production harvest |
| Date | TBD — filled in after the first production harvest |

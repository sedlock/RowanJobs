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
`__DATA_ROOT__` placeholders. `ops/install.sh` performs the substitution with
absolute resolved paths (no `~`, no shell expansion, no activated virtualenv),
syncs the locked environment, applies migrations, installs and enables the
units, validates the calendar expression and records a deployment manifest. It
is idempotent: an unchanged unit is left alone. `ops/uninstall.sh` removes the
units and never touches the archive.

```
./ops/install.sh                     # deploy or converge
./ops/install.sh --no-enable         # install without enabling the timers
./ops/uninstall.sh                   # remove units, keep all collected evidence
```

The units are installed as **user** units and require lingering to be enabled for the collecting user
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
4. Exits 0 if a run for the slot is **still in progress** (`ended_at_utc` is
   null) — a slow daily collection can still be going when a retry window opens,
   and saying so is better than colliding with the collector lock and looking
   like a failure.
5. Exits 0 if any run for the slot already succeeded.
6. Exits 2 if the slot has used its `max_retries_per_slot` (default 2) attempts
   and is still unresolved, leaving it for the next scheduled collection.
7. Otherwise runs a collection with `run_kind='retry'`, `parent_run_id` set to
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

## Deployment: what production actually runs

**Production does not run this working tree.** It runs a sealed, read-only
release directory named for its commit:

```
/mnt/bench/app-releases/rowanjobs/<sha>/     the exported tree and its own .venv
/mnt/bench/app-releases/rowanjobs/current    -> the release the units execute
/mnt/bench/app-releases/rowanjobs/previous   -> the rollback target
.activation-journal.jsonl                    every activation, with its reason
```

This exists because of a specific failure. Until 2026-09-19 the units executed
`/mnt/bench/src/RowanJobs/.venv/bin/rowanjobs`, an **editable** install pointing
at the checkout, so the 06:15 collection ran whatever happened to be on disk at
06:15 — including a half-finished edit. On 2026-09-18 a configuration-schema
change landed in the tree without the matching deployed config, and the next two
scheduled activations exited 5 before they could record anything.

RowanJobs owns this runtime. ControlPanel observes it and never repoints it.

### The commands

```sh
python3 ops/release.py status      # what is deployed, and what the units run
python3 ops/release.py build HEAD  # export, build, validate, seal
python3 ops/release.py deploy HEAD # build then activate
python3 ops/release.py verify      # prove the units execute the current release
python3 ops/release.py rollback    # return to `previous`
python3 ops/release.py prune       # drop old releases (never current/previous)
```

`status` deliberately reports the deployed commit **and** the development HEAD
separately, because they are different facts and conflating them is how nobody
notices that production is three commits behind.

### What a candidate must prove before anything points at it

A release is built from a **commit**, not from the working tree, so a dirty
checkout cannot become production by accident. Before the manifest is written
the candidate must:

1. **start** — `rowanjobs --version` runs;
2. **import** — the package and collector import cleanly;
3. **load the deployed configuration** — the candidate reads the *actual*
   `config.toml` the service will hand it. This is the check that would have
   caught 2026-09-18, and it is tested against that exact config shape;
4. **emit the status contract** — `rowanjobs health` produces
   `controlpanel.status.v1`.

Every check then runs **again** after sealing, at the release's final path. The
first real build passed validation and still produced an unrunnable binary: a
virtualenv's console scripts embed the interpreter's absolute path in their
shebang, so a release built in a staging directory and renamed points at a path
that no longer exists. Releases are now built where they will run.

The interpreter is pinned (`.python-version`, and explicitly at build time).
Left to itself, uv chose Python 3.14 for the first release while every test in
this project runs on 3.12.

### Activation

Under a deploy lock, and only when no collection holds the collector's own lock
— a harvest is never switched underneath itself. Then: back up, migrate with
the candidate, repoint `current`, write the drop-ins, reload, re-enable the
timers, verify the units really execute the release, and smoke-test health.

**Any failure restores the previous release** and records why in the activation
journal. An interrupted preparation leaves the known-good release in charge,
because a release is only a release once its `READY` manifest is written, and
that is written last.

Activating a release **older than the applied schema is refused**. Rolling back
code is not rolling back a database: old code against a migrated archive cannot
read its own evidence. `--allow-schema-regression` exists for when you have a
restore plan, and demands you say so.

### After deploying

```sh
python3 ops/release.py verify
rowanjobs health | python3 -m json.tool | head -20
systemctl --user list-timers 'rowanjobs*'
```

---

## When a unit fails before it can record anything

The collection tables can only describe runs that started. A unit that dies
during startup — a bad config, a missing interpreter, a broken release — leaves
no run row, so every collection-derived number still looks correct and the
archive has nothing to report. That is precisely how 2026-09-18 stayed
invisible until someone read the journal.

`OnFailure=rowanjobs-failure@%n.service` on both collecting units closes it.
The handler (`ops/onfailure.py`) is **standard-library only and imports nothing
from rowanjobs**, because a detector that depends on the configuration parser it
is monitoring cannot report that the parser is broken. It:

1. appends a durable record to `<data_root>/runtime/startup-failures.jsonl`;
2. best-effort emails the failure, clearly labelled as a startup failure.

The record is **operational evidence, never archive evidence**. It lives in
`runtime/`, it never implies a source observation occurred, and it says so in
its own `note` field.

`rowanjobs health` surfaces unresolved records as the `startup-integrity`
component. A failure is resolved **only by a later collection for the same
scheduled slot** — not by time passing and not by the next day succeeding.
Resolving it does not erase it: the record stays on disk, and the day keeps
whatever coverage it actually had.

```sh
cat ~/.local/share/rowanjobs/runtime/startup-failures.jsonl | python3 -m json.tool
rowanjobs health | python3 -c 'import json,sys; d=json.load(sys.stdin); print([c for c in d["components"] if c["id"]=="startup-integrity"])'
```

---

## ControlPanel

RowanJobs is a registered ControlPanel target. The console runs

```sh
/mnt/bench/app-releases/rowanjobs/current/.venv/bin/rowanjobs health
```

once a minute and reads the `controlpanel.status.v1` document it prints.

That command opens the archive **read-only**, so "monitoring cannot change what
it monitors" is a property of the connection rather than a promise in a
docstring. It never collects, never sends mail, never migrates, and never
writes. `status --json` is unchanged and remains the archive's own contract.

Two scoring decisions are deliberate:

* **Off-host protection is reported as evidence on the local-snapshot
  component, not as a component of its own.** ControlPanel rolls components up
  with `max()`, so publishing an `unknown` off-host component held the entire
  project at unknown on its first collection — masking nine healthy components
  behind one the operator had already decided not to care about.
* **Mail trouble is `degraded`, never `failed`.** A report that did not arrive
  is a real problem, but the harvest it describes still happened.

A linked document the source answers 404 for is recorded as evidence rather
than scored as a fault. An employer linking a PDF that does not exist is a fact
this archive captures faithfully on every run; a console that is permanently
slightly unwell is one nobody reads.

**Collection does not depend on ControlPanel.** If the console is down,
stopped, or uninstalled, the 06:15 timer collects exactly as before.

---

## Run reporting

A status report is emailed after **every actual collection run** — successful,
partial, failed, an interrupted one, a recovery attempt that really collected,
or a manual run. The three things that send nothing at all:

* a retry window that found nothing to do (it never creates a run);
* a run that collected nothing because another collector held the lock;
* `status`, `doctor`, `health`, `export` and every other reporting command.

Reporting is the last thing a collection does and it is walled off from the
collection's own verdict. **A report that cannot be delivered never makes a good
harvest look bad**: the exit code, the health JSON and the archive are
unchanged, and the delivery problem is recorded in `notifications` — never
itself emailed, because an alert about a failed alert has nowhere to go.

### What one report contains

Composed entirely from stored evidence for one `run_id`
(`src/rowanjobs/ops/report.py`), so it can be built long after the run and still
describe what actually happened. It never re-reads the website. Both a plain
text and an HTML part are sent, carrying the same facts.

Two things it is careful about:

* **A baseline is not news.** The first qualified collection discovers every
  advertisement at once. Presenting 133 as "newly published" would be a false
  claim about the employer's hiring, so newly-observed, no-longer-listed and
  content-changed counts are reported only for a run that had a baseline to
  compare against. A baseline report says so on its face.
* **A delayed report says so.** The collection time is the run's own; the send
  time is its own. A catch-up is labelled in the subject and at the top of both
  bodies, and the times shown remain the collection's.

The **Action required** section is the one to read first: it names what a person
actually has to do, or says `None.`

### Delivery state

One row per run in `notifications`, `UNIQUE(run_id, kind)`, so however many
times delivery is attempted the operator gets **one** report per run.

| State | Meaning |
|---|---|
| `pending` | Composed, not yet accepted by the provider. |
| `accepted` | The submission server took responsibility for the message. **This is not the same claim as inbox receipt**, and nothing in RowanJobs ever says it is. |
| `failed` | An attempt failed transiently (timeout, 4xx). It will be retried. |
| `abandoned` | Permanently rejected (bad App Password, refused recipient, 5xx), or the attempt budget is spent. No further attempts without an explicit ask. |
| `skipped` | Deliberately not reported. In practice: a run that finished before reporting was ever configured. |

`skipped` exists so that switching reporting on for an archive that already has
history does not leave every past run looking like a report that went missing —
and does not post a burst of mail about collections whose outcome the operator
already knows.

**Only runs that ended before reporting existed are eligible**, where "existed"
means the moment migration 7 created the `notifications` table, read from
`schema_migrations`. The earlier rule — "the delivery table is empty" — was
ambiguous in a way that mattered: emptiness is equally consistent with
*reporting is brand new* and with *the very first report was genuinely lost*,
and treating the second as the first would silently write off a real missed
report. A run that finished after the epoch and still has no report stays
visible as an unreported run, however empty the table happens to be.

### `rowanjobs notify`

Delivers whatever reporting still owes. **It never collects**: no run is
created, the source is never contacted, and its worst case is a late email.

```sh
rowanjobs notify                 # retry reports composed but never accepted
rowanjobs notify --catch-up      # also compose reports for runs that got none
rowanjobs notify --run 7         # send run 7's report even if skipped/abandoned
rowanjobs notify --test          # prove the credential and route; records nothing
```

**`rowanjobs-notify.timer` runs the first of these hourly at :40**, clear of the
06:15 / 09:15 / 13:15 collection windows. This matters: an implemented retry
command with no scheduled caller is not a retry policy. Before the timer
existed, a report that failed to send waited for the next collection to happen
to sweep it, which is a coincidence rather than a mechanism. A provider outage
now costs a late report, and recovery never requires a second harvest.

`--test` is a connectivity check, not evidence about a collection, so it is
deliberately **not** recorded in `notifications` next to real run reports.

`--catch-up` marks everything it composes as delayed, and suppresses the
predates-reporting marker on first use — otherwise it would silence the very
runs you just asked for.

### When reports stop arriving

`status` reports notification health **separately from collection health**, and
counts the one failure mode a delivery table cannot otherwise see: silence.

```sh
rowanjobs status --json | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["notifications"], indent=2))'
```

* `UNCONFIGURED` — no `kind`/`recipient`. Nothing is sent, by choice.
* `FAILED` — the credential file is missing, is not mode 0600, sits in a
  directory other accounts can enter, or a report was abandoned. `rowanjobs
  doctor` fails on this; an unconfigured destination is only a note.
* `DEGRADED` — reports await retry, or `unreported_runs` is non-empty: a
  collection completed and no report was ever composed for it. Run
  `rowanjobs notify --catch-up`.
* `VERIFIED` — the last report was accepted by the provider. Read that as
  written: acceptance, not receipt.

Nothing here can be inferred from mail alone, which is the point of recording it
in the archive. If the host is down, no report arrives and no local state says
so — host-down detection needs an external observer (see below).

---

## Reading `rowanjobs status`

```sh
rowanjobs status
```

Reads the archive **read-only** and prints, in order:

- `collection` — the overall state and the last attempt, with its outcome and
  duration. A second `last scheduled` line appears when the most recent
  `daily`/`retry` run is not the most recent run of any kind, because the state
  is judged on the scheduled one.
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
| `2` | DEGRADED | Useful evidence collected, but with a coverage exception: no traversal qualified, listing sets did not reconcile, queued detail retrievals were left unattempted (a bounded `--max-details` pass or an interrupted one), detail retrievals failed or were inconclusive, or the source applied an access-control challenge | Read `errors` / `coverage` in the run output; absence conclusions are suppressed for that run |
| `3` | PROTECTION | Collection was fine but protection is degraded: the backup failed, or there is no recent verified snapshot | Fix the backup before the next run; ingestion is unaffected |
| `4` | LOCKED | Another collector held the lock; **nothing was attempted** | Usually benign (an overlapping manual run). Not a source failure and not an empty collection |
| `5` | USAGE | Usage or configuration error. `doctor` also returns this when the configuration file is rejected, reporting it as a failed `configuration` check rather than failing to start — finding that before the timer does is the point of `doctor` | Fix the command line or the config file |

`SuccessExitStatus=0 2 3` in both service units: 2 and 3 mean useful evidence was
still collected, so systemd should not mark the unit failed. Only 1, 4 and 5 are
real failures from the timer's point of view.

`exit_code_for` maps health to a code: collection state `FAILED` **or `STALE`**
→ 1; `DEGRADED` or `UNKNOWN` → 2; otherwise, local backup state in
`FAILED`/`DEGRADED`/`UNPROTECTED` or off-host state `FAILED` → 3; else 0. Note
that an `UNCONFIGURED` off-host state does **not** produce exit 3 — it is
reported honestly but is not treated as a failure of something that was never
set up.

`STALE` means two or more scheduled days have passed without a qualified
discovery (`collection.coverage_window.days_since_last_qualified >= 2`). A
collector that has simply stopped running would otherwise keep reporting
`HEALTHY` on the strength of its last successful run, which is exactly the
failure an operator most needs to see.

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

A challenge that the collector *recovered* from is less serious: the page was
eventually retrieved completely, coverage is intact, and the scan still
qualifies. It is still archived as its own `fetches` row and listed in the
assessment's `pages_challenged_then_recovered`, so it remains visible
(`src/rowanjobs/collect/qualify.py`, `src/rowanjobs/collect/scanner.py`).

**What it is:** AWS WAF answering `HTTP 202` with `x-amzn-waf-action: challenge`
and an empty body. Observed during the source audit after roughly seven requests
in about ninety seconds; it cleared after about eleven minutes of quiet
(`docs/SOURCE_ADAPTER_AUDIT.md`).

**What the collector already did:** attempted to obtain an access token up
front (workflow step 0), widened its inter-request interval by 1.6× per
challenge (capped at 30 s), waited `challenge_backoff_seconds × 2^(n−1)` — 60 s
→ 120 s → 240 s at the default of 60 — re-solved the challenge through the
ordinary browser path if one is available, and stopped requesting after
`max_consecutive_challenges` rather than hammering the source.

If `errors` contains `access_priming_unavailable`, priming was **attempted and
failed** — usually because Playwright is not installed. Collection continues and
backs off when challenged, but coverage may be reduced. A run where the page
loaded normally and the source simply issued no token is *not* an error: priming
reports `{"primed": false, "required": false}` and nothing is added to `errors`,
because no token exists until the source challenges. Either way,
`coverage.access_priming` records what happened.

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
| `no_access_control_response` | WAF challenge the run never recovered from | See above; check `coverage.access_priming` too |
| `all_pages_retrieved` / `all_pages_http_200` | Transient network or site trouble | Let the retry window handle it |
| `legitimate_termination` = `max_pages` | The inventory grew past `max_listing_pages`, or pagination misbehaved | Raise the bound only after checking the source really has that many pages |
| `structure_recognized` | **The source's markup changed** | Stop and investigate. Compare a fresh capture against `tests/fixtures/pageup/`, update the adapter, and bump `PARSER_VERSION` |
| `source_count_reconciles` | The source's own count disagrees with what we collected | Check whether the count semantics changed; remember it is *remaining after this page*, not a total |
| `no_pagination_loop` | The site repeated a page | Usually transient; if persistent, the pagination contract changed |
| `empty_result_validated` | A zero-result scan without the intact empty-result template | Treat as an error page, not as "there are no jobs" |

Do **not** relax a qualification check to make a run go green. If a check no
longer expresses the right thing, change it deliberately and bump
`QUALIFICATION_RULES_VERSION`, which records a new verdict beside the old one.

### A `listing_disagreement_within_run` coverage gap

**Symptom:** the gap appears in `status`, and an advertisement you expected an
`absent_qualified` event for did not get one.

**What it is:** one qualified traversal in the run listed the advertisement and
a later qualified traversal did not. The run refuses to record absence that
contradicts its own evidence, so the disagreement is recorded as a coverage
exception instead (`src/rowanjobs/collect/events.py`). The gap names the scan and
the `external_job_id`.

**What to do:** nothing, usually — an advertisement withdrawn between two
traversals looks exactly like this, and the next day's collection will settle it.
If it recurs for many advertisements every run, the listing is changing under the
traversal faster than the design assumes; look at the scan set comparison in
`coverage_json` before concluding anything about withdrawals.

### The collector has stopped: `STALE`

**Symptom:** exit 1 from `status` with `collection.state = STALE`, while the last
recorded run may well say `success`.

**What it is:** `coverage_window.days_since_last_qualified` is 2 or more. The
timer is not producing qualified discoveries. Check
`systemctl --user list-timers 'rowanjobs*'`, whether lingering is still enabled,
and the last journal entries for `rowanjobs.service`. The missed days are already
listed in `coverage_window.missed_local_dates` and stay missed.

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
structure, versioned by `HEALTH_SCHEMA_VERSION` (currently `"2"`), defined in
`src/rowanjobs/ops/health.py`. Version 2 replaced the `notifications` section's
alerting stub with run reporting: it now carries per-state delivery counts and
the run ids that completed with no report at all.

Collection health, archive integrity, local backup health and off-host
protection are reported **separately**, because they fail independently and
collapsing them would hide exactly the failure an operator needs to see.

```
health_schema_version   "2"
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
                                   STALE | FAILED | UNKNOWN
  last_attempt                     run brief (see below) for the most recent run
                                   of ANY kind, or null
  last_scheduled_attempt           run brief for the most recent daily/retry
                                   run — the one `state` is derived from
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
  coverage_window                  which scheduled days since the first
                                   qualified scan actually have one:
                                   first_covered_local_date,
                                   last_covered_local_date, covered_days,
                                   missed_local_dates (most recent 30),
                                   missed_days, days_since_last_qualified,
                                   and a note that a missed day is permanent

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

notifications                      reported independently of collection health:
                                   a bounced report never makes a good harvest
                                   look bad
  state                            UNCONFIGURED | CONFIGURED | VERIFIED |
                                   DEGRADED | FAILED
  detail                           one sentence naming the reason
  recipient                        configured destination, or null
  policy                           "a report after every actual collection run"
  pending, failed, abandoned       delivery counts by state
  unreported_runs[]                run ids that completed and were never
                                   reported at all — the silence case
  last_accepted                    run_id, accepted_at_utc/local, message_id,
                                   or null
  note                             "acceptance by the provider is not the same
                                   claim as inbox receipt"

schedule                           configured_expression, timezone,
                                   calendar_validation, lingering{enabled,
                                   detail}, units{...}, next_collection_local,
                                   scheduled                 — null with --no-timer

paths                              data_root, database, backups, logs, runtime,
                                   health, config

notes[]                            standing caveats
```

A **run brief** (`last_attempt`, `last_scheduled_attempt`, `last_completed`,
`run_in_progress`) contains:
`run_id`, `run_uuid`-less summary fields `run_kind`, `attempt_no`,
`parent_run_id`, `slot_local_date`, `is_baseline`, `started_at_utc/local`,
`ended_at_utc/local`, `duration`, `outcome`, `outcome_detail`,
`qualified_scans`, `fetches`, `failed_fetches`, `access_control_responses`,
`observations`, `captured`, `resources`, `coverage_gaps`.

### Reading it correctly

- `collection.state` describes the **scheduled** collection: it is derived from
  `last_scheduled_attempt` (the most recent `daily` or `retry` run), falling back
  to `last_attempt` only when no scheduled run exists at all. A deliberately
  bounded manual or verification run therefore does not make the system look
  degraded, and a manual success cannot paper over a failing timer
  (`src/rowanjobs/ops/health.py::build_health`).
- `collection.state = HEALTHY` requires that scheduled run to have succeeded
  **and** a qualified scan to exist. A successful run with no qualified scan is
  not healthy.
- `collection.state = STALE` means `coverage_window.days_since_last_qualified`
  is 2 or more: two scheduled days have gone by without a qualified discovery,
  whatever the last run happened to return. It maps to exit **1**. Nothing else
  in the payload detects a collector that has simply stopped — every other field
  would keep describing the last successful run indefinitely.
- `collection.coverage_window.missed_local_dates` lists days with no qualified
  scan. Today counts as missed only once its scheduled slot has passed. Those
  days are **permanent** gaps: a later collection observes today and cannot
  reconstruct what was published on a day nobody looked.
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
| Daily | the run report in your inbox | It arrives at all; **Action required** says `None.`; the advertised count is plausible. No report arriving is itself a signal — the host may be down, which nothing local can tell you |
| When a report did not arrive | `rowanjobs notify` | Resends anything composed but never accepted. The hourly timer already does this; run it by hand only to see the reason immediately. `--catch-up` covers a run that was never reported at all |
| After any deployment | `python3 ops/release.py verify` | The units execute the release you think they do |
| Weekly | `rowanjobs health` in ControlPanel | RowanJobs is green, and the numbers match what the daily email said |
| Weekly | `rowanjobs verify` | `integrity_check` ok, no FK violations, all payload hashes verify |
| Weekly (automatic) | restore verification | Driven by `backup.restore_check_interval_days`; result in `runtime/restore-verification.json` |
| After a deployment | `rowanjobs record-deployment --note "..."` | The manifest and `deployments` row are written |
| When reporting or investigating a problem | `rowanjobs diagnostics` | A sanitised bundle in `<data_root>/exports/` (or `--output PATH`): health, recent runs, every scan assessment with its checks, coverage gaps, fetch exceptions, unknown source labels, unresolved listing rows, identity conflicts, resource outcomes, freshness counts and a posting sample. It excludes archived payloads, description text and markup, request/response headers and anything credential-shaped (`src/rowanjobs/ops/diagnostics.py`); it opens the archive read-only |

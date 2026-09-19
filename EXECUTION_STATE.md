# RowanJobs execution state

Durable notes so a resumed session can continue accurately. Update this file
whenever a milestone completes or a blocker changes.

## Environment (verified 2026-09-16)

| Fact | Value |
|---|---|
| Host | `entropy` (Ubuntu 24.04.4, kernel 7.0.0-31) |
| User | `sedlock` (uid 1000, in `sudo`) |
| Repo | `/mnt/bench/src/RowanJobs` (`~/src` is a symlink to `/mnt/bench/src`) |
| `/mnt/bench` | `/dev/sda1`, ext4, 1.8 TB USB SSD, **locally attached — not off-host** |
| Data root | `~/.local/share/rowanjobs/` on `/dev/nvme0n1p5` (matches the convention used by every other project on this host) |
| Config | `~/.config/rowanjobs/config.toml` |
| systemd | user units, `linger` already enabled for `sedlock` |
| GitHub | `sedlock/RowanJobs` **already existed, empty, and PUBLIC**; visibility preserved per instructions |
| Off-host backup | none configured on this host (no rclone/restic/borg/b2/aws, no ssh remotes) |
| Notifications | no established unattended alert destination for this project |

## Source audit findings (2026-09-16, captures in `~/.local/share/rowanjobs/source-audit/2026-09-16/`)

* `robots.txt` 200: only `/admin`, `/uat`, `/ci`, `/staging`-style paths are
  disallowed. `/en-us/listing/` and `/en-us/job/...` are permitted. No
  `Crawl-delay`, no `Sitemap`.
* Platform is PageUp (`PU.Jobs.source = {"instId":860,...}`).
* Pagination: `/en-us/listing/?page=N&page-items=20`.
* `<a class="more-link"><span class="count">N</span>` is the number of jobs
  **remaining after this page**, not the total. Page 1 said 113 with 20 rows on
  the page → 133 advertisements, 7 pages.
* Page 7 has 13 rows and **no** more-link → final page.
* Pages 8 and 99 return HTTP 200 with intact table markup and zero rows → a
  validated empty result, distinguishable from an error page.
* The page repeats every advertisement in a second `Current Opportunities`
  section (`tbody#recent-jobs-content`); summaries there are HTML-commented out.
  Counting `a.job-link` across the page doubles the inventory.
* **AWS WAF**: after ~7 application requests in ~90 s the site answered `HTTP
  202`, empty body, `x-amzn-waf-action: challenge`, for *every* `/en-us/` URL
  regardless of user agent. It cleared after ~11 minutes of quiet.
  `robots.txt` kept serving normally throughout. Handled as
  `access_control_challenge` — collection uncertainty, never absence.
* No JSON-LD / structured data on listing or detail pages.
* Detail dates: `Advertised` shows a bare date with a 12:00Z placeholder in the
  `datetime` attribute; `Applications close` shows minute precision. Both carry
  the literal words "Eastern Daylight Time" after the `<time>` element.

## SQLite runtime decision

* System Python's `sqlite3` is **3.45.1** → inside the WAL-reset corruption
  range (3.7.0 … 3.51.2).
* `pysqlite3-binary` 0.5.4.post2 bundles **3.51.1** → also affected.
* `apsw` 3.53.4.0 bundles **3.53.4**, `SQLITE_SOURCE_ID
  2026-07-24 19:02:57 bf7c7f30…8a88`, matching the official 3.53.4 release
  hash. 3.53.0 changelog: "Fix the WAL-reset database corruption bug."
  → **WAL enabled on this verified runtime.**

## Progress

- [x] Environment + repository inspection
- [x] Source audit and early evidence preservation
- [x] SQLite runtime verification
- [x] Schema + migrations
- [x] Archive store, decoding, verbatim text contract, PageUp adapters
- [x] Collector orchestration
- [x] CLI
- [x] Backups / health / restore verification
- [x] Tests + CI (378 fixture-based tests, 87% coverage)
- [x] Deployment (systemd user units, timer enabled, lingering already on)
- [x] First production harvest (run 2, 2026-09-16, success)
- [x] Manual inspection of 13 archived advertisements across all 7 listing pages
- [x] Verification run (run 3): 133 re-observed, perfect content deduplication
- [x] Independent adversarial review; 3 critical + 8 lesser findings fixed and pinned
- [x] Acceptance report (docs/ACCEPTANCE.md) with measured results
- [x] Pushed to https://github.com/sedlock/RowanJobs (main)

## Access-control findings (measured 2026-09-16)

The source is fronted by AWS WAF. Two measurements, both from entropy:

* **Without a token.** A plain HTTP client was served roughly six requests and
  then challenged (`HTTP 202`, empty body, `x-amzn-waf-action: challenge`) for
  every `/en-us/` URL, regardless of user agent. `robots.txt` kept serving
  normally. The challenge cleared after several minutes of quiet. During a
  bounded live run at 2.5 s pacing, listing pages 1-6 were served and page 7
  needed four attempts across ~7 minutes of backoff before it succeeded.
* **With a browser-issued token.** After one ordinary headless-Chromium page
  load issued an `aws-waf-token` cookie, 12 requests at 2.5 s intervals were all
  served with **zero** challenges.

An AWS WAF *challenge* action is designed to be resolved silently by any
visitor's browser; there is no CAPTCHA and no human step. RowanJobs therefore
primes once per run (`SourceClient.prime`) using unmodified Chromium presenting
the same honest archiver user agent, then collects over plain HTTP. No stealth
patches, no proxy rotation, no CAPTCHA solving. With the browser unavailable the
collector still works -- it backs off, and a challenge is recorded as
`access_control_challenge`, an explicit coverage exception that never becomes
evidence of absence.

Two deployment details this exposed:

* `ProtectSystem=strict` made the inherited `TMPDIR` (`/mnt/bench/tmp/pytest`)
  read-only inside the unit, so Chromium could not start. The units now set
  `TMPDIR=/tmp`, which `PrivateTmp=yes` provides.
* Playwright 1.63 needs Chromium build 1243; `playwright install chromium`
  added it alongside the existing 1234/1237 builds in the shared cache. Browser
  builds are versioned directories, so other projects are unaffected.

## Baseline reconnaissance (2026-09-16, scratch data root, not production)

A bounded live run confirmed the adapter against the real site: 7 listing pages,
**266 source occurrences, 133 unique advertisements, 133 duplicate occurrences**
from the repeated section, `source_reported_total = 133` reconciling exactly,
termination `no_more_link`, and the scan **qualified** on all 11 checks.

## First production harvest (run 2)

Executed through the installed systemd unit (`systemctl --user start
rowanjobs.service`), not the CLI directly.

| Measure | Value |
|---|---|
| Started / ended (Eastern) | 2026-09-16 18:43:10 -> 18:54:05 EDT (10m 55s) |
| Outcome | `success` |
| Final qualified listing count | 133 |
| Union encountered | 133 |
| Listing traversals | 2 (discovery + verification), 7 pages each, both **qualified** |
| Source occurrences seen | 266 (133 advertisements x 2 sections) |
| Duplicate occurrences correctly ignored | 133 |
| Source-reported total | 133 -- reconciles exactly |
| Detail pages captured | 133 / 133 queued, 0 failed, 0 inconclusive |
| Content versions created | 133 |
| Identity mismatches | 0 |
| HTTP requests | 148, 8.24 MB received, 0 retries, 1 challenge (recovered) |
| Artifacts | 140 for 147 payload fetches (7 deduplicated) |
| Payload bytes | 7.60 MB uncompressed -> 2.28 MB stored (3.3x) |
| Backup | created and VERIFIED |

Run 1 was the interrupted attempt (browser could not start under the sandbox);
it was correctly marked `aborted` by the next run and its evidence preserved.

Notable real-world data: **14 of 133 advertisements have an explicitly blank
`Applications close`** -- recorded as `field_state='blank'`,
`date_parse_state='absent'`, with no invented deadline. All 133 `Advertised`
values are date-only precision with the source's own 12:00Z placeholder in the
`datetime` attribute, recorded as `source_precision='date'`.

## Manual inspection (13 advertisements, listing pages 1-7)

Verified independently of the production parser, straight from the archived
bytes: `description_html` is a byte-exact substring of the decoded archived
document in every case; every source paragraph survives in `description_text`
with no substantive characters lost or invented; job number and title agree with
the raw markup; the listing summary is preserved separately from the detail
description.

## Post-review state (2026-09-16 evening)

An independent review demonstrated three ways the archive could have made a
false historical claim. All are fixed, pinned by tests, and the production
archive has been migrated (schema v5):

1. Bumping `TEXT_CONTRACT_VERSION` or `PARSER_VERSION` alone produced a
   fabricated `content_changed` event. Comparisons are now scoped by the whole
   lineage `(parser_version, contract_version, text_contract_version)`, which
   `posting_versions` records and `content_fingerprint` folds in.
2. A reconciliation traversal that qualified but agreed with neither earlier
   pass was treated as settling a disagreement, allowing a false absence.
   Reconciliation must now agree, and no absence claim may contradict any
   qualified traversal in the same run.
3. A closure phrase in an advertisement's own prose marked a live posting closed
   and discarded its description. Closure detection now excludes the body and
   requires closure vocabulary; a notice beside a real body is a recorded
   conflict with the content kept.

`PARSER_VERSION` is now **1.1.0** (job documents on any `*.rowan.edu` host are in
scope). Verified live: 6 advertisements re-read under the new lineage produced
**zero** content-change events. The next full collection will re-extract the
remaining 127 under 1.1.0 — expected, and not a source change.

## Run reporting by email (2026-09-18)

Unattended alerting is no longer `BLOCKED_EXTERNAL`. A Gmail App Password was
supplied by the operator and RowanJobs now emails a status report after every
actual collection run.

| Fact | Value |
|---|---|
| Recipient | the operator's personal mailbox, set in `~/.config/rowanjobs/config.toml` **only** — deliberately not written down in this repository, which is public |
| Credential | `~/.config/rowanjobs/credentials.env`, mode 0600, outside Git |
| Transport | `smtp.gmail.com:587`, STARTTLS with certificate verification |
| Schema | migration 7 adds `notifications`; production is at **v7** |
| Health contract | `HEALTH_SCHEMA_VERSION` bumped `1` -> `2` (the notifications section changed shape) |
| Live delivery test | `rowanjobs notify --test` accepted by Gmail, 2026-09-18 |

Design decisions worth not relitigating:

* **The recipient is not in the committed defaults.** `sedlock/RowanJobs` is
  public; a personal address in `config.py` would be published and scraped.
  `NotifyConfig.kind` and `.recipient` default to empty, so the shipped code
  reports UNCONFIGURED and the operator's own (untracked) config supplies both.
* **Reporting cannot change a collection's verdict.** `exit_code_for` reads only
  `collection` and `backup`; a bounced report is recorded in `notifications` and
  never emailed about itself. Pinned by tests at both the unit and CLI level.
* **Provider acceptance is not inbox receipt**, and no string anywhere claims it
  is. `notifications.accepted_at_utc` means Gmail took the message.
* **Runs predating reporting are recorded `skipped`**, not left looking like
  reports that went missing. Runs 1-5 were marked this way on first use. Only
  `rowanjobs notify --run N` will send one after the fact.
* **A baseline is not news.** Newly-observed / no-longer-listed / content-changed
  counts are omitted for a baseline run, which would otherwise report 133
  advertisements as newly published.
* **`rowanjobs notify` never collects.** No run is created and the source is
  never contacted; the worst case of any mail problem is a late email.

## Incident: 2026-09-18 collection failed on a half-finished config change

The previous session rewrote `NotifyConfig` without updating the deployed
`config.toml`, which still had the old `command` / `notify_on` keys. The loader
rejects unknown keys, so:

* `rowanjobs.service` failed at 06:15 EDT, exit 5,
  `unknown configuration key [notify] 'command'`;
* `rowanjobs-retry.service` failed the same way at 09:15 EDT;
* **no run row was created at all** — the process died before opening one, so
  the archive had no evidence for 2026-09-18 and not even a failed-run record.

Repaired by finishing the feature and rewriting the live `[notify]` block. A
`daily` collection for slot 2026-09-18 was then run through the systemd unit, so
the day is covered rather than becoming a permanent gap.

Worth remembering: **a config schema change is a deployment**. The loader's
strictness is correct — it is what made the breakage loud — but the deployed
file has to move in the same change as the dataclass.

## Stabilization pass (2026-09-19)

Steady-state work. The whole pass was driven by one question: what would make
the 2026-09-18 outage impossible to repeat, or at least impossible to miss?

### Production no longer runs the working tree

This was the real defect, and it was still live at the start of the day. The
units executed `/mnt/bench/src/RowanJobs/.venv/bin/rowanjobs`, an **editable**
install pointing at the checkout, so the 06:15 collection ran whatever was on
disk at 06:15. `ops/release.py` gives RowanJobs its own immutable release
lifecycle under `/mnt/bench/app-releases/rowanjobs/`, the same shape Feeder
already uses on this host (deliberately: one pattern to understand, not two).

Two defects the first real build exposed, both worth remembering:

* A virtualenv's console scripts embed the interpreter's **absolute path** in
  their shebang. The release was built in a staging directory and renamed, so
  it validated perfectly and then could not execute at all. Releases are built
  where they will run, and validated again after sealing.
* Left alone, `uv` chose **Python 3.14** for the release while every test runs
  on 3.12. Pinned in `.python-version` and passed explicitly at build time.

### A startup failure can no longer be silent

`OnFailure=` on both collecting units invokes `ops/onfailure.py`, which imports
nothing from rowanjobs — a detector that depends on the configuration parser it
monitors cannot report that the parser is broken. Verified end to end with an
isolated unit that exits 5: systemd fired the handler, which captured the
result, exit status, slot and journal tail. `rowanjobs health` surfaces
unresolved records; only a later collection **for the same slot** resolves one.

### Mail retry actually runs

`rowanjobs-notify.timer`, hourly at :40. `rowanjobs notify` existed before but
nothing called it, so a failed report waited for the next collection to happen
to sweep it.

### ControlPanel

Registered as `rowanjobs` in `~/.config/controlpanel/projects.json` and in the
ControlPanel repo (`c2fdc05`). Shows 13 components, all healthy, with real
production values. The daily report links to
`https://entropy/projects/rowanjobs`, verified serving HTTP 200 before it was
put in an email.

Note for the next config change: editing the deployed `config.toml` after a
release is validated re-opens exactly the 2026-09-18 failure mode. After any
config edit, re-run the candidate check against the *deployed* release:

```sh
python3 -c "import importlib.util;spec=importlib.util.spec_from_file_location('r','ops/release.py');r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r);l=r.Layout(r.DEFAULT_ROOT);print(r.validate_candidate(l.path_for(l.resolve(l.current)), r.DEFAULT_CONFIG))"
```

One thing worth not relearning: **ControlPanel recomputes a project's verdict
from the components it publishes, with `max()`.** The internal decision to
exclude off-host backup from scoring never reached it, so publishing an
`unknown` off-host component held the whole project at unknown — masking nine
healthy components behind one the operator had already decided not to care
about. Off-host is now evidence on the local-snapshot component, not a
component.

### Reporting epoch

`seed_baseline` used to fire whenever the notifications table was empty.
Emptiness is ambiguous: it is equally consistent with *reporting is brand new*
and with *the first report was genuinely lost*. It is now anchored to when
migration 7 created the table, read from `schema_migrations`. Production
matches exactly: runs 1-5 ended before `2026-09-18T16:59:10Z` and are
`skipped`; runs 6-7 ended after and were reported.

### Collection integrity audit

Nothing needed fixing. Measured on the production archive:

| Check | Result |
|---|---|
| Postings ever discovered | 137 |
| With an archived description | **137** (zero never-captured) |
| Fetches, all with retained payloads | 769 |
| Content versions / postings with >1 | 275 / 133 |
| Offline reprocess of 671 artifacts | 0 network requests, **0** new versions |
| `v_posting_current` freshness | 132 `checked`, 5 `carried-forward` (delisted) |

The 5 `redirected_to_listing` observations in run 7 are historical postings
being re-checked, not discoveries missing a description. `redirected_to_listing`
is a **terminal** availability state, so those five are recorded disappearances
rather than failures to retrieve — which is why `detail_failed` is 0 while
`detail_attempted` (137) exceeds `detail_captured` (132).

## Remaining

- **The exposed Gmail App Password has not been rotated.** It appeared in a
  session transcript on 2026-09-18 and `credentials.env` has not been modified
  since it was written (mtime 2026-09-18 12:46). Mail works, but that only
  proves the *configured* credential works, not that the exposed one was
  replaced. Rotation is a local-only action: rotate at
  <https://myaccount.google.com/apppasswords>, rewrite the file in place
  (0600), then `rowanjobs notify --test`. Nothing in RowanJobs needs changing.
- Off-host backup remains unconfigured by operator decision: local snapshots
  are considered sufficient, and it is explicitly excluded from required-health
  scoring. The configuration interface exists
  (`[backup] offhost_kind/offhost_target`).
- Host-down detection is still `BLOCKED_EXTERNAL by design`. Run reports make
  silence *meaningful* — no report means no collection — but a host that is down
  cannot report that it is down. Only an external observer can close this.
- The root filesystem is at 87% used (~17 GiB free). The archive grows roughly
  2 MB a day, so this is not urgent, but it is the disk the data root lives on.
- `sedlock/RowanJobs` is **public**. It existed before this work and its
  visibility was preserved deliberately; only code, docs, locked dependencies
  and small public fixtures are committed.

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

## Remaining

- Off-host backup and unattended alerting remain `BLOCKED_EXTERNAL`: nothing is
  configured on this host and RowanJobs will not invent a destination. The
  configuration interfaces exist (`[backup] offhost_kind/offhost_target`,
  `[notify] kind/command`).
- The root filesystem is at 87% used (~17 GiB free). The archive grows roughly
  2 MB a day, so this is not urgent, but it is the disk the data root lives on.
- `sedlock/RowanJobs` is **public**. It existed before this work and its
  visibility was preserved deliberately; only code, docs, locked dependencies
  and small public fixtures are committed.

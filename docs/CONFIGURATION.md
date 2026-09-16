# Configuration

Configuration is a single TOML file. **Every key has a documented default, so
the collector runs correctly with no configuration file at all**; a missing file
is not an error (`src/rowanjobs/config.py::load_config`).

Default location: `~/.config/rowanjobs/config.toml`
(`$XDG_CONFIG_HOME/rowanjobs/config.toml` when that variable is set).

An unknown table or an unknown key inside a known table is a **hard error**, not
a warning — a typo must not silently leave a policy at its default.

## Resolution order

| Setting | Precedence, highest first |
|---|---|
| Config file | `--config PATH` → `$ROWANJOBS_CONFIG` → `$XDG_CONFIG_HOME/rowanjobs/config.toml` → `~/.config/rowanjobs/config.toml` |
| Data root | `--data-root PATH` → `data_root` in the config file → `$ROWANJOBS_DATA_ROOT` → `$XDG_DATA_HOME/rowanjobs` → `~/.local/share/rowanjobs` |
| Database path | `--db PATH` → `db_path` in the config file → `$ROWANJOBS_DB` → `<data_root>/rowanjobs.db` |

`--config`, `--data-root` and `--db` are **global** flags and must precede the
subcommand: `rowanjobs --data-root /tmp/scratch collect`, not
`rowanjobs collect --data-root /tmp/scratch`. The same applies to `--json`.

Environment variables are read in `src/rowanjobs/paths.py`
(`ENV_CONFIG = "ROWANJOBS_CONFIG"`, `ENV_DATA_ROOT = "ROWANJOBS_DATA_ROOT"`,
`ENV_DB = "ROWANJOBS_DB"`). The systemd units set `ROWANJOBS_CONFIG` and
`ROWANJOBS_DATA_ROOT` explicitly so unattended runs never depend on `~`
expansion.

## Configuration hashing

The **effective** configuration — the `network`, `browser`, `collection` and
`schedule` sections, i.e. only the parts that change what a run *attempted* — is
serialised canonically and SHA-256 hashed into `source_configs.config_hash`, and
the full JSON is stored in `source_configs.config_json`. Local filesystem paths
are deliberately excluded so the same collection policy hashes identically on a
restore host. `[backup]` and `[notify]` are **not** part of the hash: they do not
change what was collected.

---

# Keys

## Top level

| Key | Default | Meaning |
|---|---|---|
| `data_root` | `~/.local/share/rowanjobs` | Root of all mutable state. Created mode 0700. |
| `db_path` | `<data_root>/rowanjobs.db` | The archive file. Created mode 0600. |

## `[network]` — source-traffic policy

One `RequestBudget` governs every live request the application makes
(`src/rowanjobs/net/budget.py`), which is what makes the pacing guarantee real
rather than per-module wishful thinking.

| Key | Default | Meaning |
|---|---|---|
| `concurrency` | `1` | One live request at a time. |
| `min_interval_seconds` | `1.5` | Minimum gap between live requests. Widened automatically after a challenge. |
| `connect_timeout_seconds` | `10.0` | TCP/TLS connect timeout. |
| `read_timeout_seconds` | `30.0` | Response read timeout. |
| `write_timeout_seconds` | `10.0` | Request write timeout. |
| `pool_timeout_seconds` | `10.0` | Connection-pool acquisition timeout. |
| `max_retries` | `3` | Retries for *transient* failures only: connect/read timeouts, DNS, TLS, HTTP 5xx, HTTP 429. A 404 is not retried. |
| `backoff_base_seconds` | `2.0` | Retry delay is `base ** attempt`, capped below. |
| `backoff_max_seconds` | `120.0` | Ceiling on a retry delay. An explicit `Retry-After` may raise the delay above the computed value. |
| `jitter_seconds` | `0.75` | Uniform random addition to the interval, to de-synchronise request timing. |
| `challenge_backoff_seconds` | `45.0` | Base wait after an access-control challenge; the nth consecutive challenge waits `45 × 2^(n-1)`. |
| `max_consecutive_challenges` | `4` | After this many challenges in a row the run raises `ChallengeWall` and **stops requesting** rather than hammering the source. |
| `max_requests_per_run` | `1200` | Hard ceiling on live requests in one run. Exceeding it fails the fetch with `budget_exhausted`. Protects the source and us. |
| `user_agent` | `RowanJobsArchiver/1.0 (+https://github.com/sedlock/RowanJobs; contact sedlock@rowan.edu)` | Honest, identifiable, with a contact address. Also used by the browser step, so the two transports make the same claim about who we are. |
| `accept_language` | `en-US,en;q=0.9` | `Accept-Language` header. |
| `http2` | `true` | Enable HTTP/2. |
| `max_response_bytes` | `26214400` (25 MiB) | Ceiling for a page. A larger response is stored as an explicit `capture_state='partial'` prefix with a recorded coverage exception — never silently truncated and called complete. |
| `max_resource_bytes` | `52428800` (50 MiB) | Ceiling for a linked document. Exceeding it yields resource outcome `too_large`. |
| `max_redirects` | `5` | Redirects are followed **manually** so every hop is guarded and preserved as evidence. |
| `allowed_hosts` | `["jobs.rowan.edu", "careers-static.pageuppeople.com"]` | The only hosts a connection may be opened to. Exact matches; subdomains are not implied. Every URL, including every redirect target, is checked before a socket is opened (`src/rowanjobs/net/guard.py`). |
| `resource_hosts` | `["jobs.rowan.edu"]` | Hosts whose documents count as job-specific resources worth archiving. |

## `[browser]` — optional challenge step

Used **only** to satisfy an AWS WAF *challenge* the way an ordinary public
visitor's browser does, and only after plain HTTP has already been challenged
and backed off. No stealth plugins, no proxy rotation, no CAPTCHA solving. See
`docs/SECURITY.md`. Collection remains correct with this disabled.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Whether to attempt the browser step at all. If Playwright is not installed, the solver reports unavailable and is simply not used. |
| `channel` | *(unset)* | Playwright browser channel, e.g. `"chrome"`. Unset uses the bundled Chromium. |
| `headless` | `true` | Run headless. |
| `nav_timeout_seconds` | `45.0` | Page navigation timeout. |
| `token_ttl_seconds` | `1800.0` | Reuse an obtained token for this long before re-solving. |
| `max_solves_per_run` | `6` | Ceiling on browser solves in one run. |

## `[collection]` — what is collected and how the traversal behaves

| Key | Default | Meaning |
|---|---|---|
| `base_url` | `https://jobs.rowan.edu` | Site root. |
| `listing_path` | `/en-us/listing/` | Unfiltered listing path. |
| `locale` | `en-us` | Locale segment used when constructing a detail URL from a job id. |
| `page_items` | `20` | `page-items` query value. |
| `max_listing_pages` | `200` | Safety bound on pagination. **Hitting it marks the scan unqualified** (`within_page_bound` fails), because the traversal was cut short by us rather than ended by the source. |
| `scope_label` | `all-unfiltered` | Recorded description of what was in scope. |
| `comparability_group` | `v1-unfiltered-en-us` | Absence history is only comparable within one group. **Bump this whenever the collected scope changes.** |
| `verification_scan` | `true` | Perform the second complete listing traversal (step 4). |
| `reconciliation_scan` | `true` | Perform a third bounded traversal when the first two disagree (step 6). |
| `collect_resources` | `true` | Fetch job-specific documents linked from the description. |
| `terminal_observations_before_weekly` | `3` | Consecutive *terminal* observations before an unlisted posting moves to the weekly recheck tier. Uncertain outcomes never advance this. |
| `weekly_recheck_interval_days` | `7` | Recheck interval in the weekly tier. |
| `max_historical_rechecks_per_run` | `120` | Ceiling on historical (unlisted) rechecks per run. Deferred rechecks are recorded as a `historical_recheck_deferred` coverage gap, not silently skipped. |

## `[schedule]`

| Key | Default | Meaning |
|---|---|---|
| `hour` | `6` | Slot hour, local. |
| `minute` | `15` | Slot minute, local. |
| `timezone` | `America/New_York` | Timezone of the slot, and of every `*_local_date`. Used to build the systemd calendar expression. |
| `max_retries_per_slot` | `2` | Bound on same-day retries. A retry inherits the parent's slot, so it can never create an extra daily observation. |

## `[backup]`

See `docs/BACKUP_RESTORE.md`.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Take snapshots after a collection that added evidence. |
| `keep_daily` | `7` | Daily snapshots retained. |
| `keep_weekly` | `4` | Weekly snapshots retained (one per ISO week). |
| `keep_monthly` | `12` | Monthly snapshots retained (one per calendar month). |
| `verify_after_backup` | `true` | Run `integrity_check` and `foreign_key_check` on the snapshot before promoting it to final. A snapshot that fails is kept as `.db.unverified` and reported as `FAILED`. |
| `restore_check_interval_days` | `7` | How often a **full restore-and-query verification** is performed. |
| `offhost_kind` | `""` | `""` (none), `"rclone"`, `"rsync-ssh"` or `"command"`. Empty means **no off-host protection is configured**, and protection is reported `UNCONFIGURED` rather than assumed. |
| `offhost_target` | `""` | Destination prefix, e.g. an rclone remote or an `rsync` target. |
| `offhost_namespace` | `"rowanjobs"` | Path segment appended under the target. |
| `offhost_command` | `[]` | argv for `offhost_kind = "command"`. `{src}` and `{dst}` are substituted. Run without a shell. |

## `[notify]`

RowanJobs will not borrow another application's credentials or invent a
recipient. With nothing configured, notifications report `UNCONFIGURED` in the
health output and nothing is sent — a visible status rather than pretended
delivery (`src/rowanjobs/ops/notify.py`).

| Key | Default | Meaning |
|---|---|---|
| `kind` | `""` | `""` (none) or `"command"`. |
| `command` | `[]` | argv, run without a shell. The alert JSON arrives on **stdin**. 60-second timeout. |
| `notify_on` | `["failed", "partial"]` | Which run outcomes trigger an alert. |

---

# Annotated example `config.toml`

Every value shown is the default; a real file only needs the lines you actually
change.

```toml
# ~/.config/rowanjobs/config.toml
#
# Every key here has a default. Delete anything you are not changing.
# Unknown tables and unknown keys are errors, not warnings.

# Root of all mutable state. Created mode 0700.
data_root = "~/.local/share/rowanjobs"
# The archive itself. Payloads live inside it. Created mode 0600.
db_path   = "~/.local/share/rowanjobs/rowanjobs.db"


[network]
# One live request at a time, everywhere in the application.
concurrency            = 1
# The source's WAF challenged the audit after roughly seven requests in about
# ninety seconds (docs/SOURCE_ADAPTER_AUDIT.md). Pace conservatively.
min_interval_seconds   = 1.5
jitter_seconds         = 0.75

connect_timeout_seconds = 10.0
read_timeout_seconds    = 30.0
write_timeout_seconds   = 10.0
pool_timeout_seconds    = 10.0

# Transient failures only: timeouts, DNS, TLS, 5xx, 429.
max_retries          = 3
backoff_base_seconds = 2.0
backoff_max_seconds  = 120.0

# After a challenge: wait 45s, then 90s, then 180s... and stop after four in a
# row rather than hammering the source.
challenge_backoff_seconds = 45.0
max_consecutive_challenges = 4

# Hard ceiling on live requests in one run.
max_requests_per_run = 1200

# Honest and contactable. The browser step uses this same string.
user_agent = "RowanJobsArchiver/1.0 (+https://github.com/sedlock/RowanJobs; contact sedlock@rowan.edu)"
accept_language = "en-US,en;q=0.9"
http2 = true

# Oversize responses are stored as an explicit partial prefix, never silently
# truncated and called complete.
max_response_bytes = 26214400   # 25 MiB
max_resource_bytes = 52428800   # 50 MiB

# Redirects are followed manually so every hop is guarded and archived.
max_redirects = 5

# The only hosts a socket may be opened to. Exact match; subdomains are not
# implied. Checked again on every redirect target.
allowed_hosts  = ["jobs.rowan.edu", "careers-static.pageuppeople.com"]
resource_hosts = ["jobs.rowan.edu"]


[browser]
# Only ever used to satisfy an ordinary AWS WAF challenge, and only after the
# HTTP client has already slowed down. Not stealth, not proxy rotation, not
# CAPTCHA solving. Collection is still correct with enabled = false.
enabled             = true
headless            = true
# channel           = "chrome"        # unset = bundled Chromium
nav_timeout_seconds = 45.0
token_ttl_seconds   = 1800.0
max_solves_per_run  = 6


[collection]
base_url     = "https://jobs.rowan.edu"
listing_path = "/en-us/listing/"
locale       = "en-us"
page_items   = 20

# A safety bound, not an expectation. Reaching it makes the scan UNQUALIFIED,
# because the traversal was stopped by us rather than by the source.
max_listing_pages = 200

scope_label = "all-unfiltered"
# Absence history is only comparable inside one group. Change the scope, change
# this string, and do not join series across the boundary.
comparability_group = "v1-unfiltered-en-us"

# The second and third traversals (workflow steps 4 and 6).
verification_scan   = true
reconciliation_scan = true

# Fetch job-specific documents linked from the description body.
collect_resources = true

# An unlisted posting keeps being checked daily until it has been terminal this
# many times in a row. Challenges and failures never advance that streak.
terminal_observations_before_weekly = 3
weekly_recheck_interval_days        = 7
# Deferred rechecks are recorded as a coverage gap, not silently skipped.
max_historical_rechecks_per_run     = 120


[schedule]
hour     = 6
minute   = 15
timezone = "America/New_York"
# A retry inherits its parent's slot, so it can never add a daily observation.
max_retries_per_slot = 2


[backup]
enabled = true
keep_daily   = 7
keep_weekly  = 4
keep_monthly = 12
# integrity_check + foreign_key_check before a snapshot is promoted to final.
verify_after_backup = true
# Full restore-and-query verification cadence.
restore_check_interval_days = 7

# No off-host destination is configured on this host. Leaving offhost_kind empty
# makes the health output report UNCONFIGURED, which is the truth; it does not
# pretend the archive is protected against losing the machine.
offhost_kind      = ""          # "", "rclone", "rsync-ssh", "command"
offhost_target    = ""
offhost_namespace = "rowanjobs"
offhost_command   = []          # argv for kind="command"; {src} and {dst} substituted


[notify]
# Left unconfigured on purpose: RowanJobs will not borrow another application's
# credentials or invent a recipient. Unconfigured is reported, not hidden.
kind      = ""                  # "" or "command"
command   = []                  # argv, no shell; the alert JSON arrives on stdin
notify_on = ["failed", "partial"]
```

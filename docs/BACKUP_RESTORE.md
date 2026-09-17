# Backup, restore and verification

Implementation: `src/rowanjobs/ops/backup.py`. Configuration: the `[backup]`
table (`docs/CONFIGURATION.md`).

Three things are tracked **separately**, because they fail independently and
collapsing them would hide exactly the failure that matters:

| Question | Answered by |
|---|---|
| Does the live database still hash-verify? | **archive integrity** — `rowanjobs verify` |
| Is there a recent verified snapshot on this host? | **local backup health** — `backup.local.state` |
| Is there a copy that survives losing this host? | **off-host protection** — `backup.offhost.state` |

---

## Design

### SQLite online backup API, not a file copy

Snapshots are taken with SQLite's online backup API
(`apsw.Connection.backup`), stepped 256 pages at a time. Copying only the active
`.db` file while a WAL is present yields a database **missing its most recent
committed transactions** — which is the one thing a backup must never do.

After the copy the target is checkpointed `TRUNCATE` and its journal mode is set
to `DELETE`, and any stray `-wal`/`-shm` sidecars are removed, so the resulting
snapshot is a **single self-contained file**. Payloads live inside the database,
so that one file is the whole archive.

### Atomic completion

The snapshot is written to `rowanjobs-<kind>-<stamp>.db.partial` and only
`replace()`d onto its final name after verification succeeds. A reader — or the
rotation logic — therefore never sees a half-written file as final. The final
file is `chmod 0600`.

If verification **fails**, the partial is renamed to `.db.unverified` rather than
deleted (so it can be examined) and the result is reported `FAILED`. It is never
promoted to a usable snapshot and never counted as protection.

### Manifests

Every snapshot gets a sidecar `…​.db.manifest.json`, written atomically
(`ops/atomic.py`), containing:

| Field | Meaning |
|---|---|
| `manifest_version` | Manifest format version (1) |
| `application`, `app_version` | What wrote it |
| `schema_version` | Migration version inside the snapshot |
| `kind` | `daily`, `weekly`, `monthly`, `manual`, `predeploy` |
| `snapshot_at_utc` | **Snapshot time** |
| `source_database`, `file` | Where it came from, what it is called |
| `file_bytes` | **Size** |
| `sha256` | **Checksum** of the final file |
| `last_run_id`, `artifact_count`, `posting_count`, `observation_count` | Archive contents at snapshot time |
| `verification` | **Verification outcome**: `{state, detail, verified_at_utc}` |
| `offhost` | `{state, target, detail}` |

The same facts are recorded as a row in the `backups` table, so the archive
itself knows what protection exists.

### Verification at creation

With `verify_after_backup = true` (the default) the candidate snapshot is opened
**read-only** and checked before promotion:

- `PRAGMA integrity_check` must return exactly `ok`;
- `PRAGMA foreign_key_check` must return no rows;
- the table count is recorded.

`verification.state` is then `OK` and the snapshot is reported `VERIFIED`.

With `verify_after_backup = false` the manifest records `SKIPPED` and the result
state is **`UNVERIFIED`** — not `VERIFIED`. A snapshot nobody checked is a file,
not a guarantee, and calling it verified would be the archive asserting something
it never established. `BackupResult.ok` is true only for `VERIFIED`, so
`rowanjobs backup` exits 3 (protection degraded) in that case;
`BackupResult.usable` is the separate, weaker statement that a snapshot exists
at all. Only an `OK` snapshot counts for rotation or as the `latest()` restore
source (below).

### When backups are taken

`rowanjobs collect` takes a `daily` snapshot after any run whose outcome was
`success` **or** `partial` — i.e. after any ingestion that added evidence —
unless `--no-backup` was given. **A backup failure never rolls back ingestion**;
it is reported and the collection stands. That is why exit code 3 exists
separately from 1 and 2.

---

## Rotation

`BackupManager.prune`, driven by `keep_daily` / `keep_weekly` / `keep_monthly`
(defaults 7 / 4 / 12, all configurable).

Only snapshots whose `verification_state` is `OK` count as verified here. An
`UNVERIFIED`/`SKIPPED` snapshot can never be the reason an older verified one is
dropped.

Snapshots are considered newest-first. A snapshot is kept if it is:

- the **newest verified** snapshot (always, unconditionally);
- among the newest `keep_daily` snapshots;
- the newest snapshot in an ISO week not yet represented, up to `keep_weekly`;
- the newest snapshot in a calendar month not yet represented, up to
  `keep_monthly`.

Everything else has its file and its manifest unlinked and its `backups` row
stamped with `pruned_at_utc`; the row itself is kept, so the history of what
existed is not lost.

**Rotation never drops the last verified copy.** If one or fewer verified
snapshots exist, `prune` returns immediately and removes nothing, whatever the
retention numbers say.

---

## Running a backup

```sh
rowanjobs backup                      # kind=manual
rowanjobs backup --kind predeploy     # before a deployment
rowanjobs --json backup               # machine-readable
```

Output reports the state, the file, its sha256 and size, anything pruned, and
the off-host state. Exit 0 if `VERIFIED`; `UNVERIFIED` (verification switched
off) and `FAILED` both exit 3, protection degraded.

## Verifying

```sh
rowanjobs verify                      # live archive: integrity, FKs, payload hashes
rowanjobs verify --limit 500          # sample the payload hashes
rowanjobs verify --restore            # ALSO restore the latest snapshot to a temp dir
```

`verify` checks:

1. `PRAGMA integrity_check` on the live archive;
2. `PRAGMA foreign_key_check` (reported as a violation count);
3. every archived payload: decompress it and confirm it hashes back to its stored
   `sha256` (`src/rowanjobs/archive/store.py::verify_all`). A length mismatch
   after decompression is also an error. The same check runs on **every** read:
   `ArchiveStore.get` re-hashes what it decompressed before returning it, so a
   corrupted payload raises rather than being handed to a parser or an export.

With `--restore` it additionally performs the full restore check below and writes
the outcome to `<data_root>/runtime/restore-verification.json`.

Exit 0 if everything passed, otherwise 1.

## Restoring safely

```sh
# Latest verified snapshot -> a NEW location
rowanjobs restore /tmp/rowanjobs-restored.db

# A specific snapshot
rowanjobs restore /tmp/check.db --source ~/.local/share/rowanjobs/backups/rowanjobs-daily-20260916T061500Z.db
```

Safety properties:

- **The live archive is never overwritten.** `cmd_restore` refuses when the
  destination resolves to the configured database path, and `restore_to` refuses
  to overwrite any existing file (`FileExistsError`).
- The copy is `chmod 0600`.
- The copy is opened once **read-only**, to prove it opens cleanly, before being
  handed back. Read-only matters: opening it read-write would leave `-wal`/`-shm`
  sidecars beside it and the restored snapshot would stop being the single
  self-contained file it was created as.
- The restored file is then put through the same restore check as the periodic
  verification, in a temporary directory — `restore_check` writes its own working
  copy, and dropping that next to the restore target would leave a stray database
  behind — and the per-check results are printed.
- Without `--source`, the snapshot restored is the most recent one whose
  verification state is `OK` (`BackupManager.latest`); an unverified snapshot is
  never selected silently.

To actually put a restored snapshot into service, stop the timers, move the
current archive aside (do not delete it), copy the restored file into place, and
run `rowanjobs doctor` and `rowanjobs verify` before re-enabling the timers:

```sh
systemctl --user stop rowanjobs.timer rowanjobs-retry.timer
mv ~/.local/share/rowanjobs/rowanjobs.db ~/.local/share/rowanjobs/rowanjobs.db.superseded-$(date -u +%Y%m%dT%H%M%SZ)
cp /tmp/rowanjobs-restored.db ~/.local/share/rowanjobs/rowanjobs.db
chmod 0600 ~/.local/share/rowanjobs/rowanjobs.db
rowanjobs doctor && rowanjobs verify
systemctl --user start rowanjobs.timer rowanjobs-retry.timer
```

Note what a restore costs: everything collected **after** the snapshot is gone
from the live archive. That is why the superseded file is moved aside rather than
deleted — the two can be compared afterwards.

## The restore check

`BackupManager.restore_check` copies the snapshot into a working directory,
opens it **read-only**, and records a pass/fail for each of:

| Check | What it proves |
|---|---|
| `snapshot_checksum` | The file still hashes to the value in its manifest (or notes that there is no manifest to compare against) |
| `integrity_check` | SQLite's own structural check passes |
| `foreign_key_check` | No dangling references |
| `artifact_payload_hashes` | The 25 most recent payloads decompress and hash correctly |
| `posting_histories_queryable` | Real posting histories can be read back, with their observation and version counts |
| `descriptions_survived` | Recent description versions are present and non-empty, with their `description_html_kind` |
| `resource_references_survived` | Classified links and resource retrievals are present |

It passes only if every check passes. This is a **restore-and-query**
verification, not merely "the file opens": it demonstrates that the archived
descriptions and histories are actually retrievable from the copy.

### Cadence

Driven by `backup.restore_check_interval_days` (default **7**). After a
collection's backup, `restore_check_due()` compares today's local date with the
date in `<data_root>/runtime/restore-verification.json`; if the interval has
elapsed — or the file is missing or unreadable — a full restore check runs into a
temporary directory and the result is written back atomically:

```json
{
  "checked_at_utc": "...",
  "checked_at_local": "...",
  "source": "<snapshot path>",
  "ok": true,
  "detail": "restored snapshot verified",
  "checks": [ { "name": "...", "passed": true, "detail": "..." } ]
}
```

`rowanjobs status` prints the last result; the health JSON carries it under
`backup.restore_verification`.

---

## Off-host protection

**No off-host destination is configured on this host.**

`EXECUTION_STATE.md` records the verified situation: no rclone, restic, borg, b2
or aws tooling is installed, and no ssh remotes are configured. `/mnt/bench` —
where the repository lives — is a **locally attached** 1.8 TB USB SSD on
`/dev/sda1`, and the data root is on `/dev/nvme0n1p5`. Neither is off-host.
Another directory or a second local disk is not a second site; losing the machine
loses both.

### The interface exists

The configuration surface is in place and is used the moment it is populated
(`[backup]` in `docs/CONFIGURATION.md`):

| Key | Purpose |
|---|---|
| `offhost_kind` | `""` (none), `"rclone"`, `"rsync-ssh"`, `"command"` |
| `offhost_target` | Destination prefix, e.g. `remote:bucket` or `user@host:/path` |
| `offhost_namespace` | Path segment appended under the target (default `rowanjobs`) |
| `offhost_command` | argv for `kind = "command"`; `{src}` and `{dst}` are substituted |

The upload destination is `"{offhost_target}/{offhost_namespace}/{filename}"`.
Commands are run **without a shell**, with a 30-minute timeout, and
`push_offhost` explicitly refuses an unknown `offhost_kind` rather than guessing.

### It is reported as UNCONFIGURED, not assumed

With `offhost_kind` empty, `push_offhost` returns:

```json
{"state": "UNCONFIGURED", "target": null,
 "detail": "no off-host backup destination is configured; the archive has local
            backups only and would not survive losing this host"}
```

and `BackupManager.status` reports the same in the health JSON. That is the point:
`UNCONFIGURED` is a visible, honest statement that this protection does not
exist. It is **not** counted as a failure (it does not force exit code 3), and it
is **not** quietly rendered as protected. `PROTECTION_STATES` in
`src/rowanjobs/constants.py` keeps `UNCONFIGURED` distinct from `VERIFIED`,
`DEGRADED`, `BLOCKED_EXTERNAL` and `FAILED` for exactly this reason.

### To configure it later

Set `offhost_kind`, `offhost_target` and, for `command`, `offhost_command`; make
sure the credentials the chosen tool needs are available to the systemd user
unit's environment (they are not supplied by RowanJobs, and RowanJobs will not
borrow another application's credentials). The next `rowanjobs backup` will
attempt the upload, record the outcome in the manifest and in
`backups.offhost_state`, and surface it in `status`. A failed upload reports
`FAILED` and does raise the protection exit code — once you have claimed off-host
protection exists, its failure matters.

Until then, the honest statement of the archive's durability is: **local verified
snapshots on the same host, with the last verified copy never rotated away, and
no protection against losing the host.**

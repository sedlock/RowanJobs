# RowanJobs

A durable, longitudinal archive of the public job advertisements published on the
Rowan University career site (`https://jobs.rowan.edu`, a PageUp-hosted site).

RowanJobs retrieves the public listing and detail pages once a day, archives the
bytes it received, extracts a verbatim copy of each advertisement, and records
*when* each observation happened and *how good the coverage was at that moment*.
It is an evidence archive, not a job board: it never applies for anything, never
authenticates, and never rewrites what the source published.

## Why

A career site shows only what is advertised right now. Once an advertisement is
edited or withdrawn, the earlier wording — and the fact that it was ever there —
is gone. RowanJobs keeps that history with enough provenance that a future reader
can tell the difference between "this advertisement was removed", "we could not
check that day", and "our parser changed". Those three are routinely confused by
naive scrapers, and confusing them silently corrupts every longitudinal claim
built on the data.

## What it collects

| Collected | Notes |
|---|---|
| Listing pages (all pages, unfiltered) | Archived per page, per traversal |
| Detail page for every advertisement | Full HTML body, archived before parsing |
| The advertisement title, body text and body markup | Verbatim; see `docs/EXTRACTION_CONTRACT.md` |
| Labelled source fields (Job no, Work type, Location, Categories, Advertised, Applications close, and any label the adapter has not seen before) | Source value kept as published; normalisations stored separately |
| Job-specific documents linked from the description body and hosted on `jobs.rowan.edu` | Classified in `src/rowanjobs/extract/pageup_detail.py::classify_link` |
| Every retrieval attempt, including the failures | HTTP status, headers (credentials redacted), timings, redirect chain |

It deliberately does **not** collect: application workflows or anything under
`secure.*.pageuppeople.com` / `/apply/`, applicant data, Rowan internal systems,
or any page that `robots.txt` disallows. See `docs/SECURITY.md`.

The production collector makes **zero** model/LLM calls.

## Install

Requires Python 3.12+ on Linux. Dependencies are locked in `uv.lock`.

```sh
git clone https://github.com/sedlock/RowanJobs
cd RowanJobs
uv sync --frozen             # add --all-extras for the optional browser step
```

The optional `browser` extra installs Playwright, used only to satisfy an
ordinary AWS WAF challenge the way a normal visitor's browser does. Collection
remains correct with it absent (`docs/SECURITY.md`).

## Quick start

```sh
uv run rowanjobs doctor                 # environment, runtime and schema pre-flight
uv run rowanjobs migrate                # create/upgrade the archive schema
uv run rowanjobs collect --kind manual  # one collection, paced and bounded
uv run rowanjobs status                 # what happened, and how trustworthy it is
```

Note the flag position: `--json`, `--config`, `--data-root` and `--db` are
**global** options and must precede the subcommand — `rowanjobs --json status`,
not `rowanjobs status --json` (`src/rowanjobs/cli.py::build_parser`).

## Commands

| Command | What it does |
|---|---|
| `doctor` | Checks host, SQLite runtime and WAL safety, data root, permissions, disk, schema, journal mode, backups, timer, lingering, browser availability, notifications (`src/rowanjobs/ops/doctor.py`) |
| `migrate` | Applies outstanding schema migrations (`src/rowanjobs/db/migrations/`) |
| `collect` | Runs one collection. `--kind daily\|retry\|manual\|verification`, `--max-details`, `--no-verification`, `--no-backup` |
| `retry` | Bounded same-day retry of an incomplete scheduled collection; keeps the parent's scheduled slot |
| `status` | Operational health; `--no-timer` skips systemd inspection |
| `runs` | Recent runs from `v_run_health`; `--limit` |
| `show JOB_ID` | One advertisement: freshness, source fields, URLs, links, description; `--full` |
| `history JOB_ID` | Observation history, derived events and absence evidence; `--limit` |
| `diff JOB_ID` | Unified diff between two archived content versions of the same posting |
| `reprocess [details\|listings\|all]` | Re-parses **archived payloads only**; makes no network requests |
| `backup` | Creates a verified snapshot; `--kind daily\|weekly\|monthly\|manual\|predeploy` |
| `verify` | `integrity_check`, `foreign_key_check`, payload hashes; `--restore` also restores the latest snapshot to a temporary directory |
| `restore DESTINATION` | Restores a snapshot to a new location; refuses to overwrite the live archive |
| `export DATASET` | `postings`, `current`, `history`, `versions`, `observations`, `runs`, `links`; `--format json\|csv`, `--output`, `--job-id`, `--limit` |
| `record-deployment` | Writes the runtime deployment manifest and a `deployments` row |

Exit codes: `0` success, `1` failed, `2` degraded, `3` protection degraded,
`4` lock contention, `5` usage/config error (`src/rowanjobs/cli.py`).

## Where the data lives

Nothing mutable lives in the Git working tree.

```
~/.config/rowanjobs/config.toml      configuration (optional; all keys have defaults)
~/.local/share/rowanjobs/            data root, mode 0700
    rowanjobs.db                     the archive; payloads live inside it, mode 0600
    backups/                         rotated verified snapshots + manifests
    logs/                            reserved (the collector logs to journald)
    runtime/                         collector.lock, health.json, deployment.json,
                                     restore-verification.json
    source-audit/                    manual audit captures (not production history)
    exports/                         CLI export output
```

Paths are defined in `src/rowanjobs/paths.py` and can be overridden with
`ROWANJOBS_CONFIG`, `ROWANJOBS_DATA_ROOT`, `ROWANJOBS_DB` or the global
`--config` / `--data-root` / `--db` flags. See `docs/CONFIGURATION.md`.

## Schedule

One collection per day at **06:15 America/New_York**, with up to two bounded
retry windows (09:15 and 13:15 local) that only act if the day's slot is still
unresolved. Units are in `ops/systemd/` (`rowanjobs.service`, `rowanjobs.timer`,
`rowanjobs-retry.service`, `rowanjobs-retry.timer`); they are templates with
`__VENV__`, `__CONFIG__` and `__DATA_ROOT__` placeholders to substitute at
install time. A missed day stays a coverage gap — a later run cannot reconstruct
it. See `docs/OPERATIONS.md`.

## Documentation

| Document | Contents |
|---|---|
| `CLAUDE.md` | Rules for anyone (human or agent) changing this repository. Read it first. |
| `docs/ARCHITECTURE.md` | Module map, the daily workflow, evidence vs. derived data, durability model |
| `docs/DATA_DICTIONARY.md` | Every table, view and column, with permitted values |
| `docs/SOURCE_ADAPTER_AUDIT.md` | The live source audit of 2026-09-16 and what it established |
| `docs/EXTRACTION_CONTRACT.md` | The verbatim markup-to-text contract and the four fingerprints |
| `docs/OBSERVATION_SEMANTICS.md` | The time and uncertainty model; what absence requires |
| `docs/CONFIGURATION.md` | Every config key, default and meaning, plus an annotated example |
| `docs/OPERATIONS.md` | Day-to-day running, troubleshooting, health JSON contract |
| `docs/BACKUP_RESTORE.md` | Backup design, rotation, restore and verification |
| `docs/SECURITY.md` | Source-access policy and security posture |
| `docs/ACCEPTANCE.md` | Acceptance report skeleton (measurements pending the first harvest) |

## Licence

MIT. See `LICENSE`.

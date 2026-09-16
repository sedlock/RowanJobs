# CLAUDE.md — working rules for RowanJobs

Instructions for any agent or human modifying this repository. RowanJobs is an
**evidence archive**. Its value is that a reader years from now can trust what it
says about the past. Almost every rule below exists to protect that.

Read `docs/OBSERVATION_SEMANTICS.md` before touching anything that records or
interprets time, presence or absence.

---

## NEVER

These are not style preferences. Each one, if broken, silently produces an
archive that lies about history.

1. **Never overwrite or delete archived history.**
   `artifacts`, `fetches`, `listing_entries`, `posting_observations`,
   `extractions` and `posting_versions` are **append-only in ordinary
   ingestion**. Do not add `UPDATE` or `DELETE` statements against them in the
   collection path. If an interpretation was wrong, add a new row under a new
   version — the old row stays. (`src/rowanjobs/db/migrations/m0001_initial.py`
   module docstring; `src/rowanjobs/collect/repo.py`.)

2. **Never treat a failed, partial, challenged or unqualified collection as
   evidence that an advertisement disappeared.**
   `access_control_challenge` and `retrieval_failed` are *collection
   uncertainty* (`UNCERTAIN_AVAILABILITY` in `src/rowanjobs/constants.py`). A
   scan that did not pass every check in `src/rowanjobs/collect/qualify.py`
   cannot support any absence claim; it still stands as positive evidence of
   what it *did* see. When a run has no qualified scan, absence analysis is
   suppressed for the whole run and a `coverage_gap` is recorded
   (`src/rowanjobs/collect/events.py::derive_for_run`).

3. **Never use AI/LLMs to summarise, rewrite, correct, translate or otherwise
   transform archived job descriptions.**
   The production collector makes **zero model calls**. An archived description
   is what the employer published, character for character. A model-generated
   paraphrase stored next to it — or worse, in place of it — destroys the only
   thing this archive is for. Do not add such a dependency, not behind a flag,
   not "just for search". Analysis performed *outside* the archive, on exported
   copies, is a separate matter; nothing generated may be written back into
   `posting_versions`, `version_values` or `extractions`.

4. **Never merge postings by title or textual similarity.**
   A posting is identified by `(source_namespace, external_job_id)` and nothing
   else (`UNIQUE(source_namespace, external_job_id)` on `postings`). Two
   advertisements with identical titles are two advertisements. A re-advertised
   role with a new source id is a new posting. Do not add fuzzy matching,
   title normalisation or dedup-by-description.

5. **Never treat a parser or contract change as a source content change.**
   Extractions are keyed by
   `(artifact, parser_name, parser_version, contract_version, text_contract_version)`;
   a parser upgrade produces new rows and never rewrites old ones. Content
   versions are only ever compared **within one `contract_version`**
   (`src/rowanjobs/collect/events.py::_record_content_events`,
   `src/rowanjobs/extract/fingerprint.py`). If you change parsing behaviour,
   bump the appropriate version (below) — do not silently improve a parser in
   place.

6. **Never report previously stored content as freshly retrieved.**
   `v_posting_current.content_freshness` is the contract: `checked` means the
   most recent observation is the one that produced this content;
   `carried-forward` means the content is older than the last check;
   `carried-forward-uncertain` means the last check was a challenge or a
   retrieval failure; `never-captured` means no content was ever captured
   (`src/rowanjobs/db/migrations/m0002_views.py`). Any report, export or UI must
   carry that distinction through. Exports already do
   (`src/rowanjobs/export.py`).

7. **Never fabricate historical coverage.** A missed day stays a gap. A later
   run observes *today*; it cannot backfill yesterday, and no amount of
   interpolation makes it so. `Persistent=true` on `rowanjobs.timer` triggers a
   **current** collection after a missed activation — it does not reconstruct
   the missed snapshot. Do not write code that fills gaps by carrying an
   observation backwards or forwards in time.

Two corollaries worth stating explicitly:

- **Never hold a write transaction across network I/O.** `Database.write()` is a
  short `BEGIN IMMEDIATE` block and nesting it raises
  (`src/rowanjobs/db/connection.py`). Fetch first, then write.
- **Never scrape the live source from CI.** `.github/workflows/ci.yml` is
  fixture-only and has no schedule trigger, on purpose. Production collection
  belongs to the scheduled timer on the host, under one request budget.

---

## Repository layout

```
src/rowanjobs/
    __init__.py         version constants: PARSER_VERSION, CONTRACT_VERSION,
                        TEXT_CONTRACT_VERSION, QUALIFICATION_RULES_VERSION,
                        EVENT_RULES_VERSION, HEALTH_SCHEMA_VERSION, SOURCE_NAMESPACE
    cli.py              argparse CLI and exit codes
    config.py           TOML configuration, dataclass defaults, config hashing
    constants.py        every enumeration shared by the schema and the code
    paths.py            filesystem layout and env-var overrides
    timeutil.py         UTC storage, America/New_York display, slot arithmetic
    export.py           datasets, provenance, CSV formula neutralisation
    reprocess.py        offline re-parsing of archived payloads (no network)
    archive/store.py    content-addressed compressed payload storage
    db/                 connection policy, SQLite runtime check, migrations
    net/                client, request budget, SSRF guard, browser challenge step
    extract/            decode, verbatim text contract, byte-exact slicing,
                        fingerprints, PageUp listing/detail adapters, date handling
    collect/            runner (the workflow), scanner, details, qualify, events,
                        repo (persistence), lock
    ops/                backup, health, doctor, schedule, notify, atomic writes
tests/fixtures/pageup/  trimmed real captures used by the test suite
ops/systemd/            unit templates (__VENV__/__CONFIG__/__DATA_ROOT__)
.github/workflows/ci.yml  fixture-only CI
docs/                   the documents listed in README.md
```

## Running the tests

```sh
uv sync --frozen --all-extras
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest -q
```

Tests must run entirely from `tests/fixtures/pageup/` and must never open a
socket to the live source. `pytest.ini_options` sets `testpaths = ["tests"]`,
`--strict-markers`, and turns RowanJobs `DeprecationWarning`s into errors.

## Running the collector safely

```sh
# Dry-ish first look: bounded detail retrievals, no second traversal, no backup.
uv run rowanjobs --data-root /tmp/rowanjobs-scratch collect \
    --kind manual --max-details 3 --no-verification --no-backup
```

- Always point experiments at a scratch `--data-root` or `--db`. The production
  archive is at `~/.local/share/rowanjobs/rowanjobs.db`.
- The collector takes an exclusive `flock` (`src/rowanjobs/collect/lock.py`).
  Two collectors cannot run at once; the loser records `lock_contention` and
  attempts nothing.
- All live traffic goes through one `RequestBudget`
  (`src/rowanjobs/net/budget.py`): one request at a time, a minimum interval,
  jitter, a per-run ceiling, and progressive slowdown after a challenge. Do not
  add an HTTP call that bypasses `SourceClient`.
- Reporting commands (`status`, `runs`, `show`, `history`, `diff`, `verify`,
  `export`, `restore`) open the database **read-only** and cannot migrate or
  mutate it (`src/rowanjobs/db/connection.py::open_readonly`).
- `reprocess` touches no network at all; it re-parses archived payloads and
  keeps the original observation timestamps.

## The versioned contracts

All five live in `src/rowanjobs/__init__.py`. They are written into the rows they
govern, so old data keeps its old meaning and a change never rewrites history.

| Constant | Governs | Bump when | Effect of bumping |
|---|---|---|---|
| `PARSER_VERSION` | What an adapter extracts from a page (`extract/pageup_listing.py`, `extract/pageup_detail.py`) | Any change to what is recognised, captured, labelled or classified — new field, new closure phrase, changed link classification | New `extractions` rows appear for the same artifact; old extraction rows and their interpretation stay |
| `CONTRACT_VERSION` | What counts as "different content" — the comparison contract (`extract/fingerprint.py`) | The definition of a content change moves: fields entering or leaving the fingerprint, changed canonicalisation of metadata | A parallel line of `posting_versions` begins; versions across contract versions are never compared, so nothing looks like a source edit |
| `TEXT_CONTRACT_VERSION` | Markup → plain text rules (`extract/text.py`, `docs/EXTRACTION_CONTRACT.md`) | Any change to dropped tags, block boundaries, `<br>`, list, table, entity or whitespace handling | New extractions and new text fingerprints; the previous rendering of every archived page remains reproducible |
| `QUALIFICATION_RULES_VERSION` | Whether a listing scan may support an absence claim (`collect/qualify.py`) | Adding, removing or altering a qualification check | A new `listing_scan_assessments` row per scan under the new rules; the old assessment stays, so you can see both verdicts |
| `EVENT_RULES_VERSION` | Derived presence and change events (`collect/events.py`) | Changing when `first_observed`, `listed`, `absent_qualified`, `reappeared`, `content_changed` or `coverage_gap` is emitted | `presence_events` gains a parallel rules-version line; the table is rebuildable from evidence, so old events are never edited |

Also versioned, but for different consumers: `HEALTH_SCHEMA_VERSION`
(`ops/health.py`, the `status --json` / `health.json` contract),
`EXPORT_SCHEMA_VERSION` (`export.py`), and `SCHEMA_VERSION`
(`db/migrations/__init__.py`, derived from the migration list).

Bump the constant **in the same change** that alters the behaviour, and say so
in the commit message. If you are not sure whether a change is behavioural,
assume it is and bump.

## Schema changes

Add a new numbered module under `src/rowanjobs/db/migrations/` and append it to
`MIGRATIONS`. Never edit an applied migration. Every enumeration used in a
`CHECK` constraint must also exist in `src/rowanjobs/constants.py`, so the SQL
and the Python call sites cannot drift apart.

## Style

- Plain, direct prose in docstrings; explain *why*, not *what*.
- `ruff` with the configured rule set, line length 100; `mypy` over `src`.
- Do not add a dependency without a recorded reason. The current dependency set
  is deliberately small: `apsw` (patched SQLite runtime), `httpx`, `lxml`, and
  the optional `playwright`.

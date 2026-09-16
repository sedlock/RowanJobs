# Observation semantics: the time and uncertainty model

This is the document to read before writing any query, report or UI over the
archive. Everything here exists because the three statements

> "the advertisement was removed"
> "we could not check that day"
> "our parser changed"

look identical in a naive scrape, and conflating them silently corrupts every
longitudinal claim built on the data.

---

## 1. The kinds of time

Six distinct kinds of time are stored. They are never merged, and a report that
treats any two of them as interchangeable is wrong.

| Kind | Where | Meaning |
|---|---|---|
| **Source-advertised dates** | `version_values.value_text`, `source_machine_value`, `source_precision`, `source_tz_text`, `parsed_utc`, `parsed_local_date` | What the *employer published*: "Advertised: Sep 15 2026", "Applications close: Sep 29 2026 11:55 PM Eastern Daylight Time". These are claims by the source, not observations by us. |
| **First / last successful observation** | `postings.first_discovered_at_utc`; `v_posting_current.last_captured_at_utc`, `last_listed_at_utc`, `last_seen_any_scan_at_utc` | When *we* first and last actually saw the thing. Bounded by our own schedule, never by the source's calendar. |
| **Qualified observations of absence** | `presence_events` rows with `event_kind='absent_qualified'`, `interval_start_utc` / `interval_end_utc` / `slot_local_date` | When a scan that *passed every completeness check* did not contain the advertisement. |
| **Source retrieval timestamps** | `fetches.started_at_utc` / `ended_at_utc`; `posting_observations.observed_at_utc`; `resource_observations.observed_at_utc` | When bytes were requested from the source. `posting_observations.observed_at_utc` is the **start of the retrieval**. A linked document has its **own** retrieval time, independent of its parent page. |
| **Extraction / reprocessing timestamps** | `extractions.extracted_at_utc` | When a parser ran over archived bytes. This is *not* an observation of the source. Offline reprocessing produces new extraction times while leaving `observed_at_utc` untouched (`src/rowanjobs/reprocess.py`). |
| **Scheduled slots vs. actual attempts** | `collection_runs.scheduled_slot_utc`, `scheduled_slot_local_date` vs. `started_at_utc`, `ended_at_utc`, `attempt_no`, `parent_run_id` | The *slot* is the intended daily moment. The *attempt* is what actually happened, possibly hours later, possibly more than once. |

Storage format: every timestamp is UTC, ISO-8601, `Z`-suffixed, second precision
(`src/rowanjobs/timeutil.py::utc_str`; naive datetimes are rejected). Display is
`America/New_York` with an explicit zone label. The `*_local_date` columns are
`America/New_York` calendar dates and are the key for daily reasoning.

### Source precision is never over-committed

`src/rowanjobs/extract/dates.py` records, for each date field: the display string
exactly as shown, the machine value the page supplied, the timezone wording
printed next to it, and a clearly-derived interpretation.

`source_precision` records what the *display* committed to. On this source the
`Advertised` line shows a bare date while its `<time datetime>` carries a 12:00Z
placeholder; precision is therefore `date`, `parsed_utc` is left **null**, and a
note records that the machine value is the source's own placeholder and must not
be reported as a published time. A date-only value stays date-only. Nothing here
invents a midnight.

---

## 2. Scan qualification: what absence requires

A listing scan may support an **absence** claim only if it passes every check in
`src/rowanjobs/collect/qualify.py::assess`. Anything less is still preserved as
positive evidence — "we saw these advertisements at this time" — but can never be
used to conclude that an advertisement disappeared.

The verdict is written to `listing_scan_assessments` with the
`QUALIFICATION_RULES_VERSION` that produced it and the full per-check detail, so
a later reader can see exactly *why* a scan did or did not qualify, and a rules
change produces a **second verdict beside the first** rather than silently
reinterpreting an old scan.

| Check | Passes when | Why it matters |
|---|---|---|
| `scope_unfiltered` | The traversal started at the configured unfiltered listing URL | A filtered listing does not contain everything, so nothing can be concluded from an advertisement's absence from it |
| `structure_recognized` | Every retrieved page matched the PageUp listing structure | An unrecognised page might be full of advertisements we failed to parse |
| `all_pages_retrieved` | `pages_failed == 0` | A page we never got might have held the advertisement |
| `legitimate_termination` | `termination_reason` is `no_more_link` or `empty_validated_page` | Pagination ended because *the source said so*, not because a bound or a failure stopped us |
| `no_unresolved_identity` | `unresolved_candidates == 0` | A row we could not resolve to an identifier might *be* the advertisement |
| `no_access_control_response` | No page was answered with a challenge, block or rate-limit response | A challenged page is an unknown, not an empty one |
| `no_unexpected_redirect` | No listing page redirected somewhere unexpected | We may not have been reading the listing at all |
| `all_pages_http_200` | Every listing page answered 200 | Same reason |
| `within_page_bound` | `pages_requested < max_listing_pages` | Hitting the safety ceiling means the traversal was cut short by *us* |
| `no_pagination_loop` | No page repeated a previously seen page signature | A loop means we did not actually walk the whole result set |
| `source_count_reconciles` | `source_reported_total == unique_ids` | The source's own count agrees with what we collected. Reported as *not applicable* (`passed: null`) when the source gave no count. |
| `empty_result_validated` | Only evaluated when `unique_ids == 0`: the intact empty-result template was present | "There are no jobs" must be demonstrated by the source's own empty-result page, not merely by a parser producing nothing |

A check may be `true`, `false`, or `null` meaning *not applicable / no evidence*.
**The scan qualifies only if no check is `false`;** a `null` does not disqualify.
When it fails, `reason` lists the failing check names and the scan can still be
read as positive evidence of what it did see.

### Run-level consequences

- If no traversal in a run qualifies, `EventDeriver.derive_for_run` records a
  `no_qualified_discovery` coverage gap and **suppresses absence analysis for the
  entire run**. Content events are still derived from whatever was captured.
- If the discovery and verification traversals disagree on the identifier *set*
  and a bounded reconciliation traversal does not settle it, the run records a
  `listing_set_unreconciled` coverage gap and again suppresses absence analysis
  (`src/rowanjobs/collect/runner.py::_collect`).
- Two matching traversals are **consistency evidence, not proof the source held
  still**. The run records that caveat verbatim in `coverage_json`.
- The authoritative listing set for a run is the **last qualified** traversal
  (`_final_qualified`).
- Sets are compared, not counts. Equal counts with different members is a real
  discrepancy that a count comparison would miss.

---

## 3. Availability states, and which are uncertainty

`posting_observations.availability_state`, from
`src/rowanjobs/constants.py::AVAILABILITY_STATES`.

| State | What it asserts |
|---|---|
| `content_captured` | A real advertisement body was retrieved. Substantive. |
| `explicit_closure` | The source displayed a closed/unavailable template. Terminal. |
| `not_found` | The source returned a definite not-found. Terminal. |
| `redirected_to_listing` | The detail URL bounced to the general listing. Terminal. |
| `redirected_to_other_job` | The detail URL bounced to a **different** advertisement. Conflict. |
| `identity_mismatch` | The body parsed but the displayed job id disagreed. Conflict. |
| `access_control_challenge` | **UNCERTAINTY.** A WAF/bot challenge. We do not know the state of the advertisement. |
| `retrieval_failed` | **UNCERTAINTY.** Network or HTTP failure. We do not know the state of the advertisement. |

`UNCERTAIN_AVAILABILITY = ("access_control_challenge", "retrieval_failed")`.
**Neither is ever evidence of absence.** They mean *we failed*, not *it is gone*.

`TERMINAL_AVAILABILITY = ("explicit_closure", "not_found",
"redirected_to_listing")` — these are the only states that advance the streak
towards weekly rechecking. Uncertainty never advances it
(`runner.py::_update_recheck_policy`), so a run of failed checks cannot quietly
demote a live advertisement out of daily observation.

The two **conflict** states are neither absence nor capture: the destination's
content is preserved but is **not assigned to the expected posting**, and the
disagreement is written to `conflicts_json` unresolved. Reconciling it into one
"correct" value would invent a fact the source never published.

### Content freshness

`v_posting_current.content_freshness` carries the same distinction into the
projection layer:

| Value | Meaning |
|---|---|
| `checked` | The most recent observation is the one that produced this content |
| `carried-forward` | The content predates the most recent check, which produced no content |
| `carried-forward-uncertain` | The most recent check was a challenge or a retrieval failure |
| `never-captured` | No content has ever been captured |

**Previously stored content must never be reported as freshly retrieved.** Every
export carries `content_freshness`, and `rowanjobs show` prints it.

---

## 4. The two-distinct-qualifying-daily-observations reporting rule

`src/rowanjobs/collect/events.py::repeatedly_unlisted`.

A single `absent_qualified` event is one qualified scan's observation. For
*reporting* that an advertisement is no longer advertised, the rule is:

> **Two distinct qualifying daily observations of absence are required.**

Implemented by counting **distinct `slot_local_date` values** among the posting's
`absent_qualified` events, not by counting events:

```
distinct_absent_slot_dates : the ordered distinct slot dates
count                      : how many
meets_two_day_rule         : count >= 2
intervening_uncovered_dates: calendar dates in that span with no qualified scan
```

### Why same-day retries cannot create a streak

A retry belongs to its parent's scheduled slot. `timeutil.slot_for` computes the
slot as the configured daily time **on the run's local calendar date**, so a
09:15 retry of the 06:15 slot still carries `scheduled_slot_local_date` for that
day. Presence events key on that slot date. Three retries on one afternoon
therefore produce at most **one** distinct slot date and cannot manufacture a
second daily confirmation. `cmd_retry` in `src/rowanjobs/cli.py` also sets the
retry's `parent_run_id` to the day's daily run and bounds the attempts by
`schedule.max_retries_per_slot`.

### Uncovered days are gaps, not consecutive absences

`repeatedly_unlisted` also walks the calendar between the first and last absence
date and lists every date in that span for which **no qualified scan exists at
all** (`v_qualified_scans` has no row for that `scheduled_slot_local_date`).
Those dates are reported as `intervening_uncovered_dates` — gaps. They are not
quietly treated as further consecutive absences, and they are not filled in.

`rowanjobs history JOB_ID` prints both the count and the uncovered dates.

---

## 5. A → B → A is three observations across two versions

Identical content re-observed later reuses the existing `posting_versions` row —
the table is unique on `(posting_id, contract_version, content_fingerprint)` and
`Repository.ensure_posting_version` returns the existing id when the fingerprint
matches.

So a posting that reads A on Monday, B on Tuesday and A again on Wednesday has:

- **three** `posting_observations` rows, one per day;
- **two** `posting_versions` rows, A and B;
- **two** `content_changed` presence events: A→B, then B→A.

`version_count` (2) and `observation_count` (3) in `v_posting_current` are
therefore different numbers measuring different things, and both are correct. A
reversion is a real event and is recorded as one; the archive does not "restore"
the earlier version or collapse the history.

## 6. Change intervals are intervals, not instants

`presence_events.interval_start_utc` and `interval_end_utc` bound the event; the
schema comment states it plainly: *the event happened somewhere inside this
interval. We never manufacture an exact event time.*

For a `content_changed` event the interval runs from the previous supporting
observation to the current one — typically about 24 hours with a daily schedule,
and longer across a coverage gap. The evidence JSON says so: *the change happened
somewhere inside this interval; the exact edit time is not observable.*

The same applies to `absent_qualified` (from the last `listed` event to the end
of the qualifying scan) and `reappeared` (from the last `absent_qualified` to the
sighting). `v_content_changes` exposes both bounds. **A report must not render
an interval as a point**; if it needs a single date, it must say which end of the
interval it used and that the true time is unknown within it.

`first_observed` and `listed` are the exception in form only: their interval
start and end are the same instant, because the sighting itself *is* the
observation. That is still "we saw it then", not "it appeared then".

## 7. A passed deadline is not closure

`Applications close: Sep 29 2026 11:55 PM Eastern Daylight Time` is a claim the
**source published**. It is not an observation, and the clock passing it is not
an event.

The archive records closure only when it **observes** something: an
`explicit_closure` template, a `not_found`, a `redirected_to_listing`, or a
qualified scan that no longer lists the advertisement. Advertisements routinely
outlive their stated deadline, get extended, or vanish before it. Computing
"closed" from `parsed_utc < now()` would be inventing an observation, and no code
path in RowanJobs does so.

Note also that `parsed_utc` is **null for date-only fields**, so such a
computation would silently skip exactly the values whose precision is weakest.

## 8. A same-id reappearance keeps the original identity and history

A posting is identified by `(source_namespace, external_job_id)` and nothing
else. If an advertisement disappears from the listing and later returns under the
**same source id**, it is the same `postings` row:

- the original `posting_id`, `first_discovered_at_utc` and `discovery_basis` are
  unchanged;
- the entire observation history, every content version and every earlier
  `absent_qualified` event remain attached;
- a `reappeared` presence event is emitted, whose evidence records *"same source
  identifier; the original posting identity and its full observation history are
  retained"* (`events.py`);
- `recheck_policy` resets to the daily tier immediately with
  `consecutive_terminal_observations = 0`.

A re-advertisement under a **new** source id is a **different posting**. It is
not merged with the old one, not by title and not by description similarity.
Whether the two are "the same job" is an interpretation for an analyst to make
downstream, with the evidence in front of them — not a guess the archive bakes
in.

## 9. Baseline runs

The first run that produces a qualified scan is a **baseline**
(`collection_runs.is_baseline`, `postings.discovery_basis='baseline'`).
Advertisements present at the baseline were **not** newly posted; we simply had
not been watching. Only advertisements first seen *after* a qualified baseline
exists are `observed-new`, and only those get a `first_observed` presence event
with the basis *"present in the first qualified scan that saw it"*.

Reports about "new postings per week" must filter on `discovery_basis =
'observed-new'`, or the baseline day will appear as an enormous hiring spike that
never happened.

## 10. Comparability groups

`source_configs.comparability_group` (default `v1-unfiltered-en-us`) is stamped
onto every presence event. Absence history is only comparable **within one
group**. If the collected scope ever changes — a different locale, a filtered
listing, a different definition of what is in scope — the group must change too,
and absence series must not be joined across the boundary.

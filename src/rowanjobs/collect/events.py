"""Derived presence and content-change events.

Everything here is a *projection*: each row records the rules version and the
evidence ids that produced it, and the whole table can be dropped and rebuilt
from the observations without contacting the website.

The rules, deliberately narrow:

* An absence event needs a **qualified** scan. A partial or failed scan produces
  a coverage gap, never a removal.
* Absence is recorded per *scheduled slot local date*. Same-day retries share
  their parent's slot, so two retries on one afternoon can never add up to two
  daily confirmations.
* A change event stores the interval between the two supporting observations.
  We never claim to know the instant an edit happened.
* Two versions are only compared when their contract versions match, which is
  what prevents a parser upgrade from looking like a website edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import CONTRACT_VERSION, EVENT_RULES_VERSION
from ..timeutil import utc_str

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from .repo import Repository
    from .scanner import ScanResult


@dataclass
class EventSummary:
    first_observed: int = 0
    listed: int = 0
    absent_qualified: int = 0
    reappeared: int = 0
    content_changed: int = 0
    coverage_gaps: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "first_observed": self.first_observed,
            "listed": self.listed,
            "absent_qualified": self.absent_qualified,
            "reappeared": self.reappeared,
            "content_changed": self.content_changed,
            "coverage_gaps": self.coverage_gaps,
        }


class EventDeriver:
    def __init__(self, *, cfg: Config, repo: Repository) -> None:
        self.cfg = cfg
        self.repo = repo

    def derive_for_run(
        self,
        *,
        run_id: int,
        slot_local_date: str | None,
        qualified_scan: ScanResult | None,
        listed_ids: set[str],
    ) -> EventSummary:
        summary = EventSummary()
        group = self.cfg.collection.comparability_group

        if qualified_scan is None or not qualified_scan.qualified:
            # No qualified scan: positive observations stay, absence analysis is
            # suppressed entirely for this run.
            self.repo.record_gap(
                kind="no_qualified_discovery",
                scope="listing",
                run_id=run_id,
                slot_local_date=slot_local_date,
                detail=(
                    "no listing scan in this run qualified, so no absence "
                    "conclusion may be drawn from it"
                ),
                evidence={
                    "reason": qualified_scan.assessment.reason
                    if qualified_scan and qualified_scan.assessment
                    else "no scan completed"
                },
            )
            summary.coverage_gaps += 1
            self._record_content_events(run_id, summary, group, slot_local_date)
            return summary

        scan_id = qualified_scan.scan_id
        scan_end = qualified_scan.ended_at_utc

        for posting in self.repo.db.query(
            "SELECT posting_id, external_job_id, first_discovered_at_utc, "
            "first_discovered_run_id FROM postings"
        ):
            posting_id = int(posting["posting_id"])
            job_id = str(posting["external_job_id"])
            listed = job_id in listed_ids
            seen_at = qualified_scan.entry_times.get(job_id, scan_end)
            previous = self._previous_presence(posting_id)

            if listed:
                if int(posting["first_discovered_run_id"] or 0) == run_id:
                    self._add(
                        posting_id,
                        "first_observed",
                        run_id,
                        scan_id,
                        None,
                        seen_at,
                        seen_at,
                        slot_local_date,
                        group,
                        {
                            "scan_id": scan_id,
                            "basis": "present in the first qualified scan that saw it",
                        },
                    )
                    summary.first_observed += 1
                elif previous == "absent_qualified":
                    self._add(
                        posting_id,
                        "reappeared",
                        run_id,
                        scan_id,
                        None,
                        self._last_event_time(posting_id, "absent_qualified"),
                        seen_at,
                        slot_local_date,
                        group,
                        {
                            "scan_id": scan_id,
                            "note": "same source identifier; the original posting "
                            "identity and its full observation history are retained",
                        },
                    )
                    summary.reappeared += 1
                self._add(
                    posting_id,
                    "listed",
                    run_id,
                    scan_id,
                    None,
                    seen_at,
                    seen_at,
                    slot_local_date,
                    group,
                    {"scan_id": scan_id},
                )
                summary.listed += 1
            else:
                self._add(
                    posting_id,
                    "absent_qualified",
                    run_id,
                    scan_id,
                    None,
                    self._last_event_time(posting_id, "listed"),
                    scan_end,
                    slot_local_date,
                    group,
                    {
                        "scan_id": scan_id,
                        "qualification_rules_version": (
                            qualified_scan.assessment.rules_version
                            if qualified_scan.assessment
                            else None
                        ),
                        "note": "absence observed in one qualified scan; a reporting "
                        "rule needs two distinct qualifying daily observations",
                    },
                )
                summary.absent_qualified += 1

        self._record_content_events(run_id, summary, group, slot_local_date)
        return summary

    # ---------------------------------------------------------------- content

    def _record_content_events(
        self,
        run_id: int,
        summary: EventSummary,
        group: str,
        slot_local_date: str | None,
    ) -> None:
        rows = self.repo.db.query(
            """
            SELECT o.posting_id, o.observation_id, o.posting_version_id, o.observed_at_utc
              FROM posting_observations o
             WHERE o.run_id = ? AND o.posting_version_id IS NOT NULL
               AND o.availability_state = 'content_captured'
               AND o.identity_state = 'match'
             ORDER BY o.observed_at_utc
            """,
            (run_id,),
        )
        for row in rows:
            posting_id = int(row["posting_id"])
            version_id = int(row["posting_version_id"])
            previous = self.repo.db.one(
                """
                SELECT o.posting_version_id, o.observed_at_utc
                  FROM posting_observations o
                  JOIN posting_versions v
                    ON v.posting_version_id = o.posting_version_id
                 WHERE o.posting_id = ?
                   AND o.observation_id != ?
                   AND o.observed_at_utc <= ?
                   AND o.posting_version_id IS NOT NULL
                   AND o.availability_state = 'content_captured'
                   AND v.contract_version = ?
                 ORDER BY o.observed_at_utc DESC, o.observation_id DESC
                 LIMIT 1
                """,
                (
                    posting_id,
                    int(row["observation_id"]),
                    str(row["observed_at_utc"]),
                    CONTRACT_VERSION,
                ),
            )
            if previous is None:
                continue
            prior_version = int(previous["posting_version_id"])
            if prior_version == version_id:
                continue
            self._add(
                posting_id,
                "content_changed",
                run_id,
                None,
                int(row["observation_id"]),
                str(previous["observed_at_utc"]),
                str(row["observed_at_utc"]),
                slot_local_date,
                group,
                {
                    "contract_version": CONTRACT_VERSION,
                    "note": "the change happened somewhere inside this interval; the "
                    "exact edit time is not observable",
                },
                from_version=prior_version,
                to_version=version_id,
            )
            summary.content_changed += 1

    # ---------------------------------------------------------------- helpers

    def _previous_presence(self, posting_id: int) -> str | None:
        row = self.repo.db.one(
            "SELECT event_kind FROM presence_events WHERE posting_id = ? "
            "AND event_kind IN ('listed','absent_qualified') AND rules_version = ? "
            "ORDER BY interval_end_utc DESC, presence_event_id DESC LIMIT 1",
            (posting_id, EVENT_RULES_VERSION),
        )
        return str(row["event_kind"]) if row else None

    def _last_event_time(self, posting_id: int, kind: str) -> str | None:
        row = self.repo.db.one(
            "SELECT interval_end_utc FROM presence_events WHERE posting_id = ? "
            "AND event_kind = ? AND rules_version = ? "
            "ORDER BY interval_end_utc DESC LIMIT 1",
            (posting_id, kind, EVENT_RULES_VERSION),
        )
        return str(row["interval_end_utc"]) if row else None

    def _add(
        self,
        posting_id: int,
        kind: str,
        run_id: int,
        scan_id: int | None,
        observation_id: int | None,
        interval_start: str | None,
        interval_end: str,
        slot_local_date: str | None,
        group: str,
        evidence: dict[str, Any],
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> None:
        import json

        with self.repo.db.write():
            self.repo.db.execute(
                """
                INSERT OR IGNORE INTO presence_events(
                    posting_id, event_kind, rules_version, run_id, scan_id,
                    observation_id, from_posting_version_id, to_posting_version_id,
                    interval_start_utc, interval_end_utc, slot_local_date,
                    comparability_group, evidence_json, created_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    posting_id,
                    kind,
                    EVENT_RULES_VERSION,
                    run_id,
                    scan_id,
                    observation_id,
                    from_version,
                    to_version,
                    interval_start,
                    interval_end,
                    slot_local_date,
                    group,
                    json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                    utc_str(),
                ),
            )


def repeatedly_unlisted(
    repo: Repository, posting_id: int, rules_version: str = EVENT_RULES_VERSION
) -> dict[str, Any]:
    """Absence evidence for one posting, with the daily-confirmation rule applied.

    Distinct *slot local dates* are counted, not events: same-day retries share
    their parent's slot and therefore cannot inflate a streak. Calendar days with
    no qualified scan at all are reported as gaps rather than quietly treated as
    consecutive.
    """
    rows = repo.db.query(
        "SELECT slot_local_date, interval_start_utc, interval_end_utc "
        "FROM presence_events WHERE posting_id = ? AND event_kind = 'absent_qualified' "
        "AND rules_version = ? ORDER BY interval_end_utc",
        (posting_id, rules_version),
    )
    dates: list[str] = []
    for row in rows:
        date = row["slot_local_date"]
        if date and date not in dates:
            dates.append(str(date))
    covered = {
        str(r["slot_local_date"])
        for r in repo.db.query(
            "SELECT DISTINCT scheduled_slot_local_date AS slot_local_date "
            "FROM v_qualified_scans WHERE scheduled_slot_local_date IS NOT NULL"
        )
    }
    gaps: list[str] = []
    if dates:
        from datetime import date as _date
        from datetime import timedelta

        start = _date.fromisoformat(dates[0])
        end = _date.fromisoformat(dates[-1])
        day = start
        while day <= end:
            iso = day.isoformat()
            if iso not in covered:
                gaps.append(iso)
            day += timedelta(days=1)
    return {
        "distinct_absent_slot_dates": dates,
        "count": len(dates),
        "meets_two_day_rule": len(dates) >= 2,
        "intervening_uncovered_dates": gaps,
        "first_absent_at_utc": str(rows[0]["interval_end_utc"]) if rows else None,
        "latest_absent_at_utc": str(rows[-1]["interval_end_utc"]) if rows else None,
        "rules_version": rules_version,
    }

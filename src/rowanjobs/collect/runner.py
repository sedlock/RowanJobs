"""The daily collection workflow.

    0. obtain an access token the way an ordinary visitor's browser does
    1. complete discovery traversal, queueing detail retrievals
    2. newly discovered advertisements first
    3. detail pages and their job-specific resources
    4. second complete listing traversal
    5. compare identifier *sets*, not counts
    6. one bounded reconciliation traversal if they disagree

Two matching traversals are consistency evidence, not proof the site held
still. When reconciliation cannot settle a discrepancy the run keeps every
positive observation and suppresses absence-dependent conclusions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .. import QUALIFICATION_RULES_VERSION, __version__
from ..config import Config
from ..db import Database, open_db
from ..net.browser import ChallengeSolver
from ..net.budget import ChallengeWall, RequestBudget
from ..net.client import SourceClient
from ..net.guard import UrlPolicy
from ..timeutil import now_utc, slot_for, utc_str
from .details import DetailCollector
from .events import EventDeriver, EventSummary
from .lock import CollectorLock, LockContention
from .repo import Repository
from .scanner import ListingScanner, ScanResult

DETAIL_PRIORITY_NEW = 10
DETAIL_PRIORITY_LISTED = 50
DETAIL_PRIORITY_HISTORICAL_DAILY = 80
DETAIL_PRIORITY_HISTORICAL_WEEKLY = 90


@dataclass
class CollectionResult:
    run_id: int | None
    run_uuid: str | None
    outcome: str
    detail: str | None = None
    counts: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    events: EventSummary | None = None
    started_at_utc: str = ""
    ended_at_utc: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "success"


class Collector:
    def __init__(
        self,
        cfg: Config,
        *,
        db: Database | None = None,
        client: SourceClient | None = None,
        app_revision: str | None = None,
    ) -> None:
        self.cfg = cfg
        self._db = db
        self._owns_db = db is None
        self._client = client
        self._owns_client = client is None
        self.app_revision = app_revision

    # ------------------------------------------------------------------ setup

    def _make_client(self) -> SourceClient:
        net = self.cfg.network
        budget = RequestBudget(
            min_interval=net.min_interval_seconds,
            max_requests=net.max_requests_per_run,
            challenge_backoff=net.challenge_backoff_seconds,
            max_consecutive_challenges=net.max_consecutive_challenges,
            jitter=net.jitter_seconds,
        )
        policy = UrlPolicy(
            allowed_hosts=tuple(net.allowed_hosts), allow_subdomains=net.allow_subdomains
        )
        solver = None
        if self.cfg.browser.enabled:
            solver = ChallengeSolver(
                enabled=True,
                headless=self.cfg.browser.headless,
                channel=self.cfg.browser.channel,
                nav_timeout_seconds=self.cfg.browser.nav_timeout_seconds,
                token_ttl_seconds=self.cfg.browser.token_ttl_seconds,
                max_solves=self.cfg.browser.max_solves_per_run,
                # Same honest identity as the HTTP client: the browser step is a
                # transport detail, not a different claim about who we are.
                user_agent=net.user_agent,
            )
            if not solver.available():
                solver = None
        return SourceClient(
            budget,
            policy,
            user_agent=net.user_agent,
            accept_language=net.accept_language,
            connect_timeout=net.connect_timeout_seconds,
            read_timeout=net.read_timeout_seconds,
            write_timeout=net.write_timeout_seconds,
            pool_timeout=net.pool_timeout_seconds,
            http2=net.http2,
            max_redirects=net.max_redirects,
            max_bytes=net.max_response_bytes,
            max_retries=net.max_retries,
            backoff_base=net.backoff_base_seconds,
            backoff_max=net.backoff_max_seconds,
            challenge_solver=solver,
        )

    # -------------------------------------------------------------------- run

    def run(
        self,
        *,
        run_kind: str = "daily",
        parent_run_id: int | None = None,
        attempt_no: int = 1,
        max_details: int | None = None,
        skip_verification: bool = False,
    ) -> CollectionResult:
        layout = self.cfg.layout
        layout.ensure()
        lock = CollectorLock(layout.lock_path)
        started = utc_str()
        try:
            lock.acquire()
        except LockContention as exc:
            return CollectionResult(
                run_id=None,
                run_uuid=None,
                outcome="lock_contention",
                detail=str(exc),
                started_at_utc=started,
                ended_at_utc=utc_str(),
                coverage={"holder": exc.holder},
            )
        try:
            return self._run_locked(
                run_kind=run_kind,
                parent_run_id=parent_run_id,
                attempt_no=attempt_no,
                max_details=max_details,
                skip_verification=skip_verification,
                started=started,
            )
        finally:
            lock.release()

    def _run_locked(
        self,
        *,
        run_kind: str,
        parent_run_id: int | None,
        attempt_no: int,
        max_details: int | None,
        skip_verification: bool,
        started: str,
    ) -> CollectionResult:
        db = self._db or open_db(self.cfg.layout.db_path)
        client = self._client or self._make_client()
        repo = Repository(db)
        errors: list[dict[str, Any]] = []
        run_id: int | None = None
        run_uuid: str | None = None

        try:
            # Nothing else can be running: any open run belongs to a dead process.
            abandoned = repo.abandon_stale_runs()
            if abandoned:
                errors.append(
                    {
                        "kind": "abandoned_runs",
                        "run_ids": abandoned,
                        "detail": "previous runs were never closed; marked aborted. "
                        "Their evidence is preserved.",
                    }
                )

            source_id = repo.ensure_source(self.cfg)
            source_config_id = repo.ensure_source_config(source_id, self.cfg)
            slot_utc, slot_date = slot_for(None, self.cfg.schedule.hour, self.cfg.schedule.minute)
            is_baseline = not repo.has_qualified_baseline()

            run_id, run_uuid = repo.start_run(
                source_id=source_id,
                source_config_id=source_config_id,
                run_kind=run_kind,
                scheduled_slot_utc=slot_utc,
                scheduled_slot_local_date=slot_date,
                parent_run_id=parent_run_id,
                attempt_no=attempt_no,
                app_revision=self.app_revision,
                is_baseline=is_baseline,
            )

            result = self._collect(
                repo=repo,
                client=client,
                run_id=run_id,
                run_uuid=run_uuid,
                source_id=source_id,
                slot_date=slot_date,
                is_baseline=is_baseline,
                max_details=max_details,
                skip_verification=skip_verification,
                errors=errors,
                started=started,
            )
            repo.finish_run(
                run_id,
                outcome=result.outcome,
                outcome_detail=result.detail,
                counts=result.counts,
                errors=result.errors,
                coverage=result.coverage,
            )
            return result
        except BaseException as exc:
            if run_id is not None:
                errors.append({"kind": type(exc).__name__, "detail": str(exc)})
                repo.finish_run(
                    run_id,
                    outcome="failed",
                    outcome_detail=f"{type(exc).__name__}: {exc}",
                    errors=errors,
                )
            if isinstance(exc, Exception):
                return CollectionResult(
                    run_id=run_id,
                    run_uuid=run_uuid,
                    outcome="failed",
                    detail=f"{type(exc).__name__}: {exc}",
                    errors=errors,
                    started_at_utc=started,
                    ended_at_utc=utc_str(),
                )
            raise
        finally:
            if self._owns_client:
                client.close()
            if self._owns_db:
                db.close()

    # --------------------------------------------------------------- workflow

    def _collect(
        self,
        *,
        repo: Repository,
        client: SourceClient,
        run_id: int,
        run_uuid: str,
        source_id: int,
        slot_date: str,
        is_baseline: bool,
        max_details: int | None,
        skip_verification: bool,
        errors: list[dict[str, Any]],
        started: str,
    ) -> CollectionResult:
        cfg = self.cfg
        scanner = ListingScanner(
            cfg=cfg, repo=repo, client=client, run_id=run_id, source_id=source_id
        )
        details = DetailCollector(cfg=cfg, repo=repo, client=client, run_id=run_id)
        deriver = EventDeriver(cfg=cfg, repo=repo)

        # --- 0. access ------------------------------------------------------
        # The source challenges token-less clients after a handful of requests.
        # An ordinary visitor's browser resolves that on its first page load, so
        # we do the same once, up front, rather than spending source requests
        # being refused. Purely optional: without it the run still works, it just
        # backs off when challenged.
        priming = client.prime(self.cfg.listing_url)
        if not priming.get("primed") and priming.get("required", True):
            errors.append(
                {
                    "kind": "access_priming_unavailable",
                    "detail": priming.get("reason"),
                    "note": "collection continues; challenges will be handled by "
                    "backing off, which may reduce coverage",
                }
            )

        # --- 1. discovery ----------------------------------------------------
        discovery = self._scan(scanner, 1, "discovery", errors)
        union_ids: set[str] = set(discovery.unique_ids) if discovery else set()

        # --- 2/3. detail retrieval ------------------------------------------
        queued = self._queue_details(repo, run_id, discovery, is_baseline)
        repo.reclaim_orphans(run_id)
        detail_stats = self._drain_details(repo, details, run_id, max_details, errors)

        # --- 4. verification traversal --------------------------------------
        verification: ScanResult | None = None
        if cfg.collection.verification_scan and not skip_verification:
            verification = self._scan(scanner, 2, "verification", errors)
            if verification:
                union_ids |= set(verification.unique_ids)

        # --- 5/6. reconcile identifier sets ---------------------------------
        reconciliation: ScanResult | None = None
        set_comparison: dict[str, Any] = {"performed": False}
        if discovery and verification:
            added = sorted(set(verification.unique_ids) - set(discovery.unique_ids))
            removed = sorted(set(discovery.unique_ids) - set(verification.unique_ids))
            set_comparison = {
                "performed": True,
                "discovery_unique": len(discovery.unique_ids),
                "verification_unique": len(verification.unique_ids),
                "counts_match": len(discovery.unique_ids) == len(verification.unique_ids),
                "sets_match": not added and not removed,
                "appeared_between_passes": added,
                "disappeared_between_passes": removed,
                "note": "two matching passes are consistency evidence, not proof the "
                "source was static during the run",
            }
            if added:
                new_detail = self._collect_late_arrivals(
                    repo, details, run_id, source_id, added, errors
                )
                detail_stats["late_arrivals"] = new_detail
            if (added or removed) and cfg.collection.reconciliation_scan:
                reconciliation = self._scan(scanner, 3, "reconciliation", errors)
                if reconciliation:
                    union_ids |= set(reconciliation.unique_ids)
                    set_comparison["reconciliation_unique"] = len(reconciliation.unique_ids)
                    set_comparison["reconciliation_qualified"] = reconciliation.qualified

        # --- qualified scan selection ---------------------------------------
        final_scan = self._final_qualified(discovery, verification, reconciliation)
        listed_ids = set(final_scan.unique_ids) if final_scan else set()

        unresolved = bool(
            set_comparison.get("performed")
            and not set_comparison.get("sets_match")
            and (reconciliation is None or not reconciliation.qualified)
        )
        if unresolved:
            repo.record_gap(
                kind="listing_set_unreconciled",
                scope="listing",
                run_id=run_id,
                slot_local_date=slot_date,
                detail=(
                    "the two traversals disagreed and reconciliation did not settle "
                    "it; absence-dependent conclusions are suppressed for this run"
                ),
                evidence=set_comparison,
            )
            errors.append({"kind": "listing_set_unreconciled", "detail": set_comparison})

        # --- events ----------------------------------------------------------
        events = deriver.derive_for_run(
            run_id=run_id,
            slot_local_date=slot_date,
            qualified_scan=None if unresolved else final_scan,
            listed_ids=listed_ids,
        )

        # --- recheck policy ---------------------------------------------------
        self._update_recheck_policy(repo, run_id, listed_ids)

        counts = {
            "final_qualified_listing_count": len(listed_ids) if final_scan else None,
            "union_encountered": len(union_ids),
            "listing_scans": sum(
                1 for s in (discovery, verification, reconciliation) if s is not None
            ),
            "listing_pages": sum(
                len(s.pages) for s in (discovery, verification, reconciliation) if s
            ),
            "detail_queued": queued,
            **detail_stats,
            "events": events.as_dict(),
            "budget": client.budget.stats.as_dict(),
            "queue": repo.queue_summary(run_id),
        }
        coverage = {
            "baseline_run": is_baseline,
            "access_priming": priming,
            "discovery_qualified": bool(discovery and discovery.qualified),
            "verification_qualified": bool(verification and verification.qualified),
            "absence_analysis_supported": bool(final_scan and not unresolved),
            "set_comparison": set_comparison,
            "scan_assessments": [
                {
                    "scan_id": s.scan_id,
                    "role": s.scan_role,
                    "qualified": s.qualified,
                    "reason": s.assessment.reason if s.assessment else None,
                    "termination": s.termination_reason,
                    "pages": len(s.pages),
                    "unique_ids": len(s.unique_ids),
                    "started_at_utc": s.started_at_utc,
                    "ended_at_utc": s.ended_at_utc,
                }
                for s in (discovery, verification, reconciliation)
                if s is not None
            ],
            "rules_version": QUALIFICATION_RULES_VERSION,
            "app_version": __version__,
        }

        outcome, detail = self._outcome(discovery, final_scan, detail_stats, errors, unresolved)
        return CollectionResult(
            run_id=run_id,
            run_uuid=run_uuid,
            outcome=outcome,
            detail=detail,
            counts=counts,
            coverage=coverage,
            errors=errors,
            events=events,
            started_at_utc=started,
            ended_at_utc=utc_str(),
        )

    # ---------------------------------------------------------------- helpers

    def _scan(
        self,
        scanner: ListingScanner,
        ordinal: int,
        role: str,
        errors: list[dict[str, Any]],
    ) -> ScanResult | None:
        try:
            result = scanner.scan(scan_ordinal=ordinal, scan_role=role)
        except ChallengeWall as exc:
            errors.append({"kind": "access_control", "scan_role": role, "detail": str(exc)})
            return None
        if not result.qualified:
            errors.append(
                {
                    "kind": "scan_not_qualified",
                    "scan_role": role,
                    "scan_id": result.scan_id,
                    "detail": result.assessment.reason if result.assessment else None,
                }
            )
        return result

    def _queue_details(
        self,
        repo: Repository,
        run_id: int,
        discovery: ScanResult | None,
        is_baseline: bool,
    ) -> int:
        queued = 0
        listed: set[str] = set()
        if discovery:
            for job_id in discovery.unique_ids:
                listed.add(job_id)
                row = repo.get_posting(job_id)
                created_here = bool(row and int(row["first_discovered_run_id"] or 0) == run_id)
                # Newly discovered advertisements go first.
                priority = (
                    DETAIL_PRIORITY_NEW
                    if (created_here and not is_baseline)
                    else DETAIL_PRIORITY_LISTED
                )
                repo.enqueue(
                    run_id=run_id,
                    kind="posting_detail",
                    work_key=job_id,
                    payload={
                        "url": self._detail_url(repo, job_id),
                        "posting_id": int(row["posting_id"]) if row else None,
                        "checked_because": "listed",
                    },
                    priority=priority,
                    max_attempts=2,
                )
                queued += 1

        queued += self._queue_historical(repo, run_id, listed)
        return queued

    def _queue_historical(self, repo: Repository, run_id: int, listed: set[str]) -> int:
        """Known URLs that are no longer listed but may still serve content."""
        cfg = self.cfg.collection
        now = now_utc()
        rows = repo.db.query(
            """
            SELECT p.posting_id, p.external_job_id,
                   COALESCE(r.tier, 'daily') AS tier,
                   r.next_due_at_utc
              FROM postings p
              LEFT JOIN recheck_policy r ON r.posting_id = p.posting_id
            """
        )
        queued = 0
        deferred = 0
        for row in rows:
            job_id = str(row["external_job_id"])
            if job_id in listed:
                continue
            tier = str(row["tier"])
            if tier == "weekly":
                due = row["next_due_at_utc"]
                if due and str(due) > utc_str(now):
                    deferred += 1
                    continue
            if queued >= cfg.max_historical_rechecks_per_run:
                deferred += 1
                continue
            repo.enqueue(
                run_id=run_id,
                kind="posting_detail",
                work_key=job_id,
                payload={
                    "url": self._detail_url(repo, job_id),
                    "posting_id": int(row["posting_id"]),
                    "checked_because": (
                        "historical-weekly" if tier == "weekly" else "historical-daily"
                    ),
                },
                priority=(
                    DETAIL_PRIORITY_HISTORICAL_WEEKLY
                    if tier == "weekly"
                    else DETAIL_PRIORITY_HISTORICAL_DAILY
                ),
                max_attempts=2,
            )
            queued += 1
        if deferred:
            repo.record_gap(
                kind="historical_recheck_deferred",
                scope="detail",
                run_id=run_id,
                detail=f"{deferred} historical URL rechecks were deferred this run",
                evidence={
                    "deferred": deferred,
                    "bound": cfg.max_historical_rechecks_per_run,
                },
            )
        return queued

    def _detail_url(self, repo: Repository, job_id: str) -> str:
        row = repo.db.one(
            "SELECT pu.url FROM posting_urls pu JOIN postings p "
            "ON p.posting_id = pu.posting_id WHERE p.external_job_id = ? "
            "ORDER BY CASE pu.role WHEN 'canonical-detail' THEN 0 "
            "WHEN 'listing-link' THEN 1 ELSE 2 END, pu.last_seen_at_utc DESC LIMIT 1",
            (str(job_id),),
        )
        if row:
            return str(row["url"])
        base = self.cfg.collection.base_url.rstrip("/")
        return f"{base}/{self.cfg.collection.locale}/job/{job_id}"

    def _drain_details(
        self,
        repo: Repository,
        details: DetailCollector,
        run_id: int,
        max_details: int | None,
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        import json as _json

        stats = {
            "detail_attempted": 0,
            "detail_captured": 0,
            "detail_failed": 0,
            "detail_uncertain": 0,
            "detail_terminal": 0,
            "identity_mismatch": 0,
            "versions_created": 0,
            "resources_attempted": 0,
            "resources_captured": 0,
        }
        token = str(uuid.uuid4())
        while True:
            if max_details is not None and stats["detail_attempted"] >= max_details:
                break
            row = repo.claim_next(run_id, "posting_detail", token)
            if row is None:
                break
            payload = _json.loads(str(row["payload_json"] or "{}"))
            stats["detail_attempted"] += 1
            try:
                outcome = details.collect(
                    external_job_id=str(row["work_key"]),
                    url=str(payload.get("url")),
                    posting_id=payload.get("posting_id"),
                    checked_because=str(payload.get("checked_because", "listed")),
                )
            except ChallengeWall as exc:
                repo.requeue_or_fail(row, str(exc))
                errors.append({"kind": "access_control", "phase": "detail", "detail": str(exc)})
                break
            except Exception as exc:  # noqa: BLE001
                state = repo.requeue_or_fail(row, f"{type(exc).__name__}: {exc}")
                if state == "failed":
                    stats["detail_failed"] += 1
                    errors.append(
                        {
                            "kind": "detail_error",
                            "job_id": str(row["work_key"]),
                            "detail": f"{type(exc).__name__}: {exc}",
                        }
                    )
                continue

            if outcome.captured:
                stats["detail_captured"] += 1
                if outcome.version_created:
                    stats["versions_created"] += 1
                repo.complete_work(int(row["work_id"]), "done")
            elif outcome.uncertain:
                stats["detail_uncertain"] += 1
                state = repo.requeue_or_fail(row, outcome.detail or "uncertain")
                if state == "failed":
                    stats["detail_failed"] += 1
            else:
                if outcome.identity_state == "mismatch":
                    stats["identity_mismatch"] += 1
                stats["detail_terminal"] += 1
                repo.complete_work(int(row["work_id"]), "done", outcome.detail)

            for resource in outcome.resources:
                stats["resources_attempted"] += 1
                if resource.get("outcome") in ("captured", "reused_within_run"):
                    stats["resources_captured"] += 1

            if stats["detail_attempted"] % 25 == 0:
                repo.heartbeat(run_id)
        return stats

    def _collect_late_arrivals(
        self,
        repo: Repository,
        details: DetailCollector,
        run_id: int,
        source_id: int,
        job_ids: list[str],
        errors: list[dict[str, Any]],
    ) -> int:
        captured = 0
        for job_id in job_ids:
            row = repo.get_posting(job_id)
            posting_id = int(row["posting_id"]) if row else None
            try:
                outcome = details.collect(
                    external_job_id=job_id,
                    url=self._detail_url(repo, job_id),
                    posting_id=posting_id,
                    checked_because="listed",
                )
            except ChallengeWall as exc:
                errors.append(
                    {"kind": "access_control", "phase": "late_arrival", "detail": str(exc)}
                )
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "kind": "detail_error",
                        "job_id": job_id,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if outcome.captured:
                captured += 1
        return captured

    @staticmethod
    def _final_qualified(*scans: ScanResult | None) -> ScanResult | None:
        """The last qualified traversal is the run's authoritative listing set."""
        for scan in reversed([s for s in scans if s is not None]):
            if scan.qualified:
                return scan
        return None

    def _update_recheck_policy(self, repo: Repository, run_id: int, listed_ids: set[str]) -> None:
        from ..constants import TERMINAL_AVAILABILITY

        cfg = self.cfg.collection
        rows = repo.db.query(
            "SELECT o.posting_id, o.availability_state, o.observed_at_utc, "
            "p.external_job_id FROM posting_observations o "
            "JOIN postings p ON p.posting_id = o.posting_id WHERE o.run_id = ?",
            (run_id,),
        )
        for row in rows:
            posting_id = int(row["posting_id"])
            job_id = str(row["external_job_id"])
            state = str(row["availability_state"])
            observed = str(row["observed_at_utc"])
            current = repo.recheck_row(posting_id)
            streak = int(current["consecutive_terminal_observations"]) if current else 0

            if job_id in listed_ids:
                # Back in the listing: daily checking resumes immediately.
                repo.upsert_recheck(
                    posting_id=posting_id,
                    tier="daily",
                    consecutive_terminal=0,
                    last_checked_at_utc=observed,
                    next_due_at_utc=None,
                    reason="listed in the current qualified scan",
                )
                continue

            if state in TERMINAL_AVAILABILITY:
                streak += 1
            elif state in ("content_captured",):
                streak = 0
            else:
                # Uncertainty never advances the demotion streak.
                repo.upsert_recheck(
                    posting_id=posting_id,
                    tier=str(current["tier"]) if current else "daily",
                    consecutive_terminal=streak,
                    last_checked_at_utc=observed,
                    next_due_at_utc=str(current["next_due_at_utc"]) if current else None,
                    reason=f"last check was inconclusive ({state})",
                )
                continue

            if streak >= cfg.terminal_observations_before_weekly:
                due = now_utc() + timedelta(days=cfg.weekly_recheck_interval_days)
                repo.upsert_recheck(
                    posting_id=posting_id,
                    tier="weekly",
                    consecutive_terminal=streak,
                    last_checked_at_utc=observed,
                    next_due_at_utc=utc_str(due),
                    reason=(
                        f"{streak} consecutive terminal observations "
                        f"({state}); moved to the weekly recheck tier"
                    ),
                )
            else:
                repo.upsert_recheck(
                    posting_id=posting_id,
                    tier="daily",
                    consecutive_terminal=streak,
                    last_checked_at_utc=observed,
                    next_due_at_utc=None,
                    reason=f"unlisted; still checked daily ({state})",
                )

    @staticmethod
    def _outcome(
        discovery: ScanResult | None,
        final_scan: ScanResult | None,
        detail_stats: dict[str, Any],
        errors: list[dict[str, Any]],
        unresolved: bool,
    ) -> tuple[str, str | None]:
        if discovery is None:
            return "failed", "discovery traversal could not be completed"
        if final_scan is None:
            return (
                "partial",
                "no listing traversal qualified; positive observations preserved, "
                "absence conclusions suppressed",
            )
        problems: list[str] = []
        if unresolved:
            problems.append("listing sets did not reconcile")
        if detail_stats.get("detail_failed"):
            problems.append(f"{detail_stats['detail_failed']} detail retrievals failed")
        if detail_stats.get("detail_uncertain"):
            problems.append(
                f"{detail_stats['detail_uncertain']} detail retrievals were inconclusive"
            )
        if any(e.get("kind") == "access_control" for e in errors):
            problems.append("the source applied an access-control challenge")
        if problems:
            return "partial", "; ".join(problems)
        return "success", None

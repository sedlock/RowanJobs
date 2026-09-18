"""Run reporting: compose, deliver, and remember whether it arrived.

Policy (docs/OPERATIONS.md): a terminal status report goes out after **every
actual collection run** -- successful, partial, failed, a recovery attempt that
really collected, or a manual run. A retry window that finds nothing to do, and
any health or status command, send nothing at all.

Three invariants this module exists to keep:

* **Collection health and notification health are separate.** A bounced report
  never makes a successful harvest look unsuccessful, and a failed collection
  still gets its report.
* **One routine report per run.** Delivery state is a row keyed on the run, so a
  retry resends the same pending report rather than posting a second copy.
* **No recursive alerting.** A delivery failure is recorded, never itself
  emailed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import NOTIFICATION_KINDS
from ..db import Database
from ..timeutil import local_str, utc_str
from . import report as report_mod
from .credentials import CredentialError, check_permissions
from .mail import MailSettings, SmtpFactory, build_message, new_message_id, send

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

RUN_REPORT = NOTIFICATION_KINDS[0]

# Run kinds that represent an actual collection attempt. A retry that no-ops
# never reaches here: it does not create a run at all.
REPORTABLE_KINDS = ("daily", "retry", "manual", "verification")

# Outcomes where the collector never touched the source, so there is nothing to
# report to a person.
NON_COLLECTING_OUTCOMES = ("lock_contention",)

# Why a run predating the feature is recorded as deliberately not reported.
PREDATES_REPORTING = "this run finished before run reporting was configured"


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _table_exists(db: Database) -> bool:
    """An archive migrated before reporting existed simply has no history."""
    return bool(
        db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='notifications'")
    )


@dataclass(slots=True)
class DeliveryOutcome:
    state: str
    detail: str
    notification_id: int | None = None
    message_id: str | None = None
    accepted_at_utc: str | None = None
    retryable: bool = False

    @property
    def accepted(self) -> bool:
        return self.state == "accepted"


class Notifier:
    def __init__(self, cfg: Config, *, smtp_factory: SmtpFactory | None = None) -> None:
        self.cfg = cfg
        self.smtp_factory = smtp_factory
        self.settings = MailSettings(
            host=cfg.notify.smtp_host,
            port=cfg.notify.smtp_port,
            timeout_seconds=cfg.notify.smtp_timeout_seconds,
            credentials_path=Path(cfg.notify.credentials_path).expanduser(),
            sender=cfg.notify.sender,
            sender_name=cfg.notify.sender_name,
        )

    # ------------------------------------------------------------------ state

    @property
    def configured(self) -> bool:
        return bool(self.cfg.notify.kind == "smtp" and self.cfg.notify.recipient)

    def credential_problem(self) -> str | None:
        if not self.configured:
            return None
        try:
            check_permissions(Path(self.cfg.notify.credentials_path).expanduser())
        except CredentialError as exc:
            return str(exc)
        return None

    def status(self, db: Database | None = None) -> dict[str, Any]:
        """Notification health, deliberately independent of collection health."""
        base: dict[str, Any] = {
            "recipient": self.cfg.notify.recipient or None,
            "policy": "a report after every actual collection run",
        }
        if not self.cfg.notify.kind:
            return {
                **base,
                "state": "UNCONFIGURED",
                "detail": "no notification destination is configured; set [notify] kind",
            }
        if self.cfg.notify.kind != "smtp":
            return {
                **base,
                "state": "UNCONFIGURED",
                "detail": f"unsupported [notify] kind {self.cfg.notify.kind!r}",
            }
        if not self.cfg.notify.recipient:
            return {**base, "state": "UNCONFIGURED", "detail": "no [notify] recipient set"}
        problem = self.credential_problem()
        if problem:
            return {**base, "state": "FAILED", "detail": problem}

        if db is None or not _table_exists(db):
            return {**base, "state": "CONFIGURED", "detail": f"SMTP via {self.settings.host}"}

        last = db.one(
            "SELECT run_id, accepted_at_utc, message_id FROM notifications "
            "WHERE state = 'accepted' ORDER BY accepted_at_utc DESC LIMIT 1"
        )
        pending = int(db.scalar("SELECT COUNT(*) FROM notifications WHERE state = 'pending'") or 0)
        failed = int(db.scalar("SELECT COUNT(*) FROM notifications WHERE state = 'failed'") or 0)
        abandoned = int(
            db.scalar("SELECT COUNT(*) FROM notifications WHERE state = 'abandoned'") or 0
        )
        # A run that completed and never got a report row at all is the one
        # failure mode a delivery table cannot see on its own: silence. Count it
        # explicitly rather than letting it look like nothing was owed.
        unreported = self.unreported_runs(db, limit=None)
        if abandoned or failed or unreported:
            state = "FAILED" if abandoned else "DEGRADED"
            parts = []
            if failed:
                parts.append(f"{failed} report(s) awaiting retry")
            if abandoned:
                parts.append(f"{abandoned} gave up after their attempt budget")
            if unreported:
                parts.append(f"{len(unreported)} completed run(s) have no report at all")
            detail = "; ".join(parts)
        elif pending:
            state, detail = "DEGRADED", f"{pending} report(s) composed but not yet accepted"
        elif last:
            state = "VERIFIED"
            detail = (
                f"last report accepted by the provider at {local_str(str(last['accepted_at_utc']))}"
            )
        else:
            state, detail = "CONFIGURED", "no report has been sent yet"
        return {
            **base,
            "state": state,
            "detail": detail,
            "pending": pending,
            "failed": failed,
            "abandoned": abandoned,
            "unreported_runs": [int(r["run_id"]) for r in unreported],
            "last_accepted": {
                "run_id": int(last["run_id"]) if last else None,
                "accepted_at_utc": last["accepted_at_utc"] if last else None,
                "accepted_at_local": local_str(last["accepted_at_utc"]) if last else None,
                "message_id": last["message_id"] if last else None,
            }
            if last
            else None,
            "note": "acceptance by the provider is not the same claim as inbox receipt",
        }

    # ----------------------------------------------------------------- queue

    @staticmethod
    def should_report(run_kind: str, outcome: str) -> bool:
        """Whether this run warrants a report to a person."""
        return run_kind in REPORTABLE_KINDS and outcome not in NON_COLLECTING_OUTCOMES

    def queue(
        self,
        db: Database,
        run_id: int,
        *,
        delayed: bool = False,
        schedule: dict[str, Any] | None = None,
        revive: bool = False,
    ) -> int | None:
        """Compose the report and record it as pending. Idempotent per run.

        A run already reported and accepted is left alone -- that is what makes
        this safe to call on every collection. Anything else is re-composed in
        place rather than gaining a second row, so the one-report-per-run
        guarantee holds however many times delivery is attempted.

        ``revive`` is the operator asking explicitly for a run that was settled
        -- skipped as predating reporting, or abandoned after its budget -- to
        be sent after all. It restores the attempt budget, because the reason
        the earlier attempts failed has presumably been fixed.
        """
        if not self.configured:
            return None
        existing = db.one(
            "SELECT notification_id, state, delayed FROM notifications "
            "WHERE run_id = ? AND kind = ?",
            (run_id, RUN_REPORT),
        )
        if existing and str(existing["state"]) == "accepted":
            return int(existing["notification_id"])

        built = report_mod.build(
            db,
            run_id,
            delayed=delayed,
            schedule=schedule,
            controlpanel_url=self.cfg.notify.controlpanel_url or None,
        )
        digest = _digest(built.text_body)
        now = utc_str()
        # A report that has become late is relabelled as late; one that was
        # already marked delayed never quietly loses the label.
        late = 1 if (delayed or (existing and existing["delayed"])) else 0

        if existing:
            settled = str(existing["state"]) in ("skipped", "abandoned")
            notification_id = int(existing["notification_id"])
            if settled and not revive:
                return notification_id
            with db.write():
                db.execute(
                    "UPDATE notifications SET subject=?, body_sha256=?, delayed=?, "
                    "state=CASE WHEN ? THEN 'pending' ELSE state END, "
                    "attempts=CASE WHEN ? THEN 0 ELSE attempts END, "
                    "max_attempts=?, failure_kind=NULL, last_error=NULL, updated_at_utc=? "
                    "WHERE notification_id=?",
                    (
                        built.subject,
                        digest,
                        late,
                        settled,
                        settled,
                        self.cfg.notify.max_attempts,
                        now,
                        notification_id,
                    ),
                )
            return notification_id

        with db.write():
            return db.insert(
                """
                INSERT INTO notifications(
                    run_id, kind, recipient, sender, subject, body_sha256, state,
                    attempts, max_attempts, delayed, created_at_utc, updated_at_utc)
                VALUES (?,?,?,?,?,?,'pending',0,?,?,?,?)
                """,
                (
                    run_id,
                    RUN_REPORT,
                    self.cfg.notify.recipient,
                    self.settings.sender or None,
                    built.subject,
                    digest,
                    self.cfg.notify.max_attempts,
                    late,
                    now,
                    now,
                ),
            )

    # --------------------------------------------------------------- deliver

    def deliver(
        self,
        db: Database,
        notification_id: int,
        *,
        schedule: dict[str, Any] | None = None,
    ) -> DeliveryOutcome:
        """Attempt one delivery. Rebuilds the body from stored evidence."""
        row = db.one("SELECT * FROM notifications WHERE notification_id = ?", (notification_id,))
        if row is None:
            return DeliveryOutcome("missing", f"no notification {notification_id}")
        if str(row["state"]) == "accepted":
            return DeliveryOutcome(
                "accepted",
                "already accepted; not sending a second copy",
                notification_id=notification_id,
                message_id=row["message_id"],
                accepted_at_utc=row["accepted_at_utc"],
            )
        if str(row["state"]) == "skipped":
            # Settled, not owed. Only an explicit ask (queue(revive=True)) moves
            # a skipped row back to pending, so reaching here means nobody did.
            return DeliveryOutcome(
                "skipped",
                str(row["last_error"] or "this run was deliberately not reported"),
                notification_id=notification_id,
            )
        if str(row["state"]) == "abandoned":
            return DeliveryOutcome(
                "abandoned",
                f"attempt budget of {row['max_attempts']} exhausted: {row['last_error']}",
                notification_id=notification_id,
            )

        run_id = int(row["run_id"])
        built = report_mod.build(
            db,
            run_id,
            delayed=bool(row["delayed"]),
            schedule=schedule,
            controlpanel_url=self.cfg.notify.controlpanel_url or None,
        )
        message_id = new_message_id()
        message = build_message(
            sender=self.settings.sender or "",
            sender_name=self.settings.sender_name,
            recipient=str(row["recipient"]),
            subject=built.subject,
            text_body=built.text_body,
            html_body=built.html_body,
            message_id=message_id,
        )
        result = send(message, self.settings, smtp_factory=self.smtp_factory)
        attempts = int(row["attempts"]) + 1
        now = utc_str()
        # The body is rebuilt for every attempt, so the digest recorded is the
        # one that was actually handed to the provider -- not the one composed
        # when the report was first queued.
        digest = _digest(built.text_body)

        if result.accepted:
            with db.write():
                db.execute(
                    "UPDATE notifications SET state='accepted', attempts=?, "
                    "body_sha256=?, "
                    "first_attempt_at_utc=COALESCE(first_attempt_at_utc, ?), "
                    "last_attempt_at_utc=?, accepted_at_utc=?, message_id=?, "
                    "provider_response=?, failure_kind=NULL, last_error=NULL, "
                    "updated_at_utc=? WHERE notification_id=?",
                    (
                        attempts,
                        digest,
                        now,
                        now,
                        now,
                        result.message_id,
                        result.provider_response,
                        now,
                        notification_id,
                    ),
                )
            return DeliveryOutcome(
                "accepted",
                str(result.provider_response),
                notification_id=notification_id,
                message_id=result.message_id,
                accepted_at_utc=now,
            )

        exhausted = attempts >= int(row["max_attempts"]) or not result.retryable
        state = "abandoned" if exhausted else "failed"
        with db.write():
            db.execute(
                "UPDATE notifications SET state=?, attempts=?, body_sha256=?, "
                "first_attempt_at_utc=COALESCE(first_attempt_at_utc, ?), "
                "last_attempt_at_utc=?, failure_kind=?, last_error=?, updated_at_utc=? "
                "WHERE notification_id=?",
                (
                    state,
                    attempts,
                    digest,
                    now,
                    now,
                    result.failure_kind,
                    result.error,
                    now,
                    notification_id,
                ),
            )
        return DeliveryOutcome(
            state,
            str(result.error),
            notification_id=notification_id,
            retryable=result.retryable and not exhausted,
        )

    def report_run(
        self,
        db: Database,
        run_id: int,
        *,
        run_kind: str,
        outcome: str,
        delayed: bool = False,
        schedule: dict[str, Any] | None = None,
    ) -> DeliveryOutcome:
        """Queue and attempt the report for one run."""
        if not self.should_report(run_kind, outcome):
            return DeliveryOutcome(
                "skipped", f"a {run_kind} run with outcome {outcome!r} collects nothing"
            )
        if not self.configured:
            return DeliveryOutcome("unconfigured", self.status()["detail"])
        notification_id = self.queue(db, run_id, delayed=delayed, schedule=schedule)
        if notification_id is None:  # pragma: no cover - configured implies an id
            return DeliveryOutcome("unconfigured", "no destination configured")
        return self.deliver(db, notification_id, schedule=schedule)

    def retry_pending(
        self,
        db: Database,
        *,
        limit: int = 20,
        schedule: dict[str, Any] | None = None,
        exclude_run_id: int | None = None,
    ) -> list[DeliveryOutcome]:
        """Resend reports that were composed but never accepted.

        Mail only: this never re-runs a collection, so a provider outage costs
        nothing but a later email. ``exclude_run_id`` keeps a report that was
        just attempted seconds ago from being attempted again in the same
        process, which would spend its budget without waiting for anything to
        change.
        """
        clause = "" if exclude_run_id is None else "AND run_id != ? "
        params: list[Any] = [] if exclude_run_id is None else [exclude_run_id]
        rows = db.query(
            "SELECT notification_id FROM notifications WHERE state IN ('pending','failed') "
            f"{clause}ORDER BY created_at_utc LIMIT ?",
            (*params, limit),
        )
        return [self.deliver(db, int(r["notification_id"]), schedule=schedule) for r in rows]

    def seed_baseline(self, db: Database, *, exclude_run_id: int | None = None) -> int:
        """Record runs that finished before reporting existed as deliberately skipped.

        Without this, switching reporting on for an archive that already has
        history leaves every past run looking like a report that went missing,
        and a catch-up would post a burst of mail about collections the operator
        already knows the outcome of. A ``skipped`` row states the truth
        instead: reporting began at a point in time, and these runs precede it.

        Only ever runs against an archive with no delivery history at all, so it
        cannot silence a report that was genuinely due.
        """
        if not self.configured:
            return 0
        if int(db.scalar("SELECT COUNT(*) FROM notifications") or 0):
            return 0
        rows = self.unreported_runs(db, limit=None, exclude_run_id=exclude_run_id)
        if not rows:
            return 0
        now = utc_str()
        with db.write():
            for row in rows:
                db.insert(
                    """
                    INSERT INTO notifications(
                        run_id, kind, recipient, state, attempts, max_attempts,
                        created_at_utc, updated_at_utc, last_error)
                    VALUES (?,?,?,'skipped',0,0,?,?,?)
                    """,
                    (
                        int(row["run_id"]),
                        RUN_REPORT,
                        self.cfg.notify.recipient,
                        now,
                        now,
                        PREDATES_REPORTING,
                    ),
                )
        return len(rows)

    def sweep(
        self,
        db: Database,
        *,
        schedule: dict[str, Any] | None = None,
        catch_up: bool = False,
        exclude_run_id: int | None = None,
        limit: int = 20,
    ) -> list[DeliveryOutcome]:
        """Deliver whatever is still owed, without collecting anything.

        Reports already composed are retried. With ``catch_up`` a completed run
        that never got a report row at all is composed now and marked
        ``delayed`` -- the collection time stays the run's own, and the report
        says on its face that it was sent late.
        """
        if not self.configured:
            return []
        if catch_up:
            for row in self.unreported_runs(db, limit=limit, exclude_run_id=exclude_run_id):
                self.queue(db, int(row["run_id"]), delayed=True, schedule=schedule)
        return self.retry_pending(db, limit=limit, schedule=schedule, exclude_run_id=exclude_run_id)

    def unreported_runs(
        self,
        db: Database,
        *,
        limit: int | None = 5,
        exclude_run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Completed collection runs with no report row at all."""
        placeholders = ",".join("?" for _ in REPORTABLE_KINDS)
        excluded = ",".join("?" for _ in NON_COLLECTING_OUTCOMES)
        params: list[Any] = [RUN_REPORT, *REPORTABLE_KINDS, *NON_COLLECTING_OUTCOMES]
        clause = ""
        if exclude_run_id is not None:
            clause = "AND r.run_id != ? "
            params.append(exclude_run_id)
        tail = ""
        if limit is not None:
            tail = "LIMIT ?"
            params.append(limit)
        return db.query(
            f"""
            SELECT r.run_id, r.run_kind, r.outcome, r.ended_at_utc
              FROM collection_runs r
              LEFT JOIN notifications n ON n.run_id = r.run_id AND n.kind = ?
             WHERE n.notification_id IS NULL
               AND r.ended_at_utc IS NOT NULL
               AND r.run_kind IN ({placeholders})
               AND r.outcome NOT IN ({excluded})
               {clause}
             ORDER BY r.run_id DESC
             {tail}
            """,
            tuple(params),
        )

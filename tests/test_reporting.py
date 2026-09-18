"""Run reporting: the credential, the message, the report, and the record of it.

Every test here is offline. ``smtplib`` is never given a socket: a fake factory
is injected the same way ``SourceClient`` takes an httpx transport, so the
authentication failure and 4xx paths are exercised for real.

The invariants under test are the ones that would otherwise let the archive
mislead an operator:

* a failed email never changes what the collection says about itself;
* one report per run, no matter how many times delivery is attempted;
* provider acceptance is never reported as inbox receipt;
* a report sent late says so, and keeps the collection's own times;
* a run that predates reporting is recorded as deliberately skipped, not as a
  report that went missing.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rowanjobs.config import Config
from rowanjobs.constants import NOTIFICATION_STATES
from rowanjobs.db import Database
from rowanjobs.ops import report as report_mod
from rowanjobs.ops.credentials import CredentialError, load_credentials
from rowanjobs.ops.mail import MailSettings, build_message, send
from rowanjobs.ops.notify import PREDATES_REPORTING, Notifier

from .conftest import (
    LISTING_URL,
    FakeSource,
    build_detail_page,
    build_listing_page,
    listing_job,
)

DETAIL_URL = "https://jobs.rowan.edu/en-us/job/1001/job-1001"


# --------------------------------------------------------------- fake SMTP


class FakeSMTP:
    """Enough of ``smtplib.SMTP`` to drive every branch of ``send``."""

    def __init__(self, *, raises: Exception | None = None, refused: dict[str, Any] | None = None):
        self.raises = raises
        self.refused = refused or {}
        self.sent: list[Any] = []
        self.logins: list[tuple[str, str]] = []
        self.started_tls = False

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def ehlo(self) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        if isinstance(self.raises, smtplib.SMTPAuthenticationError):
            raise self.raises
        self.logins.append((username, password))

    def send_message(self, message: Any) -> dict[str, Any]:
        if self.raises is not None:
            raise self.raises
        self.sent.append(message)
        return self.refused


@pytest.fixture
def credentials_file(tmp_path: Path) -> Path:
    """A correctly stored credential file, mode 0600 in a 0700 directory."""
    directory = tmp_path / "secure"
    directory.mkdir(mode=0o700)
    path = directory / "credentials.env"
    path.write_text(
        "GMAIL_SMTP_USER=archiver@example.com\nGMAIL_APP_PASSWORD=abcd efgh ijkl mnop\n"
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def settings(credentials_file: Path) -> MailSettings:
    return MailSettings(
        host="smtp.example.com",
        port=587,
        credentials_path=credentials_file,
        sender="archiver@example.com",
    )


@pytest.fixture
def notify_cfg(cfg: Config, credentials_file: Path) -> Config:
    cfg.notify.kind = "smtp"
    cfg.notify.recipient = "operator@example.com"
    cfg.notify.sender = "archiver@example.com"
    cfg.notify.credentials_path = str(credentials_file)
    cfg.notify.smtp_host = "smtp.example.com"
    return cfg


def make_notifier(cfg: Config, smtp: FakeSMTP) -> Notifier:
    return Notifier(cfg, smtp_factory=lambda *_args: smtp)  # type: ignore[arg-type,return-value]


@pytest.fixture
def one_job_source() -> FakeSource:
    source = FakeSource()
    source.page(LISTING_URL, build_listing_page([listing_job("1001", slug="job-1001")]))
    source.page(DETAIL_URL, build_detail_page(job_id="1001"))
    return source


# ------------------------------------------------------------- credentials


def test_a_missing_credential_file_is_named_not_guessed_at(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="no SMTP credential file"):
        load_credentials(tmp_path / "nope.env")


def test_a_credential_file_other_accounts_could_read_is_refused(credentials_file: Path) -> None:
    credentials_file.chmod(0o644)
    with pytest.raises(CredentialError, match="must be 0600"):
        load_credentials(credentials_file)


def test_a_credential_directory_others_can_enter_is_refused(credentials_file: Path) -> None:
    credentials_file.parent.chmod(0o755)
    try:
        with pytest.raises(CredentialError, match="group- or world-accessible"):
            load_credentials(credentials_file)
    finally:
        credentials_file.parent.chmod(0o700)


def test_the_spaces_google_displays_an_app_password_with_are_stripped(
    credentials_file: Path,
) -> None:
    creds = load_credentials(credentials_file)
    assert creds.password == "abcdefghijklmnop"  # noqa: S105 - a fixture value, not a secret
    assert creds.username == "archiver@example.com"


def test_quoting_comments_and_export_prefixes_are_understood(credentials_file: Path) -> None:
    credentials_file.write_text(
        "# a comment\n"
        "\n"
        'export GMAIL_SMTP_USER="quoted@example.com"\n'
        "GMAIL_APP_PASSWORD='wxyz abcd efgh ijkl'\n"
    )
    credentials_file.chmod(0o600)
    creds = load_credentials(credentials_file)
    assert creds.username == "quoted@example.com"
    assert creds.password == "wxyzabcdefghijkl"  # noqa: S105 - a fixture value, not a secret


def test_a_credential_never_renders_itself(credentials_file: Path) -> None:
    creds = load_credentials(credentials_file)
    assert "abcdefghijklmnop" not in repr(creds)
    assert "abcdefghijklmnop" not in str(creds)
    assert "abcdefghijklmnop" not in f"{creds}"
    assert "<redacted>" in repr(creds)


def test_a_missing_credential_is_a_permanent_delivery_failure_not_a_crash(
    tmp_path: Path,
) -> None:
    message = build_message(
        sender="a@example.com",
        sender_name="RowanJobs",
        recipient="b@example.com",
        subject="s",
        text_body="t",
        html_body="<p>t</p>",
    )
    result = send(message, MailSettings(credentials_path=tmp_path / "absent.env"))
    assert result.accepted is False
    assert result.failure_kind == "permanent"
    assert result.retryable is False


# -------------------------------------------------------------------- mail


def test_the_message_carries_a_real_text_part_before_the_html_one() -> None:
    message = build_message(
        sender="a@example.com",
        sender_name="RowanJobs",
        recipient="b@example.com",
        subject="RowanJobs OK",
        text_body="the whole report in text",
        html_body="<p>the whole report in html</p>",
    )
    assert message.get_content_type() == "multipart/alternative"
    parts = message.get_payload()
    assert parts[0].get_content_type() == "text/plain"
    assert parts[1].get_content_type() == "text/html"
    assert "the whole report in text" in parts[0].get_content()
    # Routine machine mail must not provoke vacation responders.
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["X-Auto-Response-Suppress"] == "All"


def test_acceptance_names_the_submission_server_and_nothing_more(settings: MailSettings) -> None:
    smtp = FakeSMTP()
    message = build_message(
        sender="a@example.com",
        sender_name="RowanJobs",
        recipient="b@example.com",
        subject="s",
        text_body="t",
        html_body="<p>t</p>",
    )
    result = send(message, settings, smtp_factory=lambda *_a: smtp)  # type: ignore[arg-type,return-value]

    assert result.accepted is True
    assert result.provider_response == "250 accepted for delivery by smtp.example.com"
    assert "delivered" not in str(result.provider_response)
    assert "inbox" not in str(result.provider_response)
    assert smtp.started_tls is True
    assert smtp.logins == [("archiver@example.com", "abcdefghijklmnop")]


def test_a_rejected_app_password_is_permanent_and_never_echoes_the_secret(
    settings: MailSettings,
) -> None:
    smtp = FakeSMTP(
        raises=smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    )
    message = build_message(
        sender="a@example.com",
        sender_name="RowanJobs",
        recipient="b@example.com",
        subject="s",
        text_body="t",
        html_body="<p>t</p>",
    )
    result = send(message, settings, smtp_factory=lambda *_a: smtp)  # type: ignore[arg-type,return-value]

    assert result.failure_kind == "permanent"
    assert result.retryable is False
    assert "535" in str(result.error)
    assert "abcdefghijklmnop" not in str(result.error)
    assert "Username and Password" not in str(result.error)


@pytest.mark.parametrize(
    ("code", "expected"),
    [(421, "transient"), (450, "transient"), (550, "permanent"), (552, "permanent")],
)
def test_a_4xx_is_worth_retrying_and_a_5xx_is_not(
    settings: MailSettings, code: int, expected: str
) -> None:
    smtp = FakeSMTP(raises=smtplib.SMTPResponseException(code, b"nope"))
    message = build_message(
        sender="a@example.com",
        sender_name="RowanJobs",
        recipient="b@example.com",
        subject="s",
        text_body="t",
        html_body="<p>t</p>",
    )
    result = send(message, settings, smtp_factory=lambda *_a: smtp)  # type: ignore[arg-type,return-value]
    assert result.failure_kind == expected
    assert result.retryable is (expected == "transient")


def test_a_network_problem_is_transient(settings: MailSettings) -> None:
    smtp = FakeSMTP(raises=TimeoutError("timed out"))
    message = build_message(
        sender="a@example.com",
        sender_name="RowanJobs",
        recipient="b@example.com",
        subject="s",
        text_body="t",
        html_body="<p>t</p>",
    )
    result = send(message, settings, smtp_factory=lambda *_a: smtp)  # type: ignore[arg-type,return-value]
    assert result.failure_kind == "transient"
    assert result.retryable is True


def test_a_refused_recipient_is_not_reported_as_accepted(settings: MailSettings) -> None:
    smtp = FakeSMTP(refused={"b@example.com": (550, b"no such user")})
    message = build_message(
        sender="a@example.com",
        sender_name="RowanJobs",
        recipient="b@example.com",
        subject="s",
        text_body="t",
        html_body="<p>t</p>",
    )
    result = send(message, settings, smtp_factory=lambda *_a: smtp)  # type: ignore[arg-type,return-value]
    assert result.accepted is False
    assert result.failure_kind == "permanent"


# ------------------------------------------------------------------ report


def test_a_baseline_collection_does_not_present_its_discoveries_as_news(
    db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="manual")
    built = report_mod.build(db, result.run_id)

    assert built.facts["is_baseline"] is True
    assert built.facts["newly_observed"] is None
    assert "not meaningful for a baseline collection" in built.text_body
    assert "none of them are newly published" in built.text_body
    assert "none of them are newly published" in built.html_body


def test_a_later_collection_does_report_change_counts(
    db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    collect(one_job_source, run_kind="manual")
    second = collect(one_job_source, run_kind="manual")

    built = report_mod.build(db, second.run_id)
    assert built.facts["is_baseline"] is False
    assert built.facts["newly_observed"] == 0
    assert "Newly observed advertisements: 0" in built.text_body


def test_a_delayed_report_says_so_and_keeps_the_collection_s_own_times(
    db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")

    punctual = report_mod.build(db, result.run_id)
    late = report_mod.build(db, result.run_id, delayed=True)

    assert "(delayed report)" in late.subject
    assert "(delayed report)" not in punctual.subject
    assert "DELAYED REPORT" in late.text_body
    assert "Delayed report." in late.html_body
    # The collection's own times are unchanged: only the sending is late.
    assert late.facts["started_at_local"] == punctual.facts["started_at_local"]
    assert late.facts["ended_at_local"] == punctual.facts["ended_at_local"]


def test_the_subject_states_the_outcome_the_date_and_the_count(
    db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    built = report_mod.build(db, result.run_id)

    assert built.subject.startswith("RowanJobs OK — ")
    assert "1 advertisements" in built.subject
    assert built.facts["collection_date"] in built.subject


def test_a_report_for_an_unknown_run_is_an_error_not_an_empty_report(db: Database) -> None:
    with pytest.raises(KeyError):
        report_mod.build(db, 9999)


def test_the_html_report_escapes_what_it_quotes(
    db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="manual")
    with db.write():
        db.execute(
            "UPDATE collection_runs SET outcome_detail = ? WHERE run_id = ?",
            ("<script>alert('x')</script>", result.run_id),
        )
    built = report_mod.build(db, result.run_id)
    assert "<script>" not in built.html_body
    assert "&lt;script&gt;" in built.html_body


# ------------------------------------------------------------------ notify


def test_with_no_recipient_nothing_is_sent_and_the_state_says_why(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP()
    notifier = make_notifier(cfg, smtp)

    outcome = notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")

    assert outcome.state == "unconfigured"
    assert smtp.sent == []
    assert int(db.scalar("SELECT COUNT(*) FROM notifications") or 0) == 0
    assert notifier.status(db)["state"] == "UNCONFIGURED"


def test_a_run_report_is_sent_once_and_recorded_as_accepted(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP()
    notifier = make_notifier(notify_cfg, smtp)

    outcome = notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")

    assert outcome.state == "accepted"
    assert len(smtp.sent) == 1
    assert smtp.sent[0]["To"] == "operator@example.com"
    row = db.one("SELECT * FROM notifications WHERE run_id = ?", (result.run_id,))
    assert row["state"] == "accepted"
    assert row["attempts"] == 1
    assert row["accepted_at_utc"] and row["message_id"]
    assert row["delayed"] == 0


def test_reporting_the_same_run_twice_does_not_post_a_second_copy(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP()
    notifier = make_notifier(notify_cfg, smtp)

    notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")
    again = notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")

    assert again.state == "accepted"
    assert "not sending a second copy" in again.detail
    assert len(smtp.sent) == 1
    assert int(db.scalar("SELECT COUNT(*) FROM notifications") or 0) == 1


def test_a_transient_failure_is_kept_for_retry_and_the_retry_sends_it(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    failing = FakeSMTP(raises=smtplib.SMTPResponseException(451, b"try later"))
    notifier = make_notifier(notify_cfg, failing)

    first = notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")
    assert first.state == "failed"
    assert first.retryable is True
    row = db.one("SELECT * FROM notifications WHERE run_id = ?", (result.run_id,))
    assert row["attempts"] == 1
    assert row["accepted_at_utc"] is None

    working = FakeSMTP()
    notifier.smtp_factory = lambda *_a: working  # type: ignore[assignment]
    swept = notifier.sweep(db)

    assert [o.state for o in swept] == ["accepted"]
    assert len(working.sent) == 1
    row = db.one("SELECT * FROM notifications WHERE run_id = ?", (result.run_id,))
    assert row["state"] == "accepted"
    assert row["attempts"] == 2


def test_a_permanent_failure_is_abandoned_rather_than_retried_forever(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP(raises=smtplib.SMTPResponseException(550, b"mailbox unavailable"))
    notifier = make_notifier(notify_cfg, smtp)

    outcome = notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")

    assert outcome.state == "abandoned"
    assert outcome.retryable is False
    assert db.one("SELECT * FROM notifications", ())["attempts"] == 1
    # A sweep does not keep hammering a permanently broken destination.
    assert notifier.sweep(db) == []


def test_the_attempt_budget_is_finite(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    notify_cfg.notify.max_attempts = 3
    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP(raises=smtplib.SMTPResponseException(451, b"try later"))
    notifier = make_notifier(notify_cfg, smtp)

    notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")
    notifier.sweep(db)
    notifier.sweep(db)
    row = db.one("SELECT * FROM notifications", ())

    assert row["attempts"] == 3
    assert row["state"] == "abandoned"
    assert notifier.status(db)["state"] == "FAILED"


def test_a_failed_report_never_changes_what_the_collection_says_about_itself(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    notifier = make_notifier(notify_cfg, FakeSMTP(raises=TimeoutError("down")))

    before = db.one(
        "SELECT outcome, outcome_detail FROM collection_runs WHERE run_id = ?", (result.run_id,)
    )
    notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")
    after = db.one(
        "SELECT outcome, outcome_detail FROM collection_runs WHERE run_id = ?", (result.run_id,)
    )

    assert before["outcome"] == after["outcome"] == "success"
    assert before["outcome_detail"] == after["outcome_detail"]
    assert db.scalar("SELECT COUNT(*) FROM coverage_gaps WHERE run_id = ?", (result.run_id,)) == 0


def test_a_run_that_collected_nothing_is_not_reported_to_a_person(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP()
    notifier = make_notifier(notify_cfg, smtp)

    outcome = notifier.report_run(db, result.run_id, run_kind="daily", outcome="lock_contention")

    assert outcome.state == "skipped"
    assert smtp.sent == []
    assert int(db.scalar("SELECT COUNT(*) FROM notifications") or 0) == 0


def test_runs_predating_reporting_are_recorded_as_skipped_not_missing(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    old_one = collect(one_job_source, run_kind="daily")
    old_two = collect(one_job_source, run_kind="manual")
    smtp = FakeSMTP()
    notifier = make_notifier(notify_cfg, smtp)

    seeded = notifier.seed_baseline(db)

    assert seeded == 2
    assert smtp.sent == []
    states = {
        int(r["run_id"]): str(r["state"])
        for r in db.query("SELECT run_id, state FROM notifications", ())
    }
    assert states == {old_one.run_id: "skipped", old_two.run_id: "skipped"}
    assert db.one("SELECT last_error FROM notifications LIMIT 1", ())["last_error"] == (
        PREDATES_REPORTING
    )
    # A skipped run is settled: health is clean and nothing is owed.
    assert notifier.status(db)["state"] == "CONFIGURED"
    assert notifier.unreported_runs(db) == []


def test_the_baseline_marker_only_ever_runs_on_an_archive_with_no_history(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    first = collect(one_job_source, run_kind="daily")
    notifier = make_notifier(notify_cfg, FakeSMTP())
    notifier.report_run(db, first.run_id, run_kind="daily", outcome="success")

    # A second run arrives after reporting is live. It is genuinely owed a
    # report, so the baseline marker must not quietly write it off as history.
    second = collect(one_job_source, run_kind="daily")

    assert notifier.seed_baseline(db) == 0
    assert [int(r["run_id"]) for r in notifier.unreported_runs(db)] == [second.run_id]
    assert db.scalar("SELECT COUNT(*) FROM notifications WHERE state = 'skipped'") == 0


def test_the_current_run_is_never_marked_as_predating_reporting(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    old = collect(one_job_source, run_kind="daily")
    current = collect(one_job_source, run_kind="daily")
    notifier = make_notifier(notify_cfg, FakeSMTP())

    notifier.seed_baseline(db, exclude_run_id=current.run_id)

    rows = {
        int(r["run_id"]): str(r["state"])
        for r in db.query("SELECT run_id, state FROM notifications", ())
    }
    assert rows == {old.run_id: "skipped"}


def test_a_completed_run_with_no_report_at_all_is_visible_as_silence(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    notifier = make_notifier(notify_cfg, FakeSMTP())

    status = notifier.status(db)

    assert status["state"] == "DEGRADED"
    assert status["unreported_runs"] == [result.run_id]
    assert "no report at all" in status["detail"]


def test_a_catch_up_is_composed_and_labelled_delayed(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP()
    notifier = make_notifier(notify_cfg, smtp)

    outcomes = notifier.sweep(db, catch_up=True)

    assert [o.state for o in outcomes] == ["accepted"]
    row = db.one("SELECT run_id, delayed FROM notifications", ())
    assert int(row["run_id"]) == result.run_id
    assert row["delayed"] == 1
    assert "(delayed report)" in str(smtp.sent[0]["Subject"])
    assert notifier.status(db)["state"] == "VERIFIED"


def test_a_sweep_without_catch_up_composes_nothing_new(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP()
    notifier = make_notifier(notify_cfg, smtp)

    assert notifier.sweep(db) == []
    assert smtp.sent == []


def test_status_never_calls_provider_acceptance_inbox_receipt(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    notifier = make_notifier(notify_cfg, FakeSMTP())
    notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")

    status = notifier.status(db)

    assert status["state"] == "VERIFIED"
    assert "accepted by the provider" in status["detail"]
    assert status["note"] == "acceptance by the provider is not the same claim as inbox receipt"
    assert status["last_accepted"]["run_id"] == result.run_id


def test_an_unreadable_credential_file_is_reported_before_anything_is_sent(
    notify_cfg: Config, credentials_file: Path
) -> None:
    credentials_file.chmod(0o644)
    try:
        status = Notifier(notify_cfg).status()
    finally:
        credentials_file.chmod(0o600)

    assert status["state"] == "FAILED"
    assert "must be 0600" in status["detail"]


def test_the_recorded_digest_is_of_the_body_that_was_actually_sent(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    import hashlib

    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP()
    notifier = make_notifier(notify_cfg, smtp)
    notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")

    sent_text = smtp.sent[0].get_payload()[0].get_content()
    recorded = db.one("SELECT body_sha256 FROM notifications", ())["body_sha256"]
    assert recorded == hashlib.sha256(sent_text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- the schema


def test_the_notification_states_in_the_schema_match_the_python_enumeration(
    db: Database,
) -> None:
    sql = str(
        db.one("SELECT sql FROM sqlite_master WHERE type='table' AND name='notifications'", ())[
            "sql"
        ]
    )
    quoted = {part.strip("'") for part in sql.split("state IN (")[1].split(")")[0].split(",")}
    assert quoted == set(NOTIFICATION_STATES)


def test_a_row_cannot_claim_acceptance_without_the_evidence_of_it(db: Database) -> None:
    import apsw

    with pytest.raises(apsw.ConstraintError), db.write():
        db.execute(
            "INSERT INTO notifications(run_id, kind, recipient, subject, body_sha256, "
            "state, created_at_utc, updated_at_utc) "
            "VALUES (NULL,'run_report','a@b','s','d','accepted','t','t')"
        )


def test_only_a_skipped_row_may_have_no_composed_body(db: Database) -> None:
    import apsw

    with db.write():
        db.execute(
            "INSERT INTO notifications(run_id, kind, recipient, state, "
            "created_at_utc, updated_at_utc) "
            "VALUES (NULL,'run_report','a@b','skipped','t','t')"
        )
    with pytest.raises(apsw.ConstraintError), db.write():
        db.execute(
            "INSERT INTO notifications(run_id, kind, recipient, state, "
            "created_at_utc, updated_at_utc) "
            "VALUES (NULL,'run_report','a@b','pending','t','t')"
        )


def test_one_routine_report_per_run_is_a_schema_guarantee(
    db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    import apsw

    result = collect(one_job_source, run_kind="daily")
    values = "(?,'run_report','a@b','s','d','pending','t','t')"
    columns = (
        "INSERT INTO notifications(run_id, kind, recipient, subject, body_sha256, "
        "state, created_at_utc, updated_at_utc) VALUES "
    )
    with db.write():
        db.execute(columns + values, (result.run_id,))
    with pytest.raises(apsw.ConstraintError), db.write():
        db.execute(columns + values, (result.run_id,))


def test_a_run_written_off_as_predating_reporting_can_still_be_asked_for(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP()
    notifier = make_notifier(notify_cfg, smtp)
    notifier.seed_baseline(db)
    assert db.one("SELECT state FROM notifications", ())["state"] == "skipped"

    # Without an explicit ask, a settled run stays settled.
    notifier.queue(db, result.run_id, delayed=True)
    assert db.one("SELECT state FROM notifications", ())["state"] == "skipped"

    notification_id = notifier.queue(db, result.run_id, delayed=True, revive=True)
    outcome = notifier.deliver(db, notification_id)

    assert outcome.state == "accepted"
    row = db.one("SELECT state, delayed, subject FROM notifications", ())
    assert row["state"] == "accepted"
    assert row["delayed"] == 1
    assert "(delayed report)" in str(row["subject"])
    assert len(smtp.sent) == 1


def test_an_abandoned_report_is_revived_with_a_fresh_budget(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    notifier = make_notifier(notify_cfg, FakeSMTP(raises=smtplib.SMTPResponseException(550, b"no")))
    notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")
    assert db.one("SELECT state, attempts FROM notifications", ())["state"] == "abandoned"

    working = FakeSMTP()
    notifier.smtp_factory = lambda *_a: working  # type: ignore[assignment]
    notification_id = notifier.queue(db, result.run_id, delayed=True, revive=True)
    assert db.one("SELECT state, attempts FROM notifications", ())["attempts"] == 0

    assert notifier.deliver(db, notification_id).state == "accepted"
    assert len(working.sent) == 1


def test_an_accepted_report_is_never_re_composed_even_when_asked_for(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP()
    notifier = make_notifier(notify_cfg, smtp)
    notifier.report_run(db, result.run_id, run_kind="daily", outcome="success")
    before = db.one("SELECT subject, accepted_at_utc, message_id FROM notifications", ())

    notification_id = notifier.queue(db, result.run_id, delayed=True, revive=True)
    outcome = notifier.deliver(db, notification_id)

    assert outcome.state == "accepted"
    after = db.one("SELECT subject, accepted_at_utc, message_id FROM notifications", ())
    assert dict(after) == dict(before)
    assert len(smtp.sent) == 1


def test_an_unset_sender_becomes_the_authenticated_account_not_an_empty_address(
    settings: MailSettings,
) -> None:
    settings.sender = ""
    smtp = FakeSMTP()
    message = build_message(
        sender="",
        sender_name="RowanJobs",
        recipient="b@example.com",
        subject="s",
        text_body="t",
        html_body="<p>t</p>",
    )
    assert message.get("From") is None

    result = send(message, settings, smtp_factory=lambda *_a: smtp)  # type: ignore[arg-type,return-value]

    assert result.accepted is True
    assert message["From"] == "RowanJobs <archiver@example.com>"
    assert "<>" not in str(message["From"])


def test_delivering_a_settled_row_sends_nothing_without_an_explicit_ask(
    notify_cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    smtp = FakeSMTP()
    notifier = make_notifier(notify_cfg, smtp)
    notifier.seed_baseline(db)
    notification_id = int(
        db.one("SELECT notification_id FROM notifications", ())["notification_id"]
    )

    outcome = notifier.deliver(db, notification_id)

    assert outcome.state == "skipped"
    assert outcome.detail == PREDATES_REPORTING
    assert smtp.sent == []
    # And reporting the run again does not sneak it out either.
    assert notifier.report_run(db, result.run_id, run_kind="daily", outcome="success").state == (
        "skipped"
    )
    assert smtp.sent == []


@pytest.mark.parametrize("notification_state", ["FAILED", "DEGRADED", "UNCONFIGURED"])
def test_notification_health_can_never_change_the_collection_exit_code(
    notification_state: str,
) -> None:
    from rowanjobs.ops.health import exit_code_for

    payload = {
        "collection": {"state": "HEALTHY"},
        "backup": {"local": {"state": "VERIFIED"}, "offhost": {"state": "VERIFIED"}},
        "notifications": {"state": notification_state},
    }
    assert exit_code_for(payload) == 0


def test_a_shortfall_says_whether_it_was_a_failure_or_a_disappearance(
    db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    # An advertisement taken down between the traversal and the retrieval is
    # not a failed retrieval, and must not be reported as one.
    with db.write():
        db.execute(
            "UPDATE posting_observations SET availability_state = 'redirected_to_listing', "
            "availability_detail = 'detail URL bounced to the listing' WHERE run_id = ?",
            (result.run_id,),
        )
    facts = report_mod.gather(db, result.run_id)
    facts["detail_captured"], facts["detail_attempted"] = 4, 5

    line = report_mod._captured_line(facts)

    assert "no longer published" in line
    assert "could not be checked" not in line


def test_an_uncertain_retrieval_is_never_described_as_a_disappearance(
    db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    with db.write():
        db.execute(
            "UPDATE posting_observations SET availability_state = 'access_control_challenge', "
            "availability_detail = 'challenged' WHERE run_id = ?",
            (result.run_id,),
        )
    facts = report_mod.gather(db, result.run_id)
    facts["detail_captured"], facts["detail_attempted"] = 4, 5

    line = report_mod._captured_line(facts)

    assert "could not be checked" in line
    assert "no longer published" not in line

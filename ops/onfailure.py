#!/usr/bin/env python3
"""Independent failure handler for the RowanJobs units.

systemd runs this via ``OnFailure=`` when ``rowanjobs.service`` or
``rowanjobs-retry.service`` fails. Its whole reason to exist is the
2026-09-18 outage: a configuration-schema mismatch made the application exit
before it opened a database run row, so the archive recorded nothing, the
health output saw nothing missing, and no report went out. The failure was
invisible until someone read the journal.

**This script therefore imports nothing from rowanjobs.** It is standard
library only. A detector that depends on the configuration parser it is
monitoring cannot report that the parser is broken. It reads the config with
``tomllib`` defensively, tolerates every key being absent or wrong, and still
produces a record if the config file is unparseable.

What it does, in order of importance:

1. Appends a durable record to ``<data_root>/runtime/startup-failures.jsonl``.
   That file is operational evidence, never archive evidence: it says a
   scheduled activation failed, and it never implies a source observation
   occurred. ``rowanjobs health`` reads it and surfaces unresolved entries.
2. Best-effort emails the failure. If mail is unconfigured or also broken, the
   record above still exists and step 1 has already succeeded.

It never collects, never migrates, never touches the archive database, and
exits 0 even when it fails, so a handler problem cannot mask the original
failure in the journal.
"""

from __future__ import annotations

import contextlib
import json
import os
import smtplib
import socket
import ssl
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

APP = "rowanjobs"
TZ = ZoneInfo("America/New_York")
RECORD_NAME = "startup-failures.jsonl"
SCHEMA = "rowanjobs.startup-failure.v1"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def utc_str(moment: datetime | None = None) -> str:
    return (moment or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_str(moment: datetime | None = None) -> str:
    return (moment or utcnow()).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def config_path() -> Path:
    override = os.environ.get("ROWANJOBS_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / APP / "config.toml"


def load_config() -> tuple[dict, str | None]:
    """Read the config defensively. An unreadable config is the thing we report."""
    path = config_path()
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle), None
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def data_root(config: dict) -> Path:
    override = os.environ.get("ROWANJOBS_DATA_ROOT")
    if override:
        return Path(override).expanduser()
    configured = config.get("data_root")
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / APP


def journal_tail(unit: str, lines: int = 25) -> str:
    try:
        proc = subprocess.run(  # noqa: S603
            ["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout.strip()[:4000]
    except (OSError, subprocess.SubprocessError):
        return ""


def unit_properties(unit: str) -> dict[str, str]:
    try:
        proc = subprocess.run(  # noqa: S603
            [  # noqa: S607 - resolved from PATH by design
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=Result,ExecMainStatus,ExecMainExitTimestamp,InvocationID",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return dict(line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line)
    except (OSError, subprocess.SubprocessError):
        return {}


def append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def read_credentials(path: Path) -> tuple[str, str] | None:
    """Same 0600 rule the application enforces, restated without importing it."""
    try:
        if path.stat().st_mode & 0o077:
            return None
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line[len("export ") :].strip() if line.startswith("export ") else line
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    user = values.get("GMAIL_SMTP_USER", "").strip()
    password = values.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not user or not password:
        return None
    return user, password


def send_mail(config: dict, record: dict) -> dict:
    """Best effort. A mail problem never hides the record already written."""
    notify = config.get("notify") if isinstance(config.get("notify"), dict) else {}
    recipient = notify.get("recipient") or ""
    if notify.get("kind") != "smtp" or not recipient:
        return {"attempted": False, "detail": "no SMTP recipient configured"}

    raw_path = notify.get("credentials_path") or "~/.config/rowanjobs/credentials.env"
    creds = read_credentials(Path(str(raw_path)).expanduser())
    if creds is None:
        return {"attempted": False, "detail": "credential unreadable or insecurely stored"}
    username, password = creds

    body = (
        f"RowanJobs FAILED TO START\n\n"
        f"Unit        : {record['unit']}\n"
        f"When        : {record['failed_at_local']}\n"
        f"Scheduled slot: {record['slot_local_date']}\n"
        f"Result      : {record.get('result')} (exit status {record.get('exit_status')})\n"
        f"Host        : {record['host']}\n\n"
        "The application exited before it could record a collection run, so the\n"
        "archive holds NO evidence for this activation. This is a startup failure,\n"
        "not a collection result: nothing was observed at the source.\n\n"
        f"{'This slot has no successful collection yet.' if not record.get('resolved') else ''}\n\n"
        "Journal tail:\n"
        f"{record.get('journal', '(unavailable)')}\n\n"
        "Recorded in runtime/startup-failures.jsonl and surfaced by\n"
        "`rowanjobs health` until a run for this slot succeeds.\n"
    )
    message = EmailMessage()
    message["From"] = f"RowanJobs <{username}>"
    message["To"] = recipient
    message["Subject"] = (
        f"RowanJobs FAILED TO START — {record['slot_local_date']} — {record['unit']}"
    )
    message["Date"] = format_datetime(utcnow())
    message["Message-ID"] = make_msgid(domain="rowanjobs.local")
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message["X-RowanJobs-Report"] = "startup-failure"
    message.set_content(body)

    host = notify.get("smtp_host") or "smtp.gmail.com"
    port = notify.get("smtp_port") or 587
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(str(host), int(port), timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 - a handler must never raise
        # Deliberately no credential material: only the exception type and a
        # short reason.
        return {"attempted": True, "accepted": False, "detail": type(exc).__name__}
    return {"attempted": True, "accepted": True, "message_id": message["Message-ID"]}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    unit = args[0] if args else "rowanjobs.service"

    config, config_error = load_config()
    moment = utcnow()
    properties = unit_properties(unit)

    record = {
        "schema": SCHEMA,
        "kind": "startup_failure",
        "unit": unit,
        "host": socket.gethostname(),
        "failed_at_utc": utc_str(moment),
        "failed_at_local": local_str(moment),
        "slot_local_date": moment.astimezone(TZ).date().isoformat(),
        "result": properties.get("Result"),
        "exit_status": properties.get("ExecMainStatus"),
        "invocation_id": properties.get("InvocationID"),
        "config_path": str(config_path()),
        "config_error": config_error,
        "journal": journal_tail(unit),
        "note": (
            "the application exited before recording a collection run; "
            "no source observation occurred"
        ),
    }

    root = data_root(config)
    try:
        append_record(root / "runtime" / RECORD_NAME, record)
        record["recorded"] = True
    except OSError as exc:
        record["recorded"] = False
        print(f"could not write the failure record: {exc}", file=sys.stderr)

    outcome = send_mail(config, record)
    print(json.dumps({"failure": record["unit"], "mail": outcome}, ensure_ascii=False))
    # Always 0: the handler's own exit code must not add a second failed unit.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

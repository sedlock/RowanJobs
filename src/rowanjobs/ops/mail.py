"""Gmail SMTP delivery.

Delivery is over STARTTLS with certificate verification, authenticated with a
Gmail App Password held in a 0600 file outside Git.

The distinction this module exists to preserve: **provider acceptance is not
inbox receipt**. A 250 from Gmail's submission server means Gmail took
responsibility for the message. It does not mean it was delivered, and nothing
here ever claims that it was.

Failures are classified so a retry cannot spin forever on a permanent problem:
a rejected recipient or a bad App Password is terminal, a timeout or a 4xx is
worth another attempt.
"""

from __future__ import annotations

import smtplib
import ssl
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

from ..timeutil import now_utc, utc_str
from .credentials import CredentialError, load_credentials

TRANSIENT = "transient"
PERMANENT = "permanent"

# Injected in tests so the suite can exercise every delivery path -- including
# authentication failure and a 4xx -- without opening a socket, the same way
# SourceClient takes an httpx transport and RequestBudget takes a sleeper.
SmtpFactory = Callable[[str, int, float], smtplib.SMTP]


def _default_smtp(host: str, port: int, timeout: float) -> smtplib.SMTP:
    return smtplib.SMTP(host, port, timeout=timeout)


@dataclass(slots=True)
class DeliveryResult:
    accepted: bool
    message_id: str | None
    provider_response: str | None
    failure_kind: str | None = None
    error: str | None = None
    attempted_at_utc: str = ""

    @property
    def retryable(self) -> bool:
        return self.failure_kind == TRANSIENT


@dataclass(slots=True)
class MailSettings:
    host: str = "smtp.gmail.com"
    port: int = 587
    timeout_seconds: float = 30.0
    credentials_path: Path = Path("~/.config/rowanjobs/credentials.env")
    sender: str = ""
    sender_name: str = "RowanJobs"


def build_message(
    *,
    sender: str,
    sender_name: str,
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str,
    message_id: str | None = None,
) -> EmailMessage:
    """Assemble a multipart/alternative message.

    The plain-text part is written first and is a real report, not a
    placeholder: a text-only reader must get the same facts.
    """
    message = EmailMessage()
    # An empty sender is the documented default: the authenticated SMTP account
    # is used instead. Leave the header off entirely rather than emitting
    # "RowanJobs <>", which is not an address at all -- ``send`` fills it in
    # once the credential naming that account has been loaded.
    if sender:
        message["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(now_utc())
    message["Message-ID"] = message_id or make_msgid(domain="rowanjobs.local")
    # Routine status mail: keep it out of vacation responders and other
    # automatic replies, and mark it as machine-generated.
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message["X-RowanJobs-Report"] = "run-report"
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def send(
    message: EmailMessage,
    settings: MailSettings,
    *,
    smtp_factory: SmtpFactory | None = None,
) -> DeliveryResult:
    """Deliver one message. Never raises for a delivery problem."""
    attempted = utc_str()
    try:
        creds = load_credentials(Path(settings.credentials_path).expanduser())
    except CredentialError as exc:
        return DeliveryResult(
            accepted=False,
            message_id=message.get("Message-ID"),
            provider_response=None,
            failure_kind=PERMANENT,
            error=str(exc),
            attempted_at_utc=attempted,
        )

    if not message.get("From"):
        message["From"] = (
            f"{settings.sender_name} <{creds.username}>" if settings.sender_name else creds.username
        )

    context = ssl.create_default_context()
    connect = smtp_factory or _default_smtp
    try:
        with connect(settings.host, settings.port, settings.timeout_seconds) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(creds.username, creds.password)
            refused = smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        return DeliveryResult(
            accepted=False,
            message_id=message.get("Message-ID"),
            provider_response=None,
            failure_kind=PERMANENT,
            # The exception text can echo credentials back; report the code only.
            error=(
                f"SMTP authentication rejected (code {exc.smtp_code}). Check the "
                "Gmail account and that the App Password is still valid."
            ),
            attempted_at_utc=attempted,
        )
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
        return DeliveryResult(
            accepted=False,
            message_id=message.get("Message-ID"),
            provider_response=None,
            failure_kind=PERMANENT,
            error=f"{type(exc).__name__}: address refused by the provider",
            attempted_at_utc=attempted,
        )
    except smtplib.SMTPResponseException as exc:
        kind = TRANSIENT if 400 <= int(exc.smtp_code) < 500 else PERMANENT
        return DeliveryResult(
            accepted=False,
            message_id=message.get("Message-ID"),
            provider_response=f"{exc.smtp_code}",
            failure_kind=kind,
            error=f"SMTP {exc.smtp_code}",
            attempted_at_utc=attempted,
        )
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
        return DeliveryResult(
            accepted=False,
            message_id=message.get("Message-ID"),
            provider_response=None,
            failure_kind=TRANSIENT,
            error=f"{type(exc).__name__}: {exc}",
            attempted_at_utc=attempted,
        )

    if refused:
        return DeliveryResult(
            accepted=False,
            message_id=message.get("Message-ID"),
            provider_response=None,
            failure_kind=PERMANENT,
            error=f"provider refused {len(refused)} recipient(s)",
            attempted_at_utc=attempted,
        )
    return DeliveryResult(
        accepted=True,
        message_id=message.get("Message-ID"),
        # send_message returns {} on success; the meaningful fact is that the
        # submission server took every recipient.
        provider_response=f"250 accepted for delivery by {settings.host}",
        attempted_at_utc=attempted,
    )


def new_message_id() -> str:
    return make_msgid(idstring=uuid.uuid4().hex[:12], domain="rowanjobs.local")

"""SMTP credential loading.

Rules enforced here, matching the convention already used by the other mail
senders on this host:

* the file must exist, be a regular file, and be mode 0600
* the parent directory must not be group- or world-readable
* the value is never printed, logged, or included in an exception message
* it is only ever held in memory inside the sending process
* incidental spaces are stripped, because Google displays App Passwords in
  groups of four and they get pasted that way
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path


class CredentialError(RuntimeError):
    """The credential file is missing, unreadable, or insecurely stored."""


@dataclass(frozen=True, slots=True)
class SmtpCredentials:
    username: str
    password: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"SmtpCredentials(username={self.username!r}, password=<redacted>)"

    __str__ = __repr__


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def check_permissions(path: Path) -> Path:
    """Validate location and mode. Reads no secret material."""
    path = Path(path).expanduser()
    if not path.exists():
        raise CredentialError(f"no SMTP credential file at {path}")
    if not path.is_file():
        raise CredentialError(f"{path} is not a regular file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise CredentialError(
            f"{path} is mode {mode:04o}; it must be 0600 so no other account can read it"
        )
    parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
    if parent_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialError(
            f"{path.parent} is mode {parent_mode:04o}; it must not be group- or world-accessible"
        )
    return path


def load_credentials(
    path: Path,
    *,
    user_key: str = "GMAIL_SMTP_USER",
    password_key: str = "GMAIL_APP_PASSWORD",  # noqa: S107 - the key's name, not a secret
) -> SmtpCredentials:
    target = check_permissions(path)
    values = _parse_env(target.read_text(encoding="utf-8"))
    username = values.get(user_key, "").strip()
    password = values.get(password_key, "").replace(" ", "")
    if not username:
        raise CredentialError(f"{target} does not define {user_key}")
    if not password:
        raise CredentialError(f"{target} does not define {password_key}")
    return SmtpCredentials(username=username, password=password)

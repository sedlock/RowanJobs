"""systemd timer inspection.

Reports what systemd *actually* says rather than what the unit file intends: the
next activation is read back from ``systemctl --user show``, and the calendar
expression is validated with ``systemd-analyze calendar``.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING, Any

from ..timeutil import OPERATIONAL_TZ_NAME, local_str

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

TIMER_UNIT = "rowanjobs.timer"
SERVICE_UNIT = "rowanjobs.service"
RETRY_TIMER_UNIT = "rowanjobs-retry.timer"
#: Mail-only. Inspected so the health output can show when an outstanding
#: report will next be retried; it never collects.
NOTIFY_TIMER_UNIT = "rowanjobs-notify.timer"
RETRY_SERVICE_UNIT = "rowanjobs-retry.service"


def calendar_expression(cfg: Config) -> str:
    s = cfg.schedule
    return f"*-*-* {s.hour:02d}:{s.minute:02d}:00 {s.timezone}"


def _systemctl(*args: str) -> tuple[int, str, str]:
    if shutil.which("systemctl") is None:
        return 127, "", "systemctl not available"
    try:
        proc = subprocess.run(  # noqa: S603
            ["systemctl", "--user", *args],  # noqa: S607 - resolved from PATH by design
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 126, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def validate_calendar(expression: str) -> dict[str, Any]:
    if shutil.which("systemd-analyze") is None:
        return {"valid": None, "detail": "systemd-analyze not available"}
    try:
        proc = subprocess.run(  # noqa: S603
            ["systemd-analyze", "calendar", expression],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"valid": None, "detail": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"valid": False, "detail": proc.stderr.strip() or proc.stdout.strip()}
    parsed: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(":")
        if value:
            parsed[key.strip()] = value.strip()
    return {"valid": True, "detail": proc.stdout.strip(), "fields": parsed}


def _show(unit: str, properties: list[str]) -> dict[str, str]:
    code, out, _ = _systemctl("show", unit, "--property=" + ",".join(properties))
    if code != 0:
        return {}
    values: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    return values


def lingering_enabled(user: str | None = None) -> dict[str, Any]:
    import os
    from pathlib import Path

    user = user or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    marker = Path("/var/lib/systemd/linger") / user
    if marker.exists():
        return {"enabled": True, "detail": f"{marker} exists"}
    code, out, err = 0, "", ""
    if shutil.which("loginctl"):
        try:
            proc = subprocess.run(  # noqa: S603
                ["loginctl", "show-user", user, "--property=Linger"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            err = str(exc)
    if code == 0 and "Linger=yes" in out:
        return {"enabled": True, "detail": "loginctl reports Linger=yes"}
    return {
        "enabled": False,
        "detail": err.strip()
        or "lingering is not enabled; user units will not run without an active login",
    }


def timer_status(cfg: Config) -> dict[str, Any]:
    expression = calendar_expression(cfg)
    info: dict[str, Any] = {
        "configured_expression": expression,
        "timezone": OPERATIONAL_TZ_NAME,
        "calendar_validation": validate_calendar(expression),
        "lingering": lingering_enabled(),
        "units": {},
    }
    timers = _list_timers()
    for unit in (TIMER_UNIT, RETRY_TIMER_UNIT, NOTIFY_TIMER_UNIT):
        props = _show(
            unit,
            [
                "LoadState",
                "ActiveState",
                "UnitFileState",
                "Persistent",
                "TimersCalendar",
            ],
        )
        if not props or props.get("LoadState") in (None, "not-found"):
            info["units"][unit] = {"installed": False}
            continue
        raw = timers.get(unit, {})
        next_utc = _usec_to_iso(raw.get("next"))
        last_utc = _usec_to_iso(raw.get("last"))
        info["units"][unit] = {
            "installed": True,
            "load_state": props.get("LoadState"),
            "active_state": props.get("ActiveState"),
            "unit_file_state": props.get("UnitFileState"),
            "persistent": props.get("Persistent"),
            "calendar": props.get("TimersCalendar"),
            "next_activation_utc": next_utc,
            "next_activation_local": local_str(next_utc) if next_utc else None,
            "last_trigger_utc": last_utc,
            "last_trigger_local": local_str(last_utc) if last_utc else None,
        }
    primary = info["units"].get(TIMER_UNIT, {})
    info["next_collection_local"] = primary.get("next_activation_local")
    info["scheduled"] = bool(
        primary.get("installed")
        and primary.get("active_state") == "active"
        and primary.get("next_activation_utc")
    )
    return info


def _list_timers() -> dict[str, dict[str, int]]:
    """Next/last activation in microseconds, straight from systemd.

    ``systemctl show`` renders ``NextElapseUSecRealtime`` as a human-readable
    local timestamp regardless of ``--timestamp``, which is awkward to parse back
    reliably. ``list-timers --output=json`` gives the raw microsecond values.
    """
    code, out, _ = _systemctl("list-timers", "--all", "--output=json")
    if code != 0 or not out.strip():
        return {}
    import json

    try:
        entries = json.loads(out)
    except ValueError:
        return {}
    result: dict[str, dict[str, int]] = {}
    for entry in entries:
        unit = str(entry.get("unit", ""))
        if unit:
            result[unit] = {
                "next": int(entry.get("next") or 0),
                "last": int(entry.get("last") or 0),
            }
    return result


def _usec_to_iso(value: str | int | None) -> str | None:
    if not value:
        return None
    try:
        usec = int(value)
    except (TypeError, ValueError):
        return None
    if usec <= 0:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(usec / 1_000_000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

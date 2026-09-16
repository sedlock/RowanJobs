"""Operational alerting.

RowanJobs will not borrow another application's credentials or send to a
recipient nobody configured. With no destination set, notifications report as
``UNCONFIGURED`` in the health output and nothing is sent -- a visible status
rather than invented delivery.

Configuring ``[notify] kind = "command"`` with an argv list enables delivery
through whatever the operator already trusts; the alert JSON arrives on stdin.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..config import NotifyConfig


@dataclass
class NotifyResult:
    state: str
    detail: str
    sent: bool = False


class Notifier:
    def __init__(self, cfg: NotifyConfig) -> None:
        self.cfg = cfg

    @property
    def configured(self) -> bool:
        return bool(self.cfg.kind) and bool(self.cfg.command)

    def status(self) -> dict[str, Any]:
        if not self.cfg.kind:
            return {
                "state": "UNCONFIGURED",
                "detail": "no notification destination is configured for RowanJobs; "
                "set [notify] kind and command in the config file",
                "notify_on": list(self.cfg.notify_on),
            }
        if not self.cfg.command:
            return {
                "state": "UNCONFIGURED",
                "detail": f"[notify] kind is {self.cfg.kind!r} but no command was given",
                "notify_on": list(self.cfg.notify_on),
            }
        return {
            "state": "CONFIGURED",
            "detail": f"delivery via {self.cfg.kind}",
            "notify_on": list(self.cfg.notify_on),
        }

    def maybe_notify(self, outcome: str, payload: dict[str, Any]) -> NotifyResult:
        if outcome not in self.cfg.notify_on:
            return NotifyResult("SKIPPED", f"outcome {outcome!r} is not an alerting outcome")
        if not self.configured:
            return NotifyResult(
                "UNCONFIGURED",
                "an alert would have been sent, but no destination is configured",
            )
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            proc = subprocess.run(  # noqa: S603 - operator-supplied argv, no shell
                list(self.cfg.command),
                input=body,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return NotifyResult("FAILED", f"{type(exc).__name__}: {exc}")
        if proc.returncode != 0:
            return NotifyResult(
                "FAILED", f"command exited {proc.returncode}: {proc.stderr.strip()[:300]}"
            )
        return NotifyResult("SENT", "delivered", sent=True)

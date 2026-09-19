"""Startup failures: activations that never became a collection run.

``ops/onfailure.py`` writes one JSON line per failed unit activation. It is a
separate, dependency-free program on purpose -- a detector that imports the
configuration parser it is monitoring cannot report that the parser is broken.
This module is the reading half, and it lives inside the application because by
the time anyone reads it the application is working again.

The distinction this file exists to hold:

* A startup failure is **operational evidence**, not archive evidence. It says
  a scheduled activation died before opening a run row. It never implies that
  anything was observed at the source, and it is kept in ``runtime/``, well
  away from the collection tables.
* A failure is **resolved** by a later qualified collection for the same
  scheduled slot -- not by time passing, and not by the next day succeeding.
  Resolving it does not erase it: the record stays on disk, and the day it
  describes keeps whatever coverage it actually had.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..db import Database
from ..timeutil import local_str

if TYPE_CHECKING:  # pragma: no cover
    from ..paths import Layout

RECORD_NAME = "startup-failures.jsonl"
#: Most recent records to read. A handler loop cannot flood the health output.
MAX_RECORDS = 200


def record_path(layout: Layout) -> Path:
    return layout.runtime_dir / RECORD_NAME


def read_records(layout: Layout, *, limit: int = MAX_RECORDS) -> list[dict[str, Any]]:
    """Read the failure journal. A malformed line is skipped, never fatal."""
    path = record_path(layout)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _slots_with_success(db: Database) -> set[str]:
    """Slots that later got a real collection, qualified enough to count."""
    rows = db.query(
        "SELECT DISTINCT scheduled_slot_local_date AS slot FROM collection_runs "
        "WHERE outcome IN ('success','partial') AND scheduled_slot_local_date IS NOT NULL"
    )
    return {str(r["slot"]) for r in rows}


def assess(layout: Layout, db: Database) -> dict[str, Any]:
    """Which recorded startup failures still stand unanswered.

    A failure whose slot later collected successfully is reported as resolved
    and stops counting against health -- the day was covered in the end. One
    whose slot never collected is unresolved, and stays that way: no later day
    can retroactively cover it.
    """
    records = read_records(layout)
    resolved_slots = _slots_with_success(db)

    unresolved: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for record in records:
        slot = str(record.get("slot_local_date") or "")
        entry = {
            "unit": record.get("unit"),
            "slot_local_date": slot or None,
            "failed_at_utc": record.get("failed_at_utc"),
            "failed_at_local": record.get("failed_at_local")
            or local_str(record.get("failed_at_utc")),
            "result": record.get("result"),
            "exit_status": record.get("exit_status"),
            "config_error": record.get("config_error"),
        }
        (resolved if slot in resolved_slots else unresolved).append(entry)

    # One line per slot is what an operator needs; a unit that failed at 06:15
    # and again at 09:15 is one unresolved day, not two problems.
    slots = sorted({str(e["slot_local_date"]) for e in unresolved if e["slot_local_date"]})
    return {
        "recorded": len(records),
        "unresolved": unresolved,
        "unresolved_slots": slots,
        "resolved": len(resolved),
        "path": str(record_path(layout)),
        "note": (
            "a startup failure means the application exited before recording a run; "
            "no source observation occurred"
        ),
    }

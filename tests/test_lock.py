"""Single-collector mutual exclusion.

Contention is a first-class outcome: "another collector was already running" is
recorded as ``lock_contention`` and must never be reported as a source failure
or as a successful collection that happened to find nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rowanjobs.collect.lock import CollectorLock, LockContention
from rowanjobs.collect.runner import Collector
from rowanjobs.config import Config
from rowanjobs.db import Database

from .conftest import LISTING_URL, FakeSource, build_listing_page, listing_job


def test_acquiring_records_who_holds_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "collector.lock"
    lock = CollectorLock(path)
    held = lock.acquire(run_uuid="run-1234")
    try:
        holder = json.loads(path.read_text())
        assert holder["pid"] == os.getpid()
        assert holder["host"]
        assert holder["run_uuid"] == "run-1234"
        assert holder["acquired_at_utc"].endswith("Z")
        assert held.read_holder() == holder
    finally:
        lock.release()
    assert path.read_text() == ""


def test_the_context_manager_acquires_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "collector.lock"
    with CollectorLock(path) as lock:
        assert lock.holder is not None
        with pytest.raises(LockContention):
            CollectorLock(path).acquire()
    CollectorLock(path).acquire().release()


def test_a_second_holder_is_refused_with_the_first_holders_details(tmp_path: Path) -> None:
    path = tmp_path / "collector.lock"
    first = CollectorLock(path)
    first.acquire(run_uuid="run-a")
    try:
        with pytest.raises(LockContention) as excinfo:
            CollectorLock(path).acquire(run_uuid="run-b")
    finally:
        first.release()

    assert excinfo.value.path == path
    assert excinfo.value.holder is not None
    assert excinfo.value.holder["run_uuid"] == "run-a"
    assert "already held" in str(excinfo.value)


def test_the_lock_is_reusable_once_released(tmp_path: Path) -> None:
    path = tmp_path / "collector.lock"
    CollectorLock(path).acquire().release()
    second = CollectorLock(path).acquire(run_uuid="second")
    try:
        assert second.holder["run_uuid"] == "second"
    finally:
        second.release()


def test_releasing_an_unheld_lock_is_harmless(tmp_path: Path) -> None:
    CollectorLock(tmp_path / "collector.lock").release()


def test_a_truncated_or_corrupt_lock_file_does_not_hide_contention(tmp_path: Path) -> None:
    path = tmp_path / "collector.lock"
    lock = CollectorLock(path)
    lock.acquire()
    try:
        path.write_text("not json at all")
        with pytest.raises(LockContention) as excinfo:
            CollectorLock(path).acquire()
        assert excinfo.value.holder is None
    finally:
        lock.release()


def test_a_contended_run_is_not_recorded_as_a_collection_failure(
    cfg: Config, db: Database, make_client
) -> None:
    source = FakeSource()
    source.page(LISTING_URL, build_listing_page([listing_job("1001")]))
    cfg.layout.ensure()
    holder = CollectorLock(cfg.layout.lock_path).acquire(run_uuid="already-running")
    try:
        result = Collector(cfg, db=db, client=make_client(source)).run(run_kind="manual")
    finally:
        holder.release()

    assert result.outcome == "lock_contention"
    assert result.run_id is None
    assert result.run_uuid is None
    assert "already held" in str(result.detail)
    assert result.coverage["holder"]["run_uuid"] == "already-running"
    # Nothing was attempted: no run row, no fetch, no failure recorded.
    assert int(db.scalar("SELECT COUNT(*) FROM collection_runs")) == 0
    assert int(db.scalar("SELECT COUNT(*) FROM fetches")) == 0
    assert source.requests == []


def test_the_lock_is_released_after_an_ordinary_run(cfg: Config, db: Database, make_client) -> None:
    source = FakeSource()
    source.page(LISTING_URL, build_listing_page([listing_job("1001")]))
    Collector(cfg, db=db, client=make_client(source)).run(run_kind="manual")

    # A following collector can take the lock straight away.
    lock = CollectorLock(cfg.layout.lock_path).acquire()
    lock.release()

"""SQLite runtime policy, migrations and the read-only guarantee.

WAL is only enabled on a build documented as containing the WAL-reset
corruption fix; reporting paths open the archive read-only so a report can never
migrate or mutate production data.
"""

from __future__ import annotations

from pathlib import Path

import apsw
import pytest

from rowanjobs.config import Config
from rowanjobs.constants import FIELD_STATES
from rowanjobs.db import Database, ReadOnlyError, open_db, open_readonly, runtime_info
from rowanjobs.db.migrations import (
    MIGRATIONS,
    SCHEMA_VERSION,
    apply_migrations,
    current_version,
    pending,
)
from rowanjobs.db.runtime import wal_reset_bug_fixed

# ------------------------------------------------------- WAL-reset bug matrix


@pytest.mark.parametrize(
    ("version", "fixed"),
    [
        ("3.45.1", False),  # Ubuntu 24.04 system SQLite
        ("3.51.1", False),  # pysqlite3-binary
        ("3.51.2", False),  # last affected release
        ("3.51.3", True),  # the fix
        ("3.53.4", True),  # apsw's bundled amalgamation
        ("3.44.6", True),  # backport
        ("3.44.5", False),  # just below the backport
        ("3.50.7", True),  # backport
        ("3.50.6", False),  # just below the backport
    ],
)
def test_wal_reset_bug_fixed_matches_the_documented_release_history(
    version: str, fixed: bool
) -> None:
    assert wal_reset_bug_fixed(version) is fixed


def test_a_backport_does_not_bless_a_later_unfixed_feature_branch() -> None:
    # 3.45.x is newer than the 3.44.6 backport but did not receive the fix.
    assert wal_reset_bug_fixed("3.45.0") is False
    assert wal_reset_bug_fixed("3.46.9") is False
    # Everything from 3.51.3 on is fixed outright.
    assert wal_reset_bug_fixed("3.52.0") is True
    assert wal_reset_bug_fixed("4.0.0") is True


def test_runtime_info_reports_the_library_actually_loaded() -> None:
    info = runtime_info()
    assert info.provider == "apsw"
    assert info.sqlite_version == apsw.sqlite_lib_version()
    assert info.wal_safe is wal_reset_bug_fixed(info.sqlite_version)
    assert "walresetbug" in info.wal_evidence
    assert info.as_dict()["sqlite_version"] == info.sqlite_version


def test_journal_mode_follows_the_wal_safety_verdict(db: Database) -> None:
    if runtime_info().wal_safe:
        assert db.journal_mode == "wal"
        assert db.wal_deviation is None
    else:
        assert db.journal_mode == "delete"
        assert db.wal_deviation


# ------------------------------------------------------------------ migrations


def test_open_db_applies_every_migration_and_records_them(db: Database) -> None:
    assert current_version(db) == SCHEMA_VERSION == max(m[0] for m in MIGRATIONS)
    assert pending(db) == []
    names = {r[1] for r in db.conn.execute("SELECT version, name FROM schema_migrations")}
    assert names == {name for _, name, _ in MIGRATIONS}


def test_migrations_are_idempotent(db: Database) -> None:
    assert apply_migrations(db) == []
    assert current_version(db) == SCHEMA_VERSION


def test_expected_tables_and_views_exist(db: Database) -> None:
    objects = {
        str(r[0])
        for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    assert {
        "artifacts",
        "collection_runs",
        "coverage_gaps",
        "extractions",
        "fetches",
        "listing_entries",
        "listing_scan_assessments",
        "listing_scans",
        "posting_observations",
        "posting_versions",
        "postings",
        "presence_events",
        "resource_observations",
        "version_values",
        "work_queue",
    } <= objects
    assert {
        "v_posting_current",
        "v_posting_history",
        "v_qualified_scans",
        "v_run_health",
    } <= objects


def test_foreign_keys_are_enforced_on_every_connection(db: Database) -> None:
    assert int(db.conn.pragma("foreign_keys")) == 1
    with pytest.raises(apsw.ConstraintError), db.write():
        db.execute(
            "INSERT INTO listing_scans(run_id, scan_ordinal, scan_role, started_at_utc) "
            "VALUES (?,?,?,?)",
            (4242, 1, "discovery", "2026-09-16T00:00:00Z"),
        )


def test_check_constraints_reject_states_outside_the_documented_enumerations(
    db: Database,
) -> None:
    with pytest.raises(apsw.ConstraintError), db.write():
        db.execute(
            "INSERT INTO artifacts(sha256, byte_length, compression, compressed_bytes, blob, "
            "capture_state, representation, first_seen_at_utc) VALUES (?,?,?,?,?,?,?,?)",
            ("a" * 64, 1, "zlib", 1, b"x", "mostly", "http-wire-body", "2026-09-16T00:00:00Z"),
        )


def test_nested_write_transactions_are_refused(db: Database) -> None:
    with pytest.raises(RuntimeError, match="nested"), db.write(), db.write():
        pass


def test_failed_write_transaction_rolls_back(db: Database) -> None:
    with pytest.raises(ValueError, match="boom"), db.write():
        db.execute(
            "INSERT INTO sources(namespace, display_name, base_url, adapter, created_at_utc) "
            "VALUES ('rollback','x','x','x','2026-09-16T00:00:00Z')"
        )
        raise ValueError("boom")
    assert int(db.scalar("SELECT COUNT(*) FROM sources")) == 0


# ------------------------------------------------------------------ read-only


def test_open_readonly_refuses_every_write_path(cfg: Config, db: Database) -> None:
    db.close()
    ro = open_readonly(cfg.layout.db_path)
    try:
        assert ro.readonly is True
        with pytest.raises(ReadOnlyError):
            ro.execute(
                "INSERT INTO sources(namespace, display_name, base_url, adapter, "
                "created_at_utc) VALUES ('x','x','x','x','x')"
            )
        with pytest.raises(ReadOnlyError), ro.write():
            pass
        with pytest.raises(apsw.ReadOnlyError):
            ro.conn.execute("CREATE TABLE sneaky(x)")
        assert ro.query("SELECT COUNT(*) AS n FROM postings")[0]["n"] == 0
    finally:
        ro.close()


def test_open_readonly_never_creates_or_migrates_a_database(tmp_path: Path) -> None:
    missing = tmp_path / "nothing-here.db"
    with pytest.raises(FileNotFoundError):
        open_readonly(missing)
    assert not missing.exists()


def test_open_db_without_migrations_leaves_the_schema_untouched(tmp_path: Path) -> None:
    path = tmp_path / "unmigrated.db"
    handle = open_db(path, migrate=False)
    try:
        assert current_version(handle) == 0
        assert [m[0] for m in pending(handle)] == [m[0] for m in MIGRATIONS]
    finally:
        handle.close()


def test_describe_exposes_the_runtime_evidence(db: Database) -> None:
    described = db.describe()
    assert described["readonly"] is False
    assert described["runtime"]["provider"] == "apsw"
    assert described["journal_mode"] == db.journal_mode
    assert described["inspected_at_utc"].endswith("Z")


def test_integrity_and_foreign_key_checks_are_clean_on_a_fresh_archive(db: Database) -> None:
    assert db.integrity_check() == ["ok"]
    assert db.foreign_key_check() == []
    assert db.page_bytes() > 0


def test_version_values_accept_every_documented_field_state(
    db: Database, repo, cfg: Config, run_environment
) -> None:
    """``unresolved`` is reserved for a structure the parser could not validate."""
    posting_id, _created = repo.ensure_posting(
        source_id=run_environment["source_id"],
        external_job_id="1001",
        run_id=run_environment["run_id"],
        discovery_basis="baseline",
        observed_at_utc="2026-09-16T10:00:00Z",
    )
    with db.write():
        version_id = db.insert(
            "INSERT INTO posting_versions(posting_id, contract_version, text_contract_version, "
            "content_fingerprint, description_text_fingerprint, description_html_fingerprint, "
            "metadata_fingerprint, description_html_kind, first_seen_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                posting_id,
                "1.0.0",
                "1.0.0",
                "fp",
                "fp",
                "fp",
                "fp",
                "absent",
                "2026-09-16T10:00:00Z",
            ),
        )
    for ordinal, state in enumerate(FIELD_STATES):
        with db.write():
            db.execute(
                "INSERT INTO version_values(posting_version_id, field_key, ordinal, "
                "field_state, origin) VALUES (?,?,?,?,?)",
                (version_id, "location", ordinal, state, "detail_labelled"),
            )
    stored = {
        r["field_state"]
        for r in db.query(
            "SELECT field_state FROM version_values WHERE posting_version_id = ?", (version_id,)
        )
    }
    assert stored == set(FIELD_STATES) == {"present", "blank", "absent", "unresolved"}

    with pytest.raises(apsw.ConstraintError), db.write():
        db.execute(
            "INSERT INTO version_values(posting_version_id, field_key, ordinal, "
            "field_state, origin) VALUES (?,?,?,?,?)",
            (version_id, "location", 99, "probably", "detail_labelled"),
        )

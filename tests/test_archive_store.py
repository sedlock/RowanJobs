"""Content-addressed payload storage.

Identity is the SHA-256 of the *uncompressed* bytes, so changing compression can
never change what counts as the same document, and an unchanged page re-fetched
tomorrow reuses today's artifact instead of storing a second copy.
"""

from __future__ import annotations

import zlib
from hashlib import sha256

import pytest

from rowanjobs.archive import ArchiveStore
from rowanjobs.archive.store import MIN_COMPRESS_BYTES, content_hash
from rowanjobs.db import Database
from rowanjobs.net.client import FetchResult


@pytest.fixture
def store(db: Database) -> ArchiveStore:
    return ArchiveStore(db)


def put(store: ArchiveStore, data: bytes, **kwargs):
    with store.db.write():
        return store.put(
            data,
            capture_state=kwargs.pop("capture_state", "complete"),
            representation=kwargs.pop("representation", "http-wire-body"),
            **kwargs,
        )


def test_payload_is_addressed_by_the_hash_of_the_uncompressed_bytes(
    store: ArchiveStore, db: Database
) -> None:
    data = b"<html>" + b"x" * 1000 + b"</html>"
    stored = put(store, data)
    assert stored.sha256 == sha256(data).hexdigest() == content_hash(data)
    row = db.one("SELECT * FROM artifacts WHERE artifact_id = ?", (stored.artifact_id,))
    assert row["compression"] == "zlib"
    assert int(row["byte_length"]) == len(data)
    assert int(row["compressed_bytes"]) < len(data)
    assert zlib.decompress(bytes(row["blob"])) == data


def test_identical_bytes_are_stored_once_and_reported_as_deduplicated(
    store: ArchiveStore, db: Database
) -> None:
    data = b"<html>same</html>" * 40
    first = put(store, data)
    second = put(store, data)
    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.artifact_id == first.artifact_id
    assert int(db.scalar("SELECT COUNT(*) FROM artifacts")) == 1


def test_small_payloads_are_stored_uncompressed_without_changing_identity(
    store: ArchiveStore, db: Database
) -> None:
    data = b"tiny"
    stored = put(store, data)
    row = db.one("SELECT compression, blob FROM artifacts WHERE artifact_id = ?", (stored.artifact_id,))
    assert row["compression"] == "none"
    assert bytes(row["blob"]) == data
    assert stored.sha256 == content_hash(data)
    assert len(data) < MIN_COMPRESS_BYTES


def test_round_trip_returns_the_exact_bytes_archived(store: ArchiveStore) -> None:
    data = "café — “quoted”  ".encode()
    stored = put(store, data)
    assert store.get(stored.artifact_id) == data


def test_partial_capture_keeps_its_exception_on_the_artifact(
    store: ArchiveStore, db: Database
) -> None:
    stored = put(
        store,
        b"prefix only" * 40,
        capture_state="partial",
        capture_exception="response exceeded the limit",
    )
    row = db.one("SELECT * FROM artifacts WHERE artifact_id = ?", (stored.artifact_id,))
    assert row["capture_state"] == "partial"
    assert row["capture_exception"] == "response exceeded the limit"


def test_decoded_body_representation_is_recorded_as_such(
    store: ArchiveStore, db: Database
) -> None:
    stored = put(
        store,
        b"body" * 100,
        representation="http-decoded-body",
        content_encoding_removed="gzip",
        media_type="text/html",
        charset_declared="utf-8",
    )
    row = db.one("SELECT * FROM artifacts WHERE artifact_id = ?", (stored.artifact_id,))
    assert row["representation"] == "http-decoded-body"
    assert row["content_encoding_removed"] == "gzip"
    assert row["media_type"] == "text/html"
    assert row["charset_declared"] == "utf-8"


def test_verify_detects_a_tampered_payload(store: ArchiveStore, db: Database) -> None:
    stored = put(store, b"original content" * 40)
    assert store.verify(stored.artifact_id) == (True, "ok")
    with db.write():
        db.execute(
            "UPDATE artifacts SET blob = ?, compression = 'none' WHERE artifact_id = ?",
            (b"tampered", stored.artifact_id),
        )
    ok, detail = store.verify(stored.artifact_id)
    assert ok is False
    assert "decompressed to" in detail or "hash mismatch" in detail


def test_verify_all_reports_every_failure_with_its_artifact_id(
    store: ArchiveStore, db: Database
) -> None:
    good = put(store, b"good payload" * 40)
    bad = put(store, b"bad payload" * 40)
    with db.write():
        db.execute(
            "UPDATE artifacts SET blob = ?, compression = 'none', byte_length = 7 "
            "WHERE artifact_id = ?",
            (b"garbage", bad.artifact_id),
        )
    report = store.verify_all()
    assert report["checked"] == 2
    assert report["ok"] is False
    assert [f["artifact_id"] for f in report["failures"]] == [bad.artifact_id]
    assert store.verify(good.artifact_id)[0] is True


def test_missing_artifact_is_an_explicit_error_not_an_empty_payload(store: ArchiveStore) -> None:
    with pytest.raises(KeyError):
        store.get(9999)
    assert store.verify(9999) == (False, "missing")


def test_put_fetch_archives_the_payload_with_its_retrieval_metadata(
    store: ArchiveStore, db: Database
) -> None:
    result = FetchResult(
        purpose="listing_page",
        requested_url="https://jobs.rowan.edu/en-us/listing/",
        body=b"<html>listing</html>" * 20,
        capture_state="complete",
        representation="http-decoded-body",
        content_encoding_removed="gzip",
        media_type="text/html",
        charset_declared="utf-8",
        declared_content_length=400,
    )
    with db.write():
        stored = store.put_fetch(result, run_id=None)
    assert stored is not None
    row = db.one("SELECT * FROM artifacts WHERE artifact_id = ?", (stored.artifact_id,))
    assert row["media_type"] == "text/html"
    assert int(row["declared_content_length"]) == 400
    assert store.get(stored.artifact_id) == result.body


def test_put_fetch_stores_nothing_when_there_was_no_body(store: ArchiveStore, db: Database) -> None:
    result = FetchResult(purpose="probe", requested_url="https://jobs.rowan.edu/", body=None)
    with db.write():
        assert store.put_fetch(result, run_id=None) is None
    assert int(db.scalar("SELECT COUNT(*) FROM artifacts")) == 0


def test_stats_report_stored_and_uncompressed_sizes(store: ArchiveStore) -> None:
    data = b"compressible " * 200
    put(store, data)
    stats = store.stats()
    assert stats["artifacts"] == 1
    assert stats["uncompressed_bytes"] == len(data)
    assert 0 < stats["compressed_bytes"] < stats["uncompressed_bytes"]

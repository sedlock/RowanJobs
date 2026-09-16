"""Content-addressed payload storage.

Payloads live inside the SQLite database, compressed, keyed by the SHA-256 of
the **uncompressed** archived bytes. Hashing before compression means switching
compression method or level later never changes content identity, and an
unchanged page re-fetched tomorrow reuses today's artifact instead of storing a
second copy.

Nothing here decodes or parses. Archive first, parse second.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from ..timeutil import utc_str

if TYPE_CHECKING:  # pragma: no cover
    from ..db import Database
    from ..net.client import FetchResult

COMPRESSION = "zlib"
COMPRESSION_LEVEL = 9
# Below this, compression usually costs more than it saves.
MIN_COMPRESS_BYTES = 256


def content_hash(data: bytes) -> str:
    return sha256(data).hexdigest()


@dataclass(slots=True)
class StoredArtifact:
    artifact_id: int
    sha256: str
    byte_length: int
    compressed_bytes: int
    deduplicated: bool
    capture_state: str


class ArchiveStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ write

    def put(
        self,
        data: bytes,
        *,
        capture_state: str,
        representation: str,
        run_id: int | None = None,
        media_type: str | None = None,
        charset_declared: str | None = None,
        content_encoding_removed: str | None = None,
        declared_content_length: int | None = None,
        capture_exception: str | None = None,
    ) -> StoredArtifact:
        """Store ``data``, reusing an existing artifact when the hash matches.

        Must be called inside a write transaction owned by the caller.
        """
        digest = content_hash(data)
        existing = self.db.one(
            "SELECT artifact_id, byte_length, compressed_bytes, capture_state "
            "FROM artifacts WHERE sha256 = ?",
            (digest,),
        )
        if existing:
            return StoredArtifact(
                artifact_id=int(existing["artifact_id"]),
                sha256=digest,
                byte_length=int(existing["byte_length"]),
                compressed_bytes=int(existing["compressed_bytes"]),
                deduplicated=True,
                capture_state=str(existing["capture_state"]),
            )

        if len(data) >= MIN_COMPRESS_BYTES:
            blob = zlib.compress(data, COMPRESSION_LEVEL)
            compression = COMPRESSION
        else:
            blob = data
            compression = "none"

        artifact_id = self.db.insert(
            """
            INSERT INTO artifacts(
                sha256, byte_length, compression, compressed_bytes, blob,
                capture_state, capture_exception, representation,
                content_encoding_removed, declared_content_length,
                media_type, charset_declared, first_seen_at_utc, first_run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                digest,
                len(data),
                compression,
                len(blob),
                blob,
                capture_state,
                capture_exception,
                representation,
                content_encoding_removed,
                declared_content_length,
                media_type,
                charset_declared,
                utc_str(),
                run_id,
            ),
        )
        return StoredArtifact(
            artifact_id=artifact_id,
            sha256=digest,
            byte_length=len(data),
            compressed_bytes=len(blob),
            deduplicated=False,
            capture_state=capture_state,
        )

    def put_fetch(self, result: FetchResult, run_id: int | None = None) -> StoredArtifact | None:
        """Archive the payload of a fetch, if it produced one."""
        if result.body is None:
            return None
        return self.put(
            result.body,
            capture_state=result.capture_state,
            representation=result.representation,
            run_id=run_id,
            media_type=result.media_type,
            charset_declared=result.charset_declared,
            content_encoding_removed=result.content_encoding_removed,
            declared_content_length=result.declared_content_length,
            capture_exception=result.capture_exception,
        )

    # ------------------------------------------------------------------- read

    def get(self, artifact_id: int) -> bytes:
        row = self.db.one(
            "SELECT blob, compression, byte_length, sha256 FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        )
        if row is None:
            raise KeyError(f"no artifact {artifact_id}")
        blob = bytes(row["blob"])
        data = zlib.decompress(blob) if row["compression"] == "zlib" else blob
        if len(data) != int(row["byte_length"]):
            raise ValueError(
                f"artifact {artifact_id} decompressed to {len(data)} bytes, "
                f"expected {row['byte_length']}"
            )
        return data

    def verify(self, artifact_id: int) -> tuple[bool, str]:
        row = self.db.one("SELECT sha256 FROM artifacts WHERE artifact_id = ?", (artifact_id,))
        if row is None:
            return False, "missing"
        try:
            data = self.get(artifact_id)
        except Exception as exc:  # noqa: BLE001
            return False, f"decompression failed: {exc}"
        actual = content_hash(data)
        if actual != row["sha256"]:
            return False, f"hash mismatch: stored {row['sha256']}, computed {actual}"
        return True, "ok"

    def verify_all(self, limit: int | None = None) -> dict[str, object]:
        sql = "SELECT artifact_id FROM artifacts ORDER BY artifact_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        failures: list[dict[str, object]] = []
        checked = 0
        for row in self.db.query(sql):
            checked += 1
            ok, detail = self.verify(int(row["artifact_id"]))
            if not ok:
                failures.append({"artifact_id": int(row["artifact_id"]), "detail": detail})
        return {"checked": checked, "failures": failures, "ok": not failures}

    def stats(self) -> dict[str, int]:
        row = self.db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(byte_length),0) AS raw, "
            "COALESCE(SUM(compressed_bytes),0) AS comp FROM artifacts"
        )
        assert row is not None
        return {
            "artifacts": int(row["n"]),
            "uncompressed_bytes": int(row["raw"]),
            "compressed_bytes": int(row["comp"]),
        }

"""Isolated ambient transcript store — a SEPARATE SQLite DB (never genesis.db).

Rolling, short-TTL quarantine for the ambient firehose. Nothing here is "memory";
graduation into Genesis is a separate, deferred design. Sync methods (sub-ms writes);
the async pipeline wraps calls in ``asyncio.to_thread`` to keep the event loop free.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ambient_transcripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,                 -- ISO8601 UTC, utterance end
    text          TEXT NOT NULL,
    duration_s    REAL,
    -- window-prefixed diarization label, e.g. 'w3:2/4' = window 3, cluster 2 of 4
    -- speakers found (NULL until diarized). The window prefix makes explicit that
    -- cluster ids are NOT comparable across windows OR across `source` connections
    -- (no cross-time / cross-room speaker identity in Stage-1). The /total suffix
    -- distinguishes a confirmed single speaker (/1) from one cluster among many.
    speaker_label TEXT,
    provenance    TEXT NOT NULL DEFAULT 'ambient_overheard',
    source        TEXT,                          -- connection/device id
    meta          TEXT                           -- JSON: asr extras, etc.
);
CREATE INDEX IF NOT EXISTS idx_ambient_ts ON ambient_transcripts(ts);
"""


class AmbientStore:
    def __init__(self, db_path: str) -> None:
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        # Stage-A: is_user column (NULL until speaker-id verifies). Idempotent ALTER so an
        # EXISTING db (created before this column) gains it — CREATE TABLE IF NOT EXISTS
        # would not add it. is_user: 1=enrolled user, 0=other, NULL=no verdict yet.
        # speaker_name: best-matching enrolled identity (NULL = no confident match / no
        # verdict). DISTINCT from speaker_label (the diar cluster tag wN:c/total). is_user is
        # derived (speaker_name == the configured user name) but kept as its own column so the
        # user-only graduation gate stays a cheap indexed lookup.
        for col, typ in (("is_user", "INTEGER"), ("speaker_name", "TEXT")):
            try:
                self._db.execute(f"ALTER TABLE ambient_transcripts ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self._db.commit()
        self._lock = threading.Lock()

    def insert(
        self, *, text: str, duration_s: float, source: str,
        meta: dict | None = None, ts: str | None = None,
    ) -> int:
        ts = ts or datetime.now(UTC).isoformat()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO ambient_transcripts (ts, text, duration_s, source, meta) "
                "VALUES (?,?,?,?,?)",
                (ts, text, duration_s, source, json.dumps(meta) if meta else None),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def set_speaker_label(self, row_id: int, label: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE ambient_transcripts SET speaker_label=? WHERE id=?",
                (label, row_id),
            )
            self._db.commit()

    def set_identity(self, row_id: int, *, speaker_name: str | None, is_user: bool, method: str) -> None:
        """Record the speaker-id verdict: ``speaker_name`` (best-matching enrolled name, or
        NULL for no confident match), ``is_user`` (1=enrolled user / 0=other), and how it was
        derived (``method``: 'direct' per-utterance, or 'cluster' centroid) in meta JSON, so a
        later graduation step can weight cluster verdicts lower. Merges into any existing meta
        rather than clobbering it."""
        with self._lock:
            row = self._db.execute(
                "SELECT meta FROM ambient_transcripts WHERE id=?", (row_id,)
            ).fetchone()
            meta: dict = {}
            if row and row[0]:
                try:
                    meta = json.loads(row[0])
                except (ValueError, TypeError):
                    meta = {}
            meta["is_user_method"] = method
            self._db.execute(
                "UPDATE ambient_transcripts SET is_user=?, speaker_name=?, meta=? WHERE id=?",
                (1 if is_user else 0, speaker_name, json.dumps(meta), row_id),
            )
            self._db.commit()

    def purge(self, ttl_hours: float, row_ceiling: int) -> tuple[int, int]:
        """Delete rows older than the TTL, AND enforce a hard row ceiling
        (protects against unbounded growth if the VAD threshold is misconfigured
        and ingest outpaces the TTL purge). Returns (ttl_deleted, ceiling_deleted).
        """
        cutoff = (datetime.now(UTC) - timedelta(hours=ttl_hours)).isoformat()
        with self._lock:
            ttl_deleted = self._db.execute(
                "DELETE FROM ambient_transcripts WHERE ts < ?", (cutoff,)
            ).rowcount
            self._db.commit()
            total = self._db.execute("SELECT COUNT(*) FROM ambient_transcripts").fetchone()[0]
            ceiling_deleted = 0
            if total > row_ceiling:
                # delete oldest down to 80% of the ceiling
                target = int(row_ceiling * 0.8)
                to_delete = total - target
                self._db.execute(
                    "DELETE FROM ambient_transcripts WHERE id IN ("
                    "SELECT id FROM ambient_transcripts ORDER BY ts ASC LIMIT ?)",
                    (to_delete,),
                )
                self._db.commit()
                ceiling_deleted = to_delete
        return ttl_deleted, ceiling_deleted

    def stats(self) -> dict:
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM ambient_transcripts").fetchone()[0]
            last_ts = self._db.execute(
                "SELECT MAX(ts) FROM ambient_transcripts"
            ).fetchone()[0]
            hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            last_hour = self._db.execute(
                "SELECT COUNT(*) FROM ambient_transcripts WHERE ts > ?", (hour_ago,)
            ).fetchone()[0]
        return {"total_rows": total, "last_ts": last_ts, "rows_last_hour": last_hour}

    def close(self) -> None:
        with self._lock:
            self._db.close()

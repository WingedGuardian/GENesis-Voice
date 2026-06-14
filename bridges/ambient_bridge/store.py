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
    -- window-prefixed diarization label, e.g. 'w3:2' (NULL until diarized).
    -- The window prefix makes explicit that cluster ids are NOT comparable
    -- across windows (no cross-time speaker identity in Stage-1).
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

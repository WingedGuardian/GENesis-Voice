"""Receiver tests for the OMI aiohttp ingest server (aiohttp TestClient, in-loop).

Covers the auth matrix, the never-5xx drop policy, segment-id dedup, the no-`message`-key
rule (a returned `message`>5 chars would push-notify the phone), oversize 413, and that rows
land in the SHARED ambient store with source=omi-<uid> and the default provenance.
"""
import sqlite3

import pytest
from aiohttp.test_utils import TestClient, TestServer

from omi_bridge.config import OmiConfig
from omi_bridge.server import OmiServer

TOKEN = "secret-tok-123"
UID = "test-omi-uid"


def _cfg(tmp_path, **over):
    base = dict(
        secret_token=TOKEN,
        uid_allowlist=(UID,),
        db_path=str(tmp_path / "ambient.db"),
        state_db_path=str(tmp_path / "omi_state.db"),
        health_path=str(tmp_path / "omi_health.json"),
    )
    base.update(over)
    return OmiConfig(**base)


def _seg(**over):
    seg = {
        "id": "seg-1",
        "text": "hello there world",
        "speaker": "SPEAKER_0",
        "speaker_id": 0,
        "is_user": True,
        "person_id": None,
        "start": 1.0,
        "end": 2.0,
        "translations": [],
        "speech_profile_processed": True,
        "stt_provider": None,
    }
    seg.update(over)
    return seg


def _rows(cfg):
    con = sqlite3.connect(cfg.db_path)
    try:
        return con.execute(
            "SELECT text, source, provenance, is_user, meta FROM ambient_transcripts ORDER BY id"
        ).fetchall()
    finally:
        con.close()


async def _client(server):
    return TestClient(TestServer(server.build_app()))


@pytest.mark.asyncio
async def test_happy_path_inserts_row_no_message_key(tmp_path):
    cfg = _cfg(tmp_path)
    server = OmiServer(cfg)
    try:
        async with await _client(server) as c:
            resp = await c.post(
                f"/omi/{TOKEN}/ingest", params={"uid": UID},
                json={"segments": [_seg()], "session_id": UID},
            )
            assert resp.status == 200
            body = await resp.json()
            assert "message" not in body  # a message>5 chars would push-notify the phone
            assert body["accepted"] == 1
        rows = _rows(cfg)
        assert len(rows) == 1
        text, source, provenance, is_user, meta = rows[0]
        assert text == "hello there world"
        assert source == f"omi-{UID}"
        assert provenance == "ambient_overheard"  # shared provenance -> visible to the engine scan
        assert is_user == 1  # applied via set_identity
        assert '"audio"' not in meta and '"omi"' in meta
    finally:
        server.close()


@pytest.mark.asyncio
async def test_bad_token_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    server = OmiServer(cfg)
    try:
        async with await _client(server) as c:
            resp = await c.post(
                "/omi/wrong-token/ingest", params={"uid": UID},
                json={"segments": [_seg()], "session_id": UID},
            )
            assert resp.status == 403
        assert _rows(cfg) == []
    finally:
        server.close()


@pytest.mark.asyncio
async def test_previous_token_accepted_for_rotation(tmp_path):
    cfg = _cfg(tmp_path, secret_token_previous="old-tok")
    server = OmiServer(cfg)
    try:
        async with await _client(server) as c:
            resp = await c.post(
                "/omi/old-tok/ingest", params={"uid": UID},
                json={"segments": [_seg()], "session_id": UID},
            )
            assert resp.status == 200
    finally:
        server.close()


@pytest.mark.asyncio
async def test_uid_not_allowed_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    server = OmiServer(cfg)
    try:
        async with await _client(server) as c:
            resp = await c.post(
                f"/omi/{TOKEN}/ingest", params={"uid": "stranger"},
                json={"segments": [_seg()], "session_id": "stranger"},
            )
            assert resp.status == 403
        assert _rows(cfg) == []
    finally:
        server.close()


@pytest.mark.asyncio
async def test_non_json_body_400(tmp_path):
    cfg = _cfg(tmp_path)
    server = OmiServer(cfg)
    try:
        async with await _client(server) as c:
            resp = await c.post(
                f"/omi/{TOKEN}/ingest", params={"uid": UID},
                data=b"{ not valid json", headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
    finally:
        server.close()


@pytest.mark.asyncio
async def test_dedup_by_segment_id(tmp_path):
    cfg = _cfg(tmp_path)
    server = OmiServer(cfg)
    try:
        async with await _client(server) as c:
            body = {"segments": [_seg(id="dup")], "session_id": UID}
            r1 = await c.post(f"/omi/{TOKEN}/ingest", params={"uid": UID}, json=body)
            r2 = await c.post(f"/omi/{TOKEN}/ingest", params={"uid": UID}, json=body)
            assert (await r1.json())["accepted"] == 1
            assert (await r2.json())["accepted"] == 0  # same segment id -> deduped
        assert len(_rows(cfg)) == 1
    finally:
        server.close()


@pytest.mark.asyncio
async def test_never_5xx_on_store_error(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    server = OmiServer(cfg)

    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(server._store, "insert", boom)
    try:
        async with await _client(server) as c:
            resp = await c.post(
                f"/omi/{TOKEN}/ingest", params={"uid": UID},
                json={"segments": [_seg()], "session_id": UID},
            )
            # A 5xx would trip OMI's retry/circuit-breaker/consecutive-failure disable.
            assert resp.status == 200
            assert (await resp.json())["accepted"] == 0
    finally:
        server.close()


@pytest.mark.asyncio
async def test_empty_text_segment_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    server = OmiServer(cfg)
    try:
        async with await _client(server) as c:
            resp = await c.post(
                f"/omi/{TOKEN}/ingest", params={"uid": UID},
                json={"segments": [_seg(text="   ")], "session_id": UID},
            )
            assert (await resp.json())["accepted"] == 0
        assert _rows(cfg) == []
    finally:
        server.close()


@pytest.mark.asyncio
async def test_oversize_body_413(tmp_path):
    cfg = _cfg(tmp_path, max_body_bytes=50)
    server = OmiServer(cfg)
    try:
        async with await _client(server) as c:
            big = {"segments": [_seg(text="x" * 500)], "session_id": UID}
            resp = await c.post(f"/omi/{TOKEN}/ingest", params={"uid": UID}, json=big)
            assert resp.status == 413
    finally:
        server.close()


@pytest.mark.asyncio
async def test_health_file_written(tmp_path):
    cfg = _cfg(tmp_path)
    server = OmiServer(cfg)
    try:
        server._write_health()
        import json

        data = json.loads((tmp_path / "omi_health.json").read_text())
        assert "total_rows" in data and "received" in data
    finally:
        server.close()

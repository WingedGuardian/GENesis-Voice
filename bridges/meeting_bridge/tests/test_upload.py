"""Phone segment upload: the receipt must never claim bytes the edge does not hold."""

import hashlib
import json
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

from meeting_bridge import upload_store
from meeting_bridge.config import MeetingConfig
from meeting_bridge.server import MeetingServer

TOKEN = "meet-secret-123"
NAME = "seg_20261021T182322_0f8fad5b-d9cb-469f-a165-70867728950e_s0"


def _cfg(tmp_path, **over):
    base = dict(
        ingest_token=TOKEN,
        output_dir=str(tmp_path / "sessions"),
        health_path=str(tmp_path / "health.json"),
        upload_min_free_bytes=0,
    )
    base.update(over)
    return MeetingConfig(**base)


def _frame(payload: int) -> bytes:
    length = payload + 7
    header = bytes([0xFF, 0xF1, 0x60, 0x40 | (length >> 11), (length >> 3) & 0xFF, ((length & 7) << 5) | 0x1F, 0xFC])
    return header + bytes([7]) * payload


AUDIO = _frame(200) + _frame(180) + _frame(220)


def _q(data: bytes, name: str = NAME, sha: str | None = None, size: int | None = None) -> str:
    sha = sha or hashlib.sha256(data).hexdigest()
    return f"name={name}&sha256={sha}&bytes={size if size is not None else len(data)}"


async def _client(cfg):
    return TestClient(TestServer(MeetingServer(cfg).build_app()))


@pytest.mark.asyncio
async def test_upload_stores_and_returns_the_reread_hash(tmp_path):
    cfg = _cfg(tmp_path)
    async with await _client(cfg) as c:
        meta = json.dumps({"run_uuid": "x", "start_sample": 0})
        r = await c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO)}&meta={meta}", data=AUDIO)
        body = await r.json()
    assert r.status == 200
    assert body == {
        "status": "stored",
        "sha256": hashlib.sha256(AUDIO).hexdigest(),
        "bytes": len(AUDIO),
        "created": True,
    }
    final = upload_store.final_path(cfg.upload_root, NAME)
    assert open(final, "rb").read() == AUDIO
    assert json.load(open(final[:-4] + ".json"))["start_sample"] == 0
    assert final.startswith(os.path.join(cfg.upload_root, "2026-10-21"))
    assert not [f for f in os.listdir(os.path.dirname(final)) if ".tmp-" in f]


@pytest.mark.asyncio
async def test_identical_reupload_is_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    async with await _client(cfg) as c:
        await c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO)}", data=AUDIO)
        r = await c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO)}", data=AUDIO)
        body = await r.json()
    assert r.status == 200 and body["status"] == "stored" and body["created"] is False


@pytest.mark.asyncio
async def test_different_bytes_under_a_held_name_are_refused_not_overwritten(tmp_path):
    cfg = _cfg(tmp_path)
    other = _frame(90) + _frame(91)
    async with await _client(cfg) as c:
        await c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO)}", data=AUDIO)
        r = await c.post(f"/meeting/{TOKEN}/upload?{_q(other)}", data=other)
    assert r.status == 409
    assert open(upload_store.final_path(cfg.upload_root, NAME), "rb").read() == AUDIO


@pytest.mark.asyncio
async def test_hash_mismatch_is_rejected_and_nothing_is_kept(tmp_path):
    cfg = _cfg(tmp_path)
    async with await _client(cfg) as c:
        r = await c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO, sha='0' * 64)}", data=AUDIO)
        body = await r.json()
    assert r.status == 422 and body["status"] == "rejected"
    assert not os.path.exists(upload_store.final_path(cfg.upload_root, NAME))


@pytest.mark.asyncio
async def test_truncated_body_is_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    async with await _client(cfg) as c:
        r = await c.post(
            f"/meeting/{TOKEN}/upload?{_q(AUDIO)}", data=AUDIO[:-50]
        )  # declared length/hash of the whole file, fewer bytes arrive
    assert r.status == 422


@pytest.mark.asyncio
async def test_non_audio_is_rejected(tmp_path):
    junk = b"not audio at all" * 20
    async with await _client(_cfg(tmp_path)) as c:
        r = await c.post(f"/meeting/{TOKEN}/upload?{_q(junk)}", data=junk)
    assert r.status == 422


@pytest.mark.asyncio
async def test_torn_last_frame_from_a_killed_phone_is_accepted(tmp_path):
    torn = AUDIO[:-30]
    async with await _client(_cfg(tmp_path)) as c:
        r = await c.post(f"/meeting/{TOKEN}/upload?{_q(torn)}", data=torn)
    assert r.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "seg_20261021T182322_r1_s0",
        NAME + "/x",
        "seg_2026102XT182322_0f8fad5b-d9cb-469f-a165-70867728950e_s0",
    ],
)
async def test_bad_names_are_rejected_before_touching_disk(tmp_path, name):
    cfg = _cfg(tmp_path)
    async with await _client(cfg) as c:
        r = await c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO, name=name)}", data=AUDIO)
    assert r.status == 400
    assert not os.path.exists(cfg.upload_root) or not os.listdir(cfg.upload_root)


@pytest.mark.asyncio
async def test_oversize_is_rejected(tmp_path):
    async with await _client(_cfg(tmp_path, upload_max_bytes=100)) as c:
        r = await c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO)}", data=AUDIO)
    assert r.status == 413


@pytest.mark.asyncio
async def test_body_longer_than_declared_cannot_slip_past_the_cap(tmp_path):
    big = AUDIO * 3
    async with await _client(_cfg(tmp_path, upload_max_bytes=len(AUDIO) + 10)) as c:
        r = await c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO)}", data=big)
    assert r.status in (413, 422)


@pytest.mark.asyncio
async def test_low_disk_refuses_with_507(tmp_path):
    async with await _client(_cfg(tmp_path, upload_min_free_bytes=1 << 62)) as c:
        r = await c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO)}", data=AUDIO)
    assert r.status == 507


@pytest.mark.asyncio
async def test_bad_token_is_forbidden(tmp_path):
    async with await _client(_cfg(tmp_path)) as c:
        r = await c.post(f"/meeting/wrong/upload?{_q(AUDIO)}", data=AUDIO)
        h = await c.get(f"/meeting/wrong/have?name={NAME}&sha256={'0' * 64}")
    assert r.status == 403 and h.status == 403


@pytest.mark.asyncio
async def test_have_rehashes_from_disk(tmp_path):
    cfg = _cfg(tmp_path)
    sha = hashlib.sha256(AUDIO).hexdigest()
    async with await _client(cfg) as c:
        await c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO)}", data=AUDIO)
        ok = await (await c.get(f"/meeting/{TOKEN}/have?name={NAME}&sha256={sha}")).json()
        # Damage the stored file after the receipt: the phone must be told it is no longer held.
        with open(upload_store.final_path(cfg.upload_root, NAME), "r+b") as f:
            f.seek(20)
            f.write(b"\x00\x00")
        damaged = await (await c.get(f"/meeting/{TOKEN}/have?name={NAME}&sha256={sha}")).json()
        missing = await (await c.get(f"/meeting/{TOKEN}/have?name={NAME.replace('_s0', '_s9')}&sha256={sha}")).json()
    assert ok["have"] is True
    assert damaged["have"] is False
    assert missing["have"] is False


def test_sweep_removes_leftover_temp_files(tmp_path):
    day = tmp_path / "2026-10-21"
    day.mkdir()
    (day / "x.aac.tmp-abc").write_bytes(b"1")
    (day / "keep.aac").write_bytes(b"1")
    assert upload_store.sweep_temps(str(tmp_path)) == 1
    assert (day / "keep.aac").exists()


def test_adts_check():
    assert upload_store.adts_ok(AUDIO)
    assert upload_store.adts_ok(AUDIO[:-5])
    assert not upload_store.adts_ok(b"")
    assert not upload_store.adts_ok(AUDIO + b"\x00" * 20)  # garbage after the frames


@pytest.mark.asyncio
async def test_two_identical_uploads_racing_store_exactly_once(tmp_path):
    import asyncio

    cfg = _cfg(tmp_path)
    async with await _client(cfg) as c:
        r1, r2 = await asyncio.gather(
            c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO)}", data=AUDIO),
            c.post(f"/meeting/{TOKEN}/upload?{_q(AUDIO)}", data=AUDIO),
        )
        bodies = [await r1.json(), await r2.json()]
    assert {r1.status, r2.status} == {200}
    assert sorted(b["created"] for b in bodies) == [False, True]
    day = os.path.dirname(upload_store.final_path(cfg.upload_root, NAME))
    assert sorted(os.listdir(day)) == [NAME + ".aac"]


@pytest.mark.parametrize(
    "name",
    [
        NAME + "\n",  # trailing newline must not pass a full match
        "seg_20261399T182322_0f8fad5b-d9cb-469f-a165-70867728950e_s0",  # impossible date
        "seg_٢٠٢٦١٠٢١T182322_0f8fad5b-d9cb-469f-a165-70867728950e_s0",
    ],
)
def test_name_validation_is_strict(name):
    assert not upload_store.valid_name(name)


def test_sha_validation_is_strict():
    assert upload_store.valid_sha("a" * 64)
    assert not upload_store.valid_sha("a" * 64 + "\n")


def test_failure_after_link_leaves_no_final_file_and_a_retry_succeeds(tmp_path, monkeypatch):
    sha = hashlib.sha256(AUDIO).hexdigest()
    real = upload_store.file_sha256
    monkeypatch.setattr(upload_store, "file_sha256", lambda p: ("0" * 64, len(AUDIO)))
    with pytest.raises(upload_store.UploadError) as ei:
        upload_store.store(str(tmp_path), NAME, AUDIO, sha256=sha, size=len(AUDIO), sidecar=None)
    assert ei.value.status == 500
    assert not os.path.exists(upload_store.final_path(str(tmp_path), NAME))
    monkeypatch.setattr(upload_store, "file_sha256", real)
    ok = upload_store.store(str(tmp_path), NAME, AUDIO, sha256=sha, size=len(AUDIO), sidecar=None)
    assert ok.created is True


def test_disk_full_maps_to_retryable_507_and_leaves_nothing(tmp_path, monkeypatch):
    import errno as _errno

    def full(*_a, **_k):
        raise OSError(_errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(upload_store.os, "link", full)
    sha = hashlib.sha256(AUDIO).hexdigest()
    with pytest.raises(upload_store.UploadError) as ei:
        upload_store.store(str(tmp_path), NAME, AUDIO, sha256=sha, size=len(AUDIO), sidecar=None)
    assert ei.value.status == 507
    day = os.path.dirname(upload_store.final_path(str(tmp_path), NAME))
    assert [f for f in os.listdir(day) if f.endswith(".aac") or ".tmp-" in f] == []


def test_sidecar_missing_on_an_identical_reupload_is_written(tmp_path):
    sha = hashlib.sha256(AUDIO).hexdigest()
    upload_store.store(str(tmp_path), NAME, AUDIO, sha256=sha, size=len(AUDIO), sidecar=None)
    again = upload_store.store(str(tmp_path), NAME, AUDIO, sha256=sha, size=len(AUDIO), sidecar={"v": 1})
    assert again.created is False
    side = upload_store.final_path(str(tmp_path), NAME)[:-4] + ".json"
    assert json.load(open(side)) == {"v": 1}

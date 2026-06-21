"""Unit tests for the ambient store's identity columns (is_user + speaker_name) + set_identity."""
import json
import sqlite3

import store


def _cols(db_path: str) -> list[str]:
    return [r[1] for r in sqlite3.connect(db_path).execute(
        "PRAGMA table_info(ambient_transcripts)").fetchall()]


def test_alter_is_idempotent(tmp_path):
    db = str(tmp_path / "a.db")
    store.AmbientStore(db).close()
    # A second init must NOT raise on the ALTER (columns already present).
    store.AmbientStore(db).close()
    cols = _cols(db)
    assert "is_user" in cols
    assert "speaker_name" in cols


def test_is_user_defaults_null(tmp_path):
    db = str(tmp_path / "a.db")
    s = store.AmbientStore(db)
    rid = s.insert(text="hi", duration_s=2.0, source="t")
    s.close()
    row = sqlite3.connect(db).execute(
        "SELECT is_user FROM ambient_transcripts WHERE id=?", (rid,)).fetchone()
    assert row[0] is None


def test_set_identity_user_direct_preserves_meta(tmp_path):
    db = str(tmp_path / "a.db")
    s = store.AmbientStore(db)
    rid = s.insert(text="hello", duration_s=4.0, source="t", meta={"asr": "sherpa"})
    s.set_identity(rid, speaker_name="user", is_user=True, method="direct")
    s.close()
    row = sqlite3.connect(db).execute(
        "SELECT is_user, speaker_name, meta FROM ambient_transcripts WHERE id=?", (rid,)).fetchone()
    assert row[0] == 1
    assert row[1] == "user"
    meta = json.loads(row[2])
    assert meta["is_user_method"] == "direct"
    assert meta["asr"] == "sherpa"  # existing meta preserved (merge, not clobber)


def test_set_identity_unknown_cluster_no_prior_meta(tmp_path):
    db = str(tmp_path / "a.db")
    s = store.AmbientStore(db)
    rid = s.insert(text="ok", duration_s=1.2, source="t")
    # no confident match → speaker_name NULL, is_user 0
    s.set_identity(rid, speaker_name=None, is_user=False, method="cluster")
    s.close()
    row = sqlite3.connect(db).execute(
        "SELECT is_user, speaker_name, meta FROM ambient_transcripts WHERE id=?", (rid,)).fetchone()
    assert row[0] == 0
    assert row[1] is None
    assert json.loads(row[2])["is_user_method"] == "cluster"

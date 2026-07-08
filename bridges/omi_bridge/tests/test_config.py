"""Unit tests for OmiConfig env parsing + auth helpers."""
from omi_bridge.config import OmiConfig, load_config


def _clean(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("OMI_"):
            monkeypatch.delenv(k, raising=False)


def test_defaults(monkeypatch):
    _clean(monkeypatch)
    c = OmiConfig()
    # Binds loopback by default: only the Tailscale Funnel (localhost) should reach it,
    # never the LAN directly.
    assert c.host == "127.0.0.1"
    assert isinstance(c.port, int)
    assert c.ttl_hours == 48.0
    assert c.row_ceiling == 200000
    assert c.anchor_tolerance_s == 60.0
    assert c.db_path.endswith("ambient.db")  # SHARED substrate with the ambient bridge
    assert c.state_db_path.endswith("omi_state.db")
    assert c.uid_allowlist == ()
    assert c.secret_token == ""
    assert c.secret_token_previous == ""


def test_uid_allowlist_parsing(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("OMI_UID_ALLOWLIST", " a , b,c ,")
    c = OmiConfig()
    assert c.uid_allowlist == ("a", "b", "c")  # trimmed, empties dropped


def test_token_candidates_drops_empty(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("OMI_INGEST_SECRET_TOKEN", "cur")
    c = OmiConfig()
    assert c.token_candidates() == ("cur",)  # previous empty -> only current
    monkeypatch.setenv("OMI_INGEST_SECRET_TOKEN_PREVIOUS", "old")
    c2 = OmiConfig()
    assert c2.token_candidates() == ("cur", "old")


def test_token_candidates_empty_when_unset(monkeypatch):
    _clean(monkeypatch)
    assert OmiConfig().token_candidates() == ()  # no token configured -> nothing accepts


def test_uid_allowed_empty_allowlist_is_open(monkeypatch):
    _clean(monkeypatch)
    c = OmiConfig()
    # Empty allowlist => uid check skipped (token is the primary gate); documented behaviour.
    assert c.uid_allowed("anything") is True


def test_uid_allowed_enforced_when_set(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("OMI_UID_ALLOWLIST", "good-uid")
    c = OmiConfig()
    assert c.uid_allowed("good-uid") is True
    assert c.uid_allowed("bad-uid") is False


def test_env_overrides(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("OMI_TTL_HOURS", "24")
    monkeypatch.setenv("OMI_ROW_CEILING", "500")
    monkeypatch.setenv("OMI_ANCHOR_TOLERANCE_S", "30")
    monkeypatch.setenv("OMI_HTTP_PORT", "9999")
    c = OmiConfig()
    assert c.ttl_hours == 24.0
    assert c.row_ceiling == 500
    assert c.anchor_tolerance_s == 30.0
    assert c.port == 9999


def test_load_config_returns_instance(monkeypatch):
    _clean(monkeypatch)
    assert isinstance(load_config(), OmiConfig)

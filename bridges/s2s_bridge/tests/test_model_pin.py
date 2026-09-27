"""The realtime model is pinned by the bridge, never inherited from pipecat's default.

pipecat's default moves between releases (1.3.0 defaulted to ``gpt-realtime-2``,
1.12.0 to ``gpt-realtime-2.1``), so an unpinned bridge silently changes models on a
version bump. These tests build the service through the bridge's own factory
against the REAL installed pipecat.
"""

import asyncio

import pytest

from app.main import Application


def _built_model(monkeypatch, env_value):
    if env_value is None:
        monkeypatch.delenv("S2S_MODEL", raising=False)
    else:
        monkeypatch.setenv("S2S_MODEL", env_value)
    app = Application()
    app.openai_api_key = "test-key"
    app.instructions = "test"
    app.semantic_vad_eagerness = "medium"
    app._genesis_tools = []
    app._read_model_config()
    asyncio.run(app._ensure_openai_service())
    return app.openai_service._settings.model


def test_the_default_model_is_the_one_the_edge_already_runs(monkeypatch):
    assert _built_model(monkeypatch, None) == "gpt-realtime-2"


@pytest.mark.parametrize("value", ["gpt-realtime-2.1", "  gpt-realtime-2.1  "])
def test_s2s_model_overrides_the_pin(monkeypatch, value):
    assert _built_model(monkeypatch, value) == "gpt-realtime-2.1"


def test_a_blank_s2s_model_keeps_the_pin(monkeypatch):
    assert _built_model(monkeypatch, "   ") == "gpt-realtime-2"

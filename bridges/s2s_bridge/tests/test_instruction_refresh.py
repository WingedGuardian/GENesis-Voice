"""Tests for the per-session Genesis system-prompt refresh.

The bridge used to fetch the Genesis prompt ONCE at process init and cache it for the
life of the process — a 6-day-old bridge greeted callers with "Today is Thursday,
June 25, 2026" (its start date; Genesis renders the date per request, the bridge froze
it). The fix re-fetches at session start and updates BOTH the app cache and the LIVE
service's settings (the pipeline holds the service object forever, so a new service
can't be swapped in; ``_send_session_update`` reads ``_settings.session_properties``
AND ``_settings.system_instruction``, so both must be updated).

These tests run against the REAL installed pipecat (same version as the edge) so a
pipecat upgrade that moves the internals fails HERE, not silently in production.
"""
import asyncio

from pipecat.services.openai.realtime.events import SessionProperties
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app import main as main_mod
from app.main import Application


class _FakeToolService:
    def __init__(self, prompt=None, exc=None, delay=0.0):
        self._prompt = prompt
        self._exc = exc
        self._delay = delay
        self.calls = 0

    async def get_system_prompt(self):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc:
            raise self._exc
        return self._prompt


def _app_with_service(stale="STALE PROMPT: Today is Thursday, June 25, 2026"):
    app = Application()
    app.instructions = stale
    app.openai_service = OpenAIRealtimeLLMService(
        api_key="test-key",
        session_properties=SessionProperties(instructions=stale),
        start_audio_paused=False,
    )
    return app


def test_refresh_updates_cache_and_live_service_settings():
    app = _app_with_service()
    app.genesis_tool_service = _FakeToolService(prompt="FRESH: Today is Thursday, July 2, 2026")
    asyncio.run(app._refresh_instructions())
    assert app.instructions.startswith("FRESH")
    # The live service replays these on every (re)connect — both read paths must be fresh.
    settings = app.openai_service._settings
    assert settings.session_properties.instructions.startswith("FRESH")
    assert settings.system_instruction.startswith("FRESH")


def test_refresh_failure_keeps_cached_instructions():
    app = _app_with_service()
    app.genesis_tool_service = _FakeToolService(exc=RuntimeError("genesis down"))
    asyncio.run(app._refresh_instructions())     # must not raise
    assert app.instructions.startswith("STALE")
    assert app.openai_service._settings.session_properties.instructions.startswith("STALE")


def test_refresh_timeout_keeps_cached_instructions(monkeypatch):
    # A hung Genesis must not add dead air to voice session start — the fetch is bounded.
    monkeypatch.setattr(main_mod, "_PROMPT_REFRESH_TIMEOUT_S", 0.05)
    app = _app_with_service()
    app.genesis_tool_service = _FakeToolService(prompt="FRESH", delay=0.5)
    asyncio.run(app._refresh_instructions())     # must not raise, must not wait 0.5s
    assert app.instructions.startswith("STALE")


def test_refresh_empty_prompt_keeps_cached_instructions():
    # Genesis returning an empty prompt (misconfig) must not blank the live persona.
    app = _app_with_service()
    app.genesis_tool_service = _FakeToolService(prompt="")
    asyncio.run(app._refresh_instructions())
    assert app.instructions.startswith("STALE")


def test_refresh_without_service_updates_cache_only():
    # First-ever connect can run before the service exists — cache updates, no crash.
    app = Application()
    app.instructions = "STALE"
    app.openai_service = None
    app.genesis_tool_service = _FakeToolService(prompt="FRESH")
    asyncio.run(app._refresh_instructions())
    assert app.instructions == "FRESH"

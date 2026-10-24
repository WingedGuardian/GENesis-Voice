"""Exercise the disconnect guard through the bridge's actual pipeline wiring."""

import asyncio
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import BotStartedSpeakingFrame, StartFrame, TranscriptionFrame
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.main import Application
from app.session_manager import SessionManager
from app.websocket_handler import WebSocketHandler


class _Service(FrameProcessor):
    """No provider connection; use the real service's transcript emission method."""

    def __init__(self):
        super().__init__()
        self.ready = asyncio.Event()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.ready.set()

    async def _call_event_handler(self, *args):
        pass

    async def _handle_user_transcription(self, *args):
        pass


async def _transcript_reaches_guard(monkeypatch, with_context):
    consumed = asyncio.Event()

    class Endpoint(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TranscriptionFrame):
                consumed.set()
            await self.push_frame(frame, direction)

    transport = SimpleNamespace(input=lambda: incoming, output=lambda: outgoing)
    incoming, outgoing = Endpoint(), Endpoint()
    sessions = SessionManager() if with_context else None
    handler = WebSocketHandler(session_manager=sessions)
    app = Application.__new__(Application)
    app.websocket_handler = handler
    service = _Service()
    monkeypatch.setattr("app.websocket_handler.PipelineRunner", lambda: PipelineRunner(handle_sigint=False))
    _pipeline, _runner, task = handler.build_pipeline(transport, service, "test")
    run = handler._pipeline_task
    if with_context:
        aggregator = sessions.context_aggregators["test"].user()
        original = aggregator._handle_transcription

        async def consume(frame):
            await original(frame)
            consumed.set()

        aggregator._handle_transcription = consume
    try:
        await asyncio.wait_for(service.ready.wait(), 5)
        idle = handler.session_idle_manager
        await idle.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert not app._disconnect_is_legitimate()

        for text, expected in [("  ", False), ("goodbye", True)]:
            consumed.clear()
            await OpenAIRealtimeLLMService.handle_evt_input_audio_transcription_completed(
                service, SimpleNamespace(item_id="test", transcript=text),
            )
            await asyncio.wait_for(consumed.wait(), 2)
            assert app._disconnect_is_legitimate() is expected

        await idle.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert not app._disconnect_is_legitimate()
    finally:
        await task.cancel()
        await asyncio.wait_for(run, 5)


@pytest.mark.parametrize("with_context", [True, False])
def test_upstream_user_transcript_reaches_the_disconnect_guard(monkeypatch, with_context):
    asyncio.run(_transcript_reaches_guard(monkeypatch, with_context))

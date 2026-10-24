"""The application owns one runner invocation and its cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pipecat.processors.frame_processor import FrameProcessor

from app.main import Application
from app.websocket_handler import WebSocketHandler


async def _boot(monkeypatch):
    runs = []

    class Runner:
        async def run(self, task):
            runs.append(task)
            await asyncio.sleep(0)  # lets an incorrectly scheduled second start run

    monkeypatch.setattr("app.websocket_handler.PipelineRunner", Runner)
    incoming, outgoing = FrameProcessor(), FrameProcessor()
    transport = SimpleNamespace(
        input=lambda: incoming,
        output=lambda: outgoing,
        event_handler=lambda name: (lambda fn: fn),
    )
    app = Application()
    app.initialize = AsyncMock()
    app._ensure_openai_service = AsyncMock()
    app.cleanup = AsyncMock()
    app.websocket_handler = WebSocketHandler()
    app.websocket_transport = transport
    app.openai_service = FrameProcessor()
    app.openai_service._websocket = None
    before = asyncio.all_tasks()
    try:
        await app.run()
        await asyncio.sleep(0)
        assert runs == [app.current_task]
        app.cleanup.assert_awaited_once()
    finally:
        pending = asyncio.all_tasks() - before
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def test_boot_runs_the_pipeline_once(monkeypatch):
    asyncio.run(_boot(monkeypatch))

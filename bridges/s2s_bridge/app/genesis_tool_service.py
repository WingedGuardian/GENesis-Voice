"""Genesis tool dispatch — HTTP client for the S2S voice bridge addon.

Replaces the HA MCP integration from the upstream project.  The addon
calls Genesis's Flask endpoints to dispatch tool calls triggered by
the OpenAI Realtime model during a voice conversation.

Endpoints called:
- ``POST /v1/voice/tool_call`` — dispatch ask_genesis, web_search, approve_pending
- ``GET /v1/voice/system_prompt`` — fetch Genesis persona + context
- ``GET /v1/voice/tool_declarations`` — fetch tool schemas for session config
- ``POST /api/t/memory_store`` — persist conversation transcripts (Phase 3D)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MAX_PERSIST_ATTEMPTS = 3


class GenesisToolService:
    """HTTP client for Genesis voice tool dispatch.

    Called by the Pipecat pipeline when the OpenAI Realtime model
    triggers a function call (ask_genesis, web_search, approve_pending).
    """

    def __init__(
        self, genesis_url: str, token: str = "", fallback_dir: str = "failed_transcripts",
    ) -> None:
        self._url = genesis_url.rstrip("/")
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        # Where to save a transcript if persistence to Genesis fails after all
        # retries, so a conversation is never lost (see store_conversation).
        self._fallback_dir = Path(fallback_dir)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        """Dispatch a tool call to Genesis and return the result."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._url}/v1/voice/tool_call",
                headers=self._headers,
                json={"tool_name": name, "arguments": arguments},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_system_prompt(self) -> str:
        """Fetch the Genesis voice system prompt."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self._url}/v1/voice/system_prompt",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json().get("prompt", "")

    async def get_tool_declarations(self) -> list[dict]:
        """Fetch Genesis tool declarations for OpenAI session config."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self._url}/v1/voice/tool_declarations",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json().get("tools", [])

    async def store_conversation(
        self, messages: list, satellite_id: str = "s2s-default",
    ) -> dict | None:
        """Persist a voice conversation transcript to Genesis memory.

        Matches the format used by ``S2SSessionManager.close()``
        (s2s_session.py) — same source, tags, wing, room — so all voice
        conversations appear in the same memory namespace regardless of
        which pipeline produced them.
        """
        turns: list[str] = []
        for msg in messages:
            role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                label = "User" if role == "user" else "Genesis"
                turns.append(f"{label}: {content}")

        if not turns:
            logger.debug("No user/assistant turns to persist")
            return None

        transcript = f"Voice conversation [{satellite_id}]:\n" + "\n".join(turns)
        # Log the transcript turn-by-turn BEFORE the (fragile) network call, so
        # the conversation survives in journald even if every persist attempt
        # fails — and one oversized line can't be truncated by journald.
        logger.info(
            "Persisting voice conversation (%d turns, %d chars):",
            len(turns), len(transcript),
        )
        for turn in turns:
            logger.info("  %s", turn)

        payload = {
            "content": transcript,
            "source": "voice_s2s",
            "memory_type": "episodic",
            "tags": ["voice", "s2s", "conversation"],
            "confidence": 0.5,
            "wing": "channels",
            "room": "voice",
        }

        last_exc: Exception | None = None
        for attempt in range(_MAX_PERSIST_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{self._url}/api/t/memory_store",
                        headers=self._headers,
                        json=payload,
                    )
                    resp.raise_for_status()
                    return resp.json()
            except asyncio.CancelledError:
                # Abrupt teardown (loop shutting down / task cancelled) raises a
                # BaseException that skips ordinary excepts. Save synchronously,
                # then let the cancellation propagate.
                self._write_fallback(transcript, None)
                raise
            except Exception as exc:
                # Broad on purpose: httpx errors AND non-httpx teardown errors
                # (OSError, "Event loop is closed") must not lose the transcript.
                last_exc = exc
                logger.warning(
                    "Persist attempt %d/%d failed: %s: %s",
                    attempt + 1, _MAX_PERSIST_ATTEMPTS, type(exc).__name__, exc,
                )
                if attempt < _MAX_PERSIST_ATTEMPTS - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))

        # Every attempt failed — save locally so the transcript is never lost.
        self._write_fallback(transcript, last_exc)
        return None

    def _write_fallback(self, transcript: str, exc: Exception | None) -> None:
        """Save a transcript to a local file when Genesis is unreachable."""
        try:
            self._fallback_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = self._fallback_dir / f"voice_{stamp}.txt"
            n = 0
            while path.exists() and n < 100:  # avoid clobber within the same second
                n += 1
                path = self._fallback_dir / f"voice_{stamp}_{n}.txt"
            if path.exists():  # pathological dir; guarantee uniqueness, don't spin
                path = self._fallback_dir / f"voice_{stamp}_{uuid.uuid4().hex[:8]}.txt"
            path.write_text(transcript, encoding="utf-8")
            logger.warning(
                "Persist failed after %d attempts (%s) — saved transcript to %s",
                _MAX_PERSIST_ATTEMPTS,
                type(exc).__name__ if exc else "unknown",
                path,
            )
        except Exception as fexc:  # never let fallback failure crash disconnect
            logger.error(
                "Could not write fallback transcript: %s: %s",
                type(fexc).__name__, fexc,
            )

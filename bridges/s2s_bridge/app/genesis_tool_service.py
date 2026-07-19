"""Genesis tool dispatch — HTTP client for the S2S voice bridge addon.

Replaces the HA MCP integration from the upstream project.  The addon
calls Genesis's Flask endpoints to dispatch tool calls triggered by
the OpenAI Realtime model during a voice conversation.

Endpoints called:
- ``POST /v1/voice/tool_call`` — dispatch ask_genesis, web_search, approve_pending
- ``GET /v1/voice/system_prompt`` — fetch Genesis persona + context
- ``GET /v1/voice/tool_declarations`` — fetch tool schemas for session config
- ``POST /v1/voice/conversation`` — persist conversation transcripts (extraction parity)
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
        self,
        genesis_url: str,
        token: str = "",
        fallback_dir: str = "failed_transcripts",
        session_split_seconds: float = 900.0,
    ) -> None:
        self._url = genesis_url.rstrip("/")
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        # Where to save a transcript if persistence to Genesis fails after all
        # retries, so a conversation is never lost (see sync_conversation).
        self._fallback_dir = Path(fallback_dir)
        # ── Conversation session scoping (see sync_conversation) ──
        # The bridge's cached context grows monotonically for the whole process
        # lifetime, so "one session per process" would produce a single
        # transcript spanning days/weeks. We slice it into per-conversation
        # transcripts by rotating the session id whenever a persist arrives more
        # than ``session_split_seconds`` after the previous one — mirroring the
        # SessionManager context-reuse window: a longer gap means the previous
        # conversation ended and a new one began.
        self._session_split_seconds = session_split_seconds
        self._session_id: str | None = None
        self._session_base = 0  # count of turns belonging to prior epochs
        self._last_turn_count = 0
        self._last_persist_ts = 0.0

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

    async def sync_conversation(
        self,
        messages: list,
        satellite_id: str = "s2s-default",
    ) -> dict | None:
        """Persist a voice conversation to Genesis via the cumulative-turns route.

        ``messages`` is the bridge's full cached context, which grows
        monotonically for the process lifetime. We filter it to user/assistant
        turns and POST the cumulative list under a session id that ROTATES on a
        long gap between persists, so each transcript maps to one human
        conversation rather than the whole process epoch. The core route
        (``sync_cumulative``) appends only turns beyond the transcript's current
        line count, so replays, double-fires and cumulative re-sends land
        exactly once.

        Replaces the former one-blob ``POST /api/t/memory_store`` landing, which
        bypassed the memory extraction pipeline.
        """
        turns = self._extract_turns(messages)
        if not turns:
            logger.debug("No user/assistant turns to persist")
            return None

        now = time.time()
        gap = self._session_id is not None and now - self._last_persist_ts > self._session_split_seconds
        if self._session_id is None or gap:
            # New conversation: turns accumulated so far belong to prior epochs,
            # so base them out (0 on the very first persist).
            self._start_session(base=self._last_turn_count if gap else 0)
        if len(turns) < self._session_base:
            # The cumulative list shrank below our epoch base. Impossible under
            # the bridge's monotonic context (verified in prod), but never trust
            # it — a stale base would silently drop every turn. Restart clean.
            logger.warning(
                "Voice turn list (%d) shrank below session base (%d) — rotating session id",
                len(turns),
                self._session_base,
            )
            self._start_session(base=0)

        self._last_turn_count = len(turns)
        self._last_persist_ts = now

        pending = turns[self._session_base :]
        if not pending:
            # Reconnect churn with no new turns since the last persist — the
            # core would append nothing anyway, so skip the network round-trip.
            logger.debug("No new turns since last persist — skipping POST")
            return None

        # Log the (new) turns BEFORE the fragile network call, so the
        # conversation survives in journald even if every persist attempt fails.
        logger.info(
            "Persisting voice conversation (session=%s, %d new / %d total turns):",
            self._session_id,
            len(pending),
            len(turns),
        )
        for turn in pending:
            logger.info("  %s: %s", turn["role"], turn["text"])

        payload = {
            "session_id": self._session_id,
            "satellite_id": satellite_id,
            "turns": pending,
        }
        return await self._post_conversation(payload, pending)

    def _start_session(self, base: int) -> None:
        """Begin a new transcript session; ``base`` turns precede it (prior epochs)."""
        self._session_id = uuid.uuid4().hex
        self._session_base = base

    @staticmethod
    def _extract_turns(messages: list) -> list[dict]:
        """Filter the raw context to persisted user/assistant turns.

        Drops system/developer/tool messages and empties — matching the shape
        the core ``/v1/voice/conversation`` validator accepts (role in
        user/assistant, non-empty text). Assistant tool-call messages carry
        non-str content and are naturally excluded by the ``isinstance`` check.
        """
        turns: list[dict] = []
        for msg in messages:
            role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                turns.append({"role": role, "text": content})
        return turns

    async def _post_conversation(
        self,
        payload: dict,
        pending: list[dict],
    ) -> dict | None:
        """POST the cumulative turns; retry transient errors, fallback on failure.

        4xx is permanent (validation/cap/auth) — saved immediately, not retried.
        5xx and network/teardown errors are retried, then saved locally.
        """
        url = f"{self._url}/v1/voice/conversation"
        last_exc: Exception | None = None
        for attempt in range(_MAX_PERSIST_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(url, headers=self._headers, json=payload)
                    resp.raise_for_status()
                    return resp.json()
            except asyncio.CancelledError:
                # Abrupt teardown (loop shutting down / task cancelled) raises a
                # BaseException that skips ordinary excepts. Save synchronously,
                # then let the cancellation propagate.
                self._write_fallback(self._render(pending), None)
                raise
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if 400 <= code < 500:
                    # Validation / cap / auth — retrying will not help.
                    logger.error(
                        "Voice conversation POST rejected %d (permanent) — saving locally, not retrying: %s",
                        code,
                        exc,
                    )
                    self._write_fallback(self._render(pending), exc)
                    return None
                last_exc = exc
                logger.warning(
                    "Persist attempt %d/%d failed: HTTP %d",
                    attempt + 1,
                    _MAX_PERSIST_ATTEMPTS,
                    code,
                )
            except Exception as exc:
                # Broad on purpose: httpx errors AND non-httpx teardown errors
                # (OSError, "Event loop is closed") must not lose the transcript.
                last_exc = exc
                logger.warning(
                    "Persist attempt %d/%d failed: %s: %s",
                    attempt + 1,
                    _MAX_PERSIST_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                )
            if attempt < _MAX_PERSIST_ATTEMPTS - 1:
                await asyncio.sleep(0.5 * (attempt + 1))

        # Every attempt failed — save locally so the transcript is never lost.
        self._write_fallback(self._render(pending), last_exc)
        return None

    @staticmethod
    def _render(turns: list[dict]) -> str:
        """Render turns as a human-readable transcript for a local fallback file."""
        label = {"user": "User", "assistant": "Genesis"}
        return "\n".join(f"{label.get(t['role'], t['role'])}: {t['text']}" for t in turns)

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

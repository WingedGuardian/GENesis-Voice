"""Main application entry point using Pipecat.

Genesis voice S2S bridge — connects Voice PE to OpenAI Realtime API
with Genesis tool dispatch.  Forked from fjfricke/ha-openai-realtime,
adapted to call Genesis HTTP endpoints instead of HA MCP.
"""
import asyncio
import json
import logging
import os
import sys

import dotenv
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.websocket.server import WebsocketServerTransport

from app.audio_recording_service import AudioRecordingService
from app.disconnect_tool import create_disconnect_tool_handler, get_disconnect_tool_definition
from app.genesis_tool_service import GenesisToolService
from app.session_manager import SessionManager
from app.websocket_handler import WebSocketHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Reduce verbosity of noisy loggers
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("__main__").setLevel(logging.INFO)

dotenv.load_dotenv()

# Bound on the per-session Genesis prompt re-fetch: a hung Genesis must not add dead
# air to voice session start. Normal fetches are ~100ms (LAN); on timeout/failure the
# session proceeds with the cached prompt (a stale date beats a delayed answer).
_PROMPT_REFRESH_TIMEOUT_S = 3.0


class Application:
    """Main application class using Pipecat."""

    def __init__(self):
        """Initialize application."""
        self.pipeline: Pipeline | None = None
        self.runner: PipelineRunner | None = None
        self.websocket_handler: WebSocketHandler | None = None
        self.websocket_transport: WebsocketServerTransport | None = None
        self.openai_service: OpenAIRealtimeLLMService | None = None
        self.genesis_tool_service: GenesisToolService | None = None
        self.audio_recording_service: AudioRecordingService | None = None
        self.session_manager: SessionManager | None = None
        self.current_task: PipelineTask | None = None
        self._pipeline_lock: asyncio.Lock | None = None
        # Monotonic connection counter, bumped on each client connect. A
        # disconnect handler snapshots it on entry and re-checks before tearing
        # down the (shared) OpenAI session, so a slow/stale disconnect can't
        # clobber a newer connection's freshly-reset session (the reconnect race
        # that left the device connected to a brain-less bridge → dark).
        self._conn_seq = 0
        # Strong refs to in-flight fire-and-forget persist tasks. Without this
        # the loop keeps only a weak ref and the GC can collect a task
        # mid-flight (documented CPython asyncio caveat).
        self._persist_tasks: set[asyncio.Task] = set()
        self._rotation_task: asyncio.Task | None = None
        # Session rotation interval in seconds. OpenAI Realtime sessions
        # expire at 60 minutes — rotate at 55 to leave margin for the
        # reconnect handshake + context replay. Configurable via env for
        # testing (e.g. SESSION_ROTATION_SECONDS=30).
        self._rotation_interval: float = 55 * 60
        # OpenAI Realtime voice (preset). Configurable via the VOICE_S2S_VOICE
        # add-on option; default "ash". Re-read from env in initialize().
        self.s2s_voice: str = "ash"

    async def initialize(self) -> None:
        """Initialize all components."""
        # Get configuration from environment
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        websocket_port = int(os.environ.get("WEBSOCKET_PORT", "8080"))
        websocket_host = os.environ.get("WEBSOCKET_HOST", "0.0.0.0")

        # Semantic VAD eagerness (replaces old threshold-based server_vad)
        # Options: "low" (less interrupts), "medium" (default), "high" (more responsive)
        self.semantic_vad_eagerness = os.environ.get("SEMANTIC_VAD_EAGERNESS", "medium")

        # OpenAI Realtime voice preset (configurable add-on option; default "ash")
        self.s2s_voice = os.environ.get("VOICE_S2S_VOICE", "ash")

        # Session rotation interval (override for testing)
        self._rotation_interval = float(
            os.environ.get("SESSION_ROTATION_SECONDS", str(55 * 60))
        )

        # Get recording setting (optional, defaults to false)
        enable_recording = os.environ.get("ENABLE_RECORDING", "false").lower() == "true"

        # Noise gate + idle-timeout config (calibrated live; sane defaults).
        # NoiseGate: gate sub-threshold mic noise before OpenAI's VAD sees it.
        # SessionIdleManager: end the session only on genuine idle.
        noise_gate_open_threshold = int(os.environ.get("NOISE_GATE_OPEN_THRESHOLD", "500"))
        noise_gate_bot_speaking_threshold = int(
            os.environ.get("NOISE_GATE_BOT_SPEAKING_THRESHOLD", "1500")
        )
        noise_gate_hangover_ms = float(os.environ.get("NOISE_GATE_HANGOVER_MS", "250"))
        # Onset guard: mute ALL mic input for this long after the bot starts speaking.
        # The bot's own onset echo used to trip OpenAI's VAD and cut the bot off ~0.5s
        # in (observed live; the cutoffs cascade). Echo and real barge-in overlap in
        # amplitude, so only a time-based guard separates them. 0 disables the guard.
        noise_gate_bot_onset_guard_ms = float(
            os.environ.get("NOISE_GATE_BOT_ONSET_GUARD_MS", "1500")
        )
        # Diagnostic logging cadence for the noise gate (sampled). 0 = off
        # (post-calibration quiet switch).
        noise_gate_log_interval_s = float(
            os.environ.get("NOISE_GATE_LOG_INTERVAL_S", "2.0")
        )
        idle_timeout_seconds = float(os.environ.get("IDLE_TIMEOUT_SECONDS", "45"))
        # Bound on a continuously-'pending' response/tool (recovers the device if
        # an LLM response or tool call stalls without its End/Result frame).
        max_pending_active_seconds = float(
            os.environ.get("MAX_PENDING_ACTIVE_SECONDS", "180")
        )
        logger.info(
            "Noise gate: open=%d, bot_speaking=%d, hangover=%.0fms, onset_guard=%.0fms, "
            "log_interval=%.1fs; idle_timeout=%.0fs; max_pending_active=%.0fs",
            noise_gate_open_threshold,
            noise_gate_bot_speaking_threshold,
            noise_gate_hangover_ms,
            noise_gate_bot_onset_guard_ms,
            noise_gate_log_interval_s,
            idle_timeout_seconds,
            max_pending_active_seconds,
        )

        # Get session reuse timeout and initialize session manager
        session_reuse_timeout = float(os.environ.get("SESSION_REUSE_TIMEOUT_SECONDS", "300"))
        self.session_manager = SessionManager(reuse_timeout=session_reuse_timeout)
        logger.info(f"Session reuse timeout: {session_reuse_timeout} seconds")

        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")

        # Initialize Genesis tool service — fetches tools + system prompt
        genesis_url = os.environ.get("GENESIS_URL", "http://localhost:5000")
        genesis_token = os.environ.get("GENESIS_TOKEN", "")
        self.genesis_tool_service = GenesisToolService(genesis_url, genesis_token)

        # Fetch system prompt and tool declarations from Genesis
        instructions = ""
        self._genesis_tools: list[dict] = []
        try:
            instructions = await self.genesis_tool_service.get_system_prompt()
            self._genesis_tools = await self.genesis_tool_service.get_tool_declarations()
            logger.info(
                "Genesis tools loaded: %s",
                [t.get("name") for t in self._genesis_tools],
            )
        except Exception as e:
            logger.warning(f"Failed to fetch Genesis tools/prompt: {e}")
            instructions = os.environ.get(
                "INSTRUCTIONS",
                "You are Genesis, a cognitive AI partner.",
            )

        # Initialize audio recording service before WebSocket handler (handler needs it)
        self.audio_recording_service = AudioRecordingService(
            enable_recording=enable_recording,
            sample_rate=24000,
            chunk_duration_seconds=30,
            output_dir="recordings"
        )

        # Initialize WebSocket handler
        self.websocket_handler = WebSocketHandler(
            host=websocket_host,
            port=websocket_port,
            session_manager=self.session_manager,
            audio_recording_service=self.audio_recording_service,
            noise_gate_open_threshold=noise_gate_open_threshold,
            noise_gate_bot_speaking_threshold=noise_gate_bot_speaking_threshold,
            noise_gate_hangover_ms=noise_gate_hangover_ms,
            noise_gate_bot_onset_guard_ms=noise_gate_bot_onset_guard_ms,
            noise_gate_log_interval_s=noise_gate_log_interval_s,
            idle_timeout_seconds=idle_timeout_seconds,
            max_pending_active_seconds=max_pending_active_seconds,
        )
        self.websocket_transport = self.websocket_handler.create_transport()

        # Store configuration for session creation
        self.openai_api_key = openai_api_key
        self.instructions = instructions

        logger.info("✅ Application initialized - ready to accept WebSocket connections")

    def _build_pipeline_for_transport(self, transport: WebsocketServerTransport, client_id: str):
        """
        Build pipeline for a WebSocket transport connection.

        Args:
            transport: The WebSocket transport instance
            client_id: Unique identifier for the client device
        """
        # Ensure OpenAI service exists
        if self.openai_service is None:
            raise RuntimeError("OpenAI service must be created before building pipeline")

        # Use WebSocket handler to build pipeline
        self.pipeline, self.runner, self.current_task = self.websocket_handler.build_pipeline(
            transport=transport,
            openai_service=self.openai_service,
            client_id=client_id,
            activity_callback=self._update_session_activity
        )

    def _update_session_activity(self):
        """Update session activity timestamp (called by SessionActivityTracker)."""
        pass

    async def _refresh_instructions(self) -> None:
        """Re-fetch the Genesis system prompt at SESSION START (bounded, best-effort).

        The prompt was previously fetched ONCE at process init and cached for the life
        of the bridge — a 6-day-old process greeted callers with "Today is Thursday,
        June 25, 2026" (its start date; Genesis renders the date per request, the bridge
        froze it). The pipeline holds the OpenAI service object forever, so a fresh
        prompt must be pushed into the LIVE service's settings: ``_send_session_update``
        reads BOTH ``_settings.session_properties`` and ``_settings.system_instruction``
        (pipecat 1.3.0), so both are updated. Any failure keeps the cached prompt —
        session start must never block on Genesis.
        """
        if not self.genesis_tool_service:
            return
        try:
            fresh = await asyncio.wait_for(
                self.genesis_tool_service.get_system_prompt(), _PROMPT_REFRESH_TIMEOUT_S
            )
        except Exception as e:
            logger.warning(
                "⚠️ System-prompt re-fetch failed — keeping cached instructions: %s: %s",
                type(e).__name__, e,
            )
            return
        if not fresh or fresh == self.instructions:
            return  # empty prompt (misconfig) must not blank the persona; same = no-op
        self.instructions = fresh
        svc = self.openai_service
        if svc is not None:
            # Mutate under the pipeline lock so the writes can't interleave with an
            # in-flight reset_conversation (the 55-min rotation loop holds this lock
            # for its whole reset).
            if self._pipeline_lock is None:
                self._pipeline_lock = asyncio.Lock()
            async with self._pipeline_lock:
                try:
                    svc._settings.session_properties.instructions = fresh
                    svc._settings.system_instruction = fresh
                    # pipecat also tracks a _base_system_instruction for composing
                    # addon instructions; left stale it would clobber the refresh if
                    # an update-settings frame ever recomposes. None sent today —
                    # this keeps the latent path consistent too.
                    if hasattr(svc, "_base_system_instruction"):
                        svc._base_system_instruction = fresh
                    logger.info("📜 Genesis prompt refreshed (%d chars) — live session updated",
                                len(fresh))
                except AttributeError:
                    # pipecat internals moved (upgrade?) — the new prompt still applies
                    # to any future service; be loud rather than silently serving stale.
                    logger.warning(
                        "⚠️ Could not update the live session properties (pipecat internals "
                        "changed?) — fresh prompt cached for the next service", exc_info=True,
                    )

    async def _persist_transcript(self, messages: list) -> None:
        """Persist a voice transcript out-of-band so it never blocks teardown.

        Spawned as a background task from on_client_disconnected — a slow Genesis
        here must not delay the OpenAI teardown (see the reconnect-race guard).
        """
        try:
            result = await self.genesis_tool_service.store_conversation(messages)
            if result:
                logger.info("💾 Voice conversation persisted to Genesis memory")
        except Exception as e:  # never let a persist failure escape the task
            logger.warning(
                "⚠️ Failed to persist voice conversation: %s: %s", type(e).__name__, e
            )

    def _disconnect_is_legitimate(self) -> bool:
        """True if the model's disconnect_client is a real end-of-call.

        Honour it only when a real user turn followed the bot's last response —
        i.e. the user actually said something to end the call. A spurious echo
        false-interrupt can drop the model into an empty-turn state where it
        hangs up with no user input; that case returns False so we keep the
        session alive (the 45s idle still ends it cleanly).
        """
        idle = getattr(self.websocket_handler, "session_idle_manager", None)
        if idle is None:
            return True  # no tracker available — don't block
        return idle.user_turn_since_last_bot_response()

    async def _ensure_openai_service(self, client_id: str | None = None):
        """Ensure the OpenAI service is ready for a client.

        On first call (startup): creates a new service instance, registers tools.
        On subsequent calls (client connect): resets the existing service's
        conversation to get a fresh OpenAI session (refreshes the 60-min clock)
        while keeping the same service object in the pipeline.

        Args:
            client_id: Optional client ID for session management
        """
        if self._pipeline_lock is None:
            self._pipeline_lock = asyncio.Lock()

        async with self._pipeline_lock:
            # Service already exists (pipeline holds reference) — reconnect to OpenAI.
            # The service was disconnected either at startup (deferred connection)
            # or when the previous client left.
            if self.openai_service is not None and client_id is not None:
                try:
                    self.session_manager.cleanup_before_new_session(client_id)
                except Exception as e:
                    logger.warning(f"⚠️ Error caching context: {e}")

                if self.openai_service._context is not None:
                    # Context exists from a previous conversation — full reset
                    # (disconnects, processes completed calls, reconnects)
                    logger.info(f"🔄 Resetting OpenAI session for client {client_id}...")
                    try:
                        await self.openai_service.reset_conversation()
                        logger.info(f"✅ OpenAI session reset for client {client_id}")
                    except Exception as e:
                        logger.warning(f"⚠️ Session reset failed, reconnecting: {e}")
                        self.openai_service._llm_needs_conversation_setup = True
                        await self.openai_service._connect()
                else:
                    # No context yet (first-ever client, or service was just created).
                    # Reset conversation setup flag so instructions and tools are sent
                    # after session.created → session.updated cycle completes.
                    # Without this, _create_response() skips conversation setup and
                    # OpenAI has no context to generate audio from → silence.
                    logger.info(f"🔗 Connecting OpenAI session for client {client_id}...")
                    self.openai_service._llm_needs_conversation_setup = True
                    await self.openai_service._connect()
                    logger.info(f"✅ OpenAI session connected for client {client_id}")

                self.session_manager.set_current_service(client_id, self.openai_service)
                return self.openai_service

            # First call or reset failed — create a brand new service
            if client_id:
                logger.info(f"🆕 Creating new OpenAI service for client {client_id}...")
            else:
                logger.info("🆕 Creating new OpenAI service (initial)...")

            # Create session properties with audio configuration
            from pipecat.services.openai.realtime.events import (
                AudioConfiguration,
                AudioInput,
                AudioOutput,
                InputAudioNoiseReduction,
                InputAudioTranscription,
                SemanticTurnDetection,
                SessionProperties,
            )

            # Collect tool definitions: disconnect + Genesis tools
            disconnect_tool_def = get_disconnect_tool_definition()
            all_tools = [disconnect_tool_def] + list(self._genesis_tools)

            session_properties = SessionProperties(
                instructions=self.instructions,
                audio=AudioConfiguration(
                    input=AudioInput(
                        turn_detection=SemanticTurnDetection(
                            eagerness=self.semantic_vad_eagerness,
                        ),
                        noise_reduction=InputAudioNoiseReduction(type="near_field"),
                        transcription=InputAudioTranscription(),
                    ),
                    output=AudioOutput(voice=self.s2s_voice)
                ),
                tools=all_tools
            )

            logger.info(f"🔧 Creating session with {len(all_tools)} tools: {[tool.get('name', 'unknown') for tool in all_tools]}")

            # Create new service instance
            self.openai_service = OpenAIRealtimeLLMService(
                api_key=self.openai_api_key,
                session_properties=session_properties,
                start_audio_paused=False
            )
            logger.info(f"✅ OpenAI Service created: {type(self.openai_service).__name__}")

            # Register disconnect tool handler
            disconnect_tool_handler = create_disconnect_tool_handler(
                self.websocket_transport,
                should_honor=self._disconnect_is_legitimate,
            )
            self.openai_service.register_function("disconnect_client", disconnect_tool_handler)
            logger.info("Registered disconnect tool handler")

            # Register Genesis tool handlers — dispatch via HTTP to Genesis
            for tool_def in self._genesis_tools:
                tool_name = tool_def.get("name", "")
                if not tool_name:
                    continue

                async def genesis_tool_handler(params):
                    """Dispatch tool call to Genesis via HTTP."""
                    try:
                        result = await self.genesis_tool_service.call_tool(
                            params.function_name, params.arguments,
                        )
                        await params.result_callback(json.dumps(result))
                    except Exception as exc:
                        logger.error("Genesis tool %s failed: %s", params.function_name, exc)
                        await params.result_callback(
                            json.dumps({"error": str(exc)}),
                        )

                self.openai_service.register_function(tool_name, genesis_tool_handler)

            logger.info(
                "Registered %d Genesis tools: %s",
                len(self._genesis_tools),
                [t.get("name") for t in self._genesis_tools],
            )

            # Register service with session manager
            if client_id:
                self.session_manager.set_current_service(client_id, self.openai_service)

            logger.info("✅ New OpenAI Session created")
            return self.openai_service

    async def run(self) -> None:
        """Run the application."""
        await self.initialize()

        # Create initial OpenAI service — needed by pipeline at build time.
        # The service auto-connects during pipeline start (StartFrame → start() → _connect()).
        # A background task disconnects after pipeline is ready, deferring the 60-min clock.
        await self._ensure_openai_service()

        # Build pipeline - based on pipecat-examples, one pipeline handles all connections
        # The transport manages multiple connections internally
        self._build_pipeline_for_transport(self.websocket_transport, "server")

        # Background task: wait for pipeline to start, then disconnect from OpenAI.
        # StartFrame → start() → _connect() happens during runner.run(). We can't
        # disconnect before that (nothing to disconnect) or synchronously after (run blocks).
        # Instead, schedule a deferred disconnect that waits for the WS to be established.
        async def _deferred_disconnect():
            """Wait for pipeline to connect to OpenAI, then disconnect to save the 60-min clock."""
            for _ in range(30):  # Wait up to 15s for connection
                await asyncio.sleep(0.5)
                if self.openai_service and self.openai_service._websocket:
                    await asyncio.sleep(1)  # Let session.created + _update_settings() complete
                    async with self._pipeline_lock:
                        # Only disconnect if no client has connected in the meantime.
                        # A client connect bumps _conn_seq and resets the session;
                        # tearing it down here would kill that live session (dark
                        # device). _conn_seq == 0 ⇒ still the startup deferral.
                        if (
                            self._conn_seq == 0
                            and self.openai_service
                            and self.openai_service._websocket
                        ):
                            await self.openai_service._disconnect()
                            logger.info("💤 OpenAI session deferred — will connect on first client")
                    return
            logger.warning("⚠️ Deferred disconnect: service never connected (pipeline may not have started)")

        # ── Session rotation ──────────────────────────────────────────
        async def _session_rotation_loop():
            """Proactively rotate the OpenAI session before the 60-min expiry.

            Runs while a client is connected. Each cycle sleeps for
            ``_rotation_interval`` seconds (default 55 min), then calls
            ``reset_conversation()`` which disconnects the WebSocket,
            reconnects (new 60-min clock), and replays cached context.
            The user hears ~1-2 s of silence during the handshake.
            """
            while True:
                await asyncio.sleep(self._rotation_interval)
                async with self._pipeline_lock:
                    if not (self.openai_service and self.openai_service._websocket):
                        # Client disconnected while we slept — stop rotating
                        logger.info("⏰ Rotation timer fired but no active session — skipping")
                        return
                    logger.info("🔄 Session rotation — resetting OpenAI conversation (%.0f min interval)",
                                self._rotation_interval / 60)
                    try:
                        await self.openai_service.reset_conversation()
                        logger.info("✅ Session rotated — new 60-min clock started")
                    except Exception as e:
                        logger.warning("⚠️ Session rotation failed: %s — will retry next interval", e)

        def _start_rotation_timer():
            """Start (or restart) the rotation background task."""
            if self._rotation_task and not self._rotation_task.done():
                self._rotation_task.cancel()
            self._rotation_task = asyncio.create_task(_session_rotation_loop())

        def _cancel_rotation_timer():
            """Cancel the rotation timer (client disconnected)."""
            if self._rotation_task and not self._rotation_task.done():
                self._rotation_task.cancel()
                self._rotation_task = None

        # ── WebSocket event handlers ──────────────────────────────────
        async def on_client_connected(client_id: str):
            """Handle new client connection."""
            # A newer connection invalidates any in-flight stale teardown.
            self._conn_seq += 1
            # Fresh Genesis prompt (today's date, prompt updates) BEFORE the session
            # (re)connects, so the connect/reset below replays current instructions.
            await self._refresh_instructions()
            await self._ensure_openai_service(client_id=client_id)
            if self.audio_recording_service:
                self.audio_recording_service.start_new_session(client_id)
            _start_rotation_timer()
            # Start tracking idle for this session (closes the WS on genuine idle).
            idle_manager = getattr(self.websocket_handler, "session_idle_manager", None)
            if idle_manager:
                idle_manager.arm()
            # Start a clean noise-gate diagnostic window for this session (the
            # gate is reused across reconnects).
            noise_gate = getattr(self.websocket_handler, "noise_gate", None)
            if noise_gate:
                noise_gate.reset_instrumentation()

        async def on_client_disconnected(client_id: str):
            """Handle client disconnection."""
            # Snapshot the connection counter. If a newer client connects while
            # this teardown is in flight, we must NOT tear down the OpenAI
            # session it has since taken over (the reconnect race).
            seq_at_entry = self._conn_seq
            _cancel_rotation_timer()
            idle_manager = getattr(self.websocket_handler, "session_idle_manager", None)
            if idle_manager:
                idle_manager.disarm()
            if self.session_manager:
                self.session_manager.handle_client_disconnect(client_id, self.openai_service)

            # Persist conversation transcript to Genesis memory (Phase 3D).
            # Fire-and-forget: a slow Genesis must NOT delay the teardown below —
            # a ~6s persist once let this handler's _disconnect() run AFTER a new
            # connection had reset the session, clobbering it. Context is already
            # cached by handle_client_disconnect; store_conversation writes a
            # local fallback on failure, so backgrounding loses nothing.
            if self.genesis_tool_service and self.session_manager:
                cached = self.session_manager.get_cached_context(client_id)
                if cached:
                    messages = cached.get_messages()
                    if messages:
                        task = asyncio.create_task(self._persist_transcript(messages))
                        self._persist_tasks.add(task)
                        task.add_done_callback(self._persist_tasks.discard)

            if self.audio_recording_service:
                self.audio_recording_service.stop_recording()
            # Disconnect from OpenAI to stop the 60-min session clock.
            if self.openai_service is not None:
                if self._pipeline_lock is None:
                    self._pipeline_lock = asyncio.Lock()
                async with self._pipeline_lock:
                    if self._conn_seq != seq_at_entry:
                        # A newer connection took over and reset the shared OpenAI
                        # session while we were tearing down. Disconnecting now
                        # would kill the LIVE session → brain-less, dark device.
                        logger.info(
                            "↩️ Skipping stale OpenAI teardown — newer connection "
                            "took over (seq %d→%d)", seq_at_entry, self._conn_seq,
                        )
                    else:
                        await self.openai_service._disconnect()
                        logger.info("💤 OpenAI session closed — client disconnected")

        self.websocket_handler.setup_event_handlers(
            transport=self.websocket_transport,
            on_client_connected_callback=on_client_connected,
            on_client_disconnected_callback=on_client_disconnected,
        )

        # Schedule deferred disconnect — runs concurrently with runner.run()
        asyncio.create_task(_deferred_disconnect())

        try:
            # Start the pipeline runner - this will start the WebSocket server
            # Based on pipecat-examples: PipelineRunner.run() starts the transport server
            logger.info("✅ Starting WebSocket server and pipeline...")
            await self.runner.run(self.current_task)
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            raise
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        """Cleanup resources."""
        logger.info("Cleaning up application...")

        if self.runner:
            try:
                await self.runner.cancel()
            except Exception as e:
                logger.warning(f"⚠️ Error cancelling runner: {e}")

        if self.websocket_handler:
            try:
                await self.websocket_handler.cleanup()
            except Exception as e:
                logger.warning(f"⚠️ Error cleaning up WebSocket handler: {e}")

        if self.audio_recording_service:
            self.audio_recording_service.cleanup()

        logger.info("✅ Application cleanup complete")


async def main() -> None:
    """Main entry point."""
    app = Application()

    try:
        await app.run()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

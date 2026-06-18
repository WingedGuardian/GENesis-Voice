"""Tool for disconnecting the client when user says goodbye or stop."""
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

from app.ws_control import send_disconnect_then_close

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams
    from pipecat.transports.websocket.server import WebsocketServerTransport

logger = logging.getLogger(__name__)


def get_disconnect_tool_definition() -> dict[str, Any]:
    """Get the tool definition for OpenAI Realtime API."""
    return {
        "type": "function",
        "name": "disconnect_client",
        "description": "Disconnect the voice assistant session when the user says goodbye, farewell, stop, or only thank you without additional questions and wants to end the conversation. Use this when the user explicitly wants to end the conversation or says phrases like 'Auf Wiedersehen', 'Tschüss', 'Stop', 'Beenden', 'Ende', etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "The reason for disconnecting (e.g., 'user_said_goodbye', 'user_requested_stop')",
                    "enum": ["user_requested_stop", "conversation_ended"]
                }
            },
            "required": ["reason"]
        }
    }


async def execute_disconnect_tool(
    arguments: dict[str, Any],
    disconnect_callback: Callable[[], Awaitable[None]] | None
) -> dict[str, Any]:
    """
    Execute the disconnect tool.

    Args:
        arguments: Tool arguments containing the reason
        disconnect_callback: Optional async callback function to disconnect the client

    Returns:
        Result dictionary with success status
    """
    reason = arguments.get("reason", "unknown")
    logger.info(f"🔌 Disconnect tool called with reason: {reason}")

    if not disconnect_callback:
        return {
            "success": False,
            "error": "Disconnect callback not available",
            "reason": reason
        }

    try:
        # Call the disconnect callback
        await disconnect_callback()

        return {
            "success": True,
            "message": "Client disconnected successfully",
            "reason": reason
        }
    except Exception as e:
        logger.error(f"❌ Error executing disconnect tool: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "reason": reason
        }


def create_disconnect_callback(
    transport: Optional["WebsocketServerTransport"],
    reason: str = "user_requested"
) -> Callable[[], Awaitable[None]]:
    """
    Create a disconnect callback that closes the WebSocket connection.

    Args:
        transport: The WebSocket transport instance
        reason: The reason for disconnecting

    Returns:
        Async callback function that closes the connection
    """
    async def disconnect_callback() -> None:
        """Disconnect callback that closes the WebSocket connection."""
        logger.info("🔌 Disconnect tool triggered - closing connection")
        try:
            if transport is None:
                logger.warning("⚠️ No transport available for disconnect")
                return

            from pipecat.transports.websocket.server import WebsocketServerTransport
            if not isinstance(transport, WebsocketServerTransport):
                logger.warning("⚠️ Transport is not a WebsocketServerTransport")
                return

            # WebSocket transport — locate the underlying websocket to close.
            # Deliberately NO transport.disconnect_client() path: WebsocketServer-
            # Transport has no such method (this is already guarded to that type
            # above), and routing through it would bare-close the socket, bypassing
            # send_disconnect_then_close and re-introducing the device spin bug.
            websocket_to_close = None

            # Try the websocket attribute on the transport directly.
            if hasattr(transport, '_websocket') and transport._websocket:
                websocket_to_close = transport._websocket
            elif hasattr(transport, 'websocket') and transport.websocket:
                websocket_to_close = transport.websocket
            elif hasattr(transport, '_connection') and transport._connection:
                websocket_to_close = transport._connection

            # Otherwise, get the websocket from the input processor.
            if not websocket_to_close and hasattr(transport, 'input'):
                input_proc = transport.input()
                if hasattr(input_proc, '_websocket') and input_proc._websocket:
                    websocket_to_close = input_proc._websocket
                elif hasattr(input_proc, 'websocket') and input_proc.websocket:
                    websocket_to_close = input_proc.websocket

            if websocket_to_close:
                # Tell the device to go idle, THEN close — the same contract the
                # idle manager uses (see app.ws_control). A bare close makes the
                # firmware reconnect into a torn-down session and spin forever.
                await send_disconnect_then_close(websocket_to_close, reason=reason)
                logger.info("✅ Closed WebSocket connection")
            else:
                logger.warning("⚠️ Could not find WebSocket connection to close")
                # Try to trigger disconnect event
                if hasattr(transport, 'event_handler'):
                    logger.info("⚠️ Attempting to trigger disconnect event")
        except Exception as e:
            logger.error(f"❌ Error closing connection: {e}", exc_info=True)

    return disconnect_callback


def create_disconnect_tool_handler(
    transport: Optional["WebsocketServerTransport"]
) -> Callable[["FunctionCallParams"], Awaitable[None]]:
    """
    Create a disconnect tool handler for Pipecat's OpenAI Realtime Service.

    Args:
        transport: The WebSocket transport instance

    Returns:
        Async function handler that can be registered with OpenAIRealtimeLLMService
    """
    async def disconnect_tool_handler(params: "FunctionCallParams") -> None:
        """Handle disconnect tool calls."""
        logger.info(f"🔌 Disconnect tool called: {params.function_name} with arguments: {params.arguments}")

        # Get reason from arguments
        reason = params.arguments.get("reason", "user_requested")

        # Create disconnect callback that closes the connection
        disconnect_callback = create_disconnect_callback(transport, reason=reason)

        # Execute the disconnect tool
        result = await execute_disconnect_tool(params.arguments, disconnect_callback)

        # Send result back to OpenAI
        if result.get("success"):
            await params.result_callback(f"Disconnected successfully: {result.get('message', '')}")
        else:
            await params.result_callback(f"Error: {result.get('error', 'Unknown error')}")

    return disconnect_tool_handler

"""DingTalk Stream WebSocket adapter for inbound chatbot messages."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from dingtalk_stream import (
    AckMessage,
    CallbackMessage,
    ChatbotHandler,
    ChatbotMessage,
    DingTalkStreamClient,
)
from dingtalk_stream import Credential as StreamCredential

from src.adapters.dingtalk.message import (
    InboundEvent,
    MessageNormalizationError,
    normalize_chatbot_event,
)
from src.infra.config import DingTalkConfig
from src.infra.log import get_logger

logger = get_logger(__name__)

OnMessage = Callable[[InboundEvent], Awaitable[None]]


class StreamClient(Protocol):
    """Protocol for SDK-compatible stream clients used by the adapter."""

    def register_callback_handler(self, topic: str, handler: ChatbotHandler) -> None:
        """Register a callback handler for one DingTalk Stream topic."""

    async def start(self) -> None:
        """Start the Stream client event loop."""


StreamClientFactory = Callable[[StreamCredential], StreamClient]


class DingTalkStreamAdapter:
    """Build and run the DingTalk Stream client with chatbot callback routing."""

    def __init__(
        self,
        config: DingTalkConfig,
        on_message: OnMessage,
        *,
        client_factory: StreamClientFactory | None = None,
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._client_factory = client_factory or _default_client_factory
        self.client: StreamClient | None = None

    def create_client(self) -> StreamClient:
        """Create a configured SDK client and register the chatbot callback handler."""

        credential = StreamCredential(self._config.app_key, self._config.app_secret)
        client = self._client_factory(credential)
        client.register_callback_handler(
            ChatbotMessage.TOPIC,
            DingTalkChatbotCallbackHandler(self._on_message),
        )
        self.client = client
        return client

    async def start(self) -> None:
        """Connect to DingTalk Stream and process callbacks until cancelled."""

        client = self.create_client()
        logger.info("dingtalk_stream_starting")
        await client.start()


class DingTalkChatbotCallbackHandler(ChatbotHandler):
    """SDK callback handler that normalizes inbound chatbot messages."""

    def __init__(self, on_message: OnMessage) -> None:
        super().__init__()
        self._on_message = on_message

    async def process(self, message: CallbackMessage) -> tuple[int, str]:
        """Normalize, log, and dispatch one DingTalk chatbot callback."""

        try:
            inbound = normalize_chatbot_event(message)
        except MessageNormalizationError as exc:
            logger.warning("dingtalk_inbound_message_invalid", extra={"error": str(exc)})
            return AckMessage.STATUS_BAD_REQUEST, str(exc)

        logger.info(
            "dingtalk_inbound_message",
            extra={
                "msg_id": inbound.msg_id,
                "sender_staff_id": inbound.sender_staff_id,
                "sender_nick": inbound.sender_nick,
                "message_type": inbound.message_type,
                "conversation_type": inbound.conversation_type,
                "conversation_id": inbound.conversation_id,
                "open_conversation_id": inbound.open_conversation_id,
            },
        )

        try:
            await self._on_message(inbound)
        except Exception:
            logger.exception(
                "dingtalk_on_message_failed",
                extra={
                    "msg_id": inbound.msg_id,
                    "conversation_id": inbound.conversation_id,
                },
            )
            return AckMessage.STATUS_SYSTEM_EXCEPTION, "on_message failed"

        return AckMessage.STATUS_OK, "ok"


def _default_client_factory(credential: StreamCredential) -> StreamClient:
    return DingTalkStreamClient(credential, logger=logger)

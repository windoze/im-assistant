"""DingTalk Stream WebSocket adapter for inbound chatbot messages."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from dingtalk_stream import (
    AckMessage,
    CallbackHandler,
    CallbackMessage,
    Card_Callback_Router_Topic,
    ChatbotHandler,
    ChatbotMessage,
    DingTalkStreamClient,
)
from dingtalk_stream import Credential as StreamCredential

from src.adapters.dingtalk.message import (
    CardCallbackEvent,
    InboundEvent,
    MessageNormalizationError,
    normalize_card_callback,
    normalize_chatbot_event,
)
from src.infra.config import DingTalkConfig
from src.infra.log import get_logger

logger = get_logger(__name__)

OnMessage = Callable[[InboundEvent], Awaitable[None]]
OnCardCallback = Callable[[CardCallbackEvent], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]
DEFAULT_RECONNECT_INITIAL_DELAY_SECONDS = 1.0
DEFAULT_RECONNECT_MAX_DELAY_SECONDS = 60.0


class StreamClient(Protocol):
    """Protocol for SDK-compatible stream clients used by the adapter."""

    def register_callback_handler(
        self,
        topic: str,
        handler: ChatbotHandler | CallbackHandler,
    ) -> None:
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
        on_card_callback: OnCardCallback | None = None,
        client_factory: StreamClientFactory | None = None,
        reconnect: bool = True,
        reconnect_initial_delay: float = DEFAULT_RECONNECT_INITIAL_DELAY_SECONDS,
        reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY_SECONDS,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._on_card_callback = on_card_callback
        self._client_factory = client_factory or _default_client_factory
        self._reconnect = reconnect
        self._reconnect_initial_delay = _positive_float(
            reconnect_initial_delay,
            "reconnect_initial_delay",
        )
        self._reconnect_max_delay = max(
            self._reconnect_initial_delay,
            _positive_float(reconnect_max_delay, "reconnect_max_delay"),
        )
        self._sleep = sleep
        self.client: StreamClient | None = None

    def create_client(self) -> StreamClient:
        """Create a configured SDK client and register the chatbot callback handler."""

        credential = StreamCredential(self._config.app_key, self._config.app_secret)
        client = self._client_factory(credential)
        client.register_callback_handler(
            ChatbotMessage.TOPIC,
            DingTalkChatbotCallbackHandler(self._on_message),
        )
        if self._on_card_callback is not None:
            client.register_callback_handler(
                Card_Callback_Router_Topic,
                DingTalkCardCallbackHandler(self._on_card_callback),
            )
        self.client = client
        return client

    async def start(self) -> None:
        """Connect to DingTalk Stream and process callbacks until cancelled."""

        delay = self._reconnect_initial_delay
        attempt = 1
        while True:
            client = self.create_client()
            logger.info("dingtalk_stream_starting", extra={"attempt": attempt})
            try:
                await client.start()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self._reconnect:
                    raise
                logger.exception(
                    "dingtalk_stream_disconnected",
                    extra={"attempt": attempt, "retry_delay_seconds": delay},
                )
            else:
                if not self._reconnect:
                    return
                logger.warning(
                    "dingtalk_stream_stopped",
                    extra={"attempt": attempt, "retry_delay_seconds": delay},
                )

            await self._sleep(delay)
            delay = min(delay * 2, self._reconnect_max_delay)
            attempt += 1


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


class DingTalkCardCallbackHandler(CallbackHandler):
    """SDK callback handler that normalizes interactive-card button callbacks."""

    def __init__(self, on_card_callback: OnCardCallback) -> None:
        super().__init__()
        self._on_card_callback = on_card_callback

    async def process(self, message: CallbackMessage) -> tuple[int, str]:
        """Normalize, log, and dispatch one DingTalk card callback."""

        try:
            callback = normalize_card_callback(message)
        except MessageNormalizationError as exc:
            logger.warning("dingtalk_card_callback_invalid", extra={"error": str(exc)})
            return AckMessage.STATUS_BAD_REQUEST, str(exc)

        logger.info(
            "dingtalk_card_callback",
            extra={
                "correlation_id": callback.correlation_id,
                "responder_id": callback.responder_id,
                "decision": callback.decision,
                "card_instance_id": callback.card_instance_id,
            },
        )

        try:
            await self._on_card_callback(callback)
        except Exception:
            logger.exception(
                "dingtalk_on_card_callback_failed",
                extra={
                    "correlation_id": callback.correlation_id,
                    "responder_id": callback.responder_id,
                },
            )
            return AckMessage.STATUS_SYSTEM_EXCEPTION, "on_card_callback failed"

        return AckMessage.STATUS_OK, "ok"


def _default_client_factory(credential: StreamCredential) -> StreamClient:
    return DingTalkStreamClient(credential, logger=logger)


def _positive_float(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or value <= 0:
        raise ValueError(f"{field_name} must be a positive number")
    return float(value)

"""Per-session asynchronous inboxes for serialized event processing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, Protocol, TypeVar

from src.infra.log import get_logger

logger = get_logger(__name__)


class InboxEvent(Protocol):
    """Inbound event fields required to assign work to a Session inbox."""

    conversation_id: str
    msg_id: str


EventT = TypeVar("EventT", bound=InboxEvent)
EventHandler = Callable[[EventT], Awaitable[None]]


class SessionInbox(Generic[EventT]):
    """Single-worker FIFO queue for one persistent Session."""

    def __init__(self, session_key: str, handler: EventHandler[EventT]) -> None:
        self._session_key = _required_string(session_key, "session_key")
        self._handler = handler
        self._queue: asyncio.Queue[EventT] = asyncio.Queue()
        self._state_lock = asyncio.Lock()
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def session_key(self) -> str:
        """Return the stable key for the Session represented by this inbox."""

        return self._session_key

    async def enqueue(self, event: EventT) -> None:
        """Append one event and ensure exactly one worker is processing this inbox."""

        await self._queue.put(event)
        async with self._state_lock:
            if self._worker_task is None or self._worker_task.done():
                self._worker_task = asyncio.create_task(
                    self._run(),
                    name=f"session-inbox-{self._session_key}",
                )

    async def join(self) -> None:
        """Wait until all events currently queued for this Session are processed."""

        await self._queue.join()

    async def close(self) -> None:
        """Drain the inbox and wait for its worker to exit."""

        await self.join()
        task = self._worker_task
        if task is not None:
            await task

    async def _run(self) -> None:
        while True:
            try:
                event = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                if await self._mark_idle_when_empty():
                    return
                continue
            await self._process(event)

    async def _mark_idle_when_empty(self) -> bool:
        async with self._state_lock:
            if self._queue.empty():
                self._worker_task = None
                return True
            return False

    async def _process(self, event: EventT) -> None:
        logger.debug(
            "session_inbox_event_started",
            extra={
                "session_key": self._session_key,
                "msg_id": event.msg_id,
                "pending": self._queue.qsize(),
            },
        )
        try:
            await self._handler(event)
        except Exception:
            logger.exception(
                "session_inbox_handler_failed",
                extra={"session_key": self._session_key, "msg_id": event.msg_id},
            )
        finally:
            self._queue.task_done()
            logger.debug(
                "session_inbox_event_finished",
                extra={
                    "session_key": self._session_key,
                    "msg_id": event.msg_id,
                    "pending": self._queue.qsize(),
                },
            )


class SessionInboxDispatcher(Generic[EventT]):
    """Route inbound events to one FIFO inbox per DingTalk conversation Session."""

    def __init__(self, handler: EventHandler[EventT]) -> None:
        self._handler = handler
        self._inboxes: dict[str, SessionInbox[EventT]] = {}
        self._state_lock = asyncio.Lock()

    async def enqueue(self, event: EventT) -> None:
        """Queue an event on the inbox for its conversation Session."""

        inbox = await self._get_or_create_inbox(_session_key_from_event(event))
        await inbox.enqueue(event)

    async def drain(self) -> None:
        """Wait until every currently known Session inbox is empty."""

        async with self._state_lock:
            inboxes = tuple(self._inboxes.values())
        await asyncio.gather(*(inbox.join() for inbox in inboxes))

    async def close(self) -> None:
        """Drain all inboxes and wait for active workers to exit."""

        async with self._state_lock:
            inboxes = tuple(self._inboxes.values())
        await asyncio.gather(*(inbox.close() for inbox in inboxes))

    async def _get_or_create_inbox(self, session_key: str) -> SessionInbox[EventT]:
        async with self._state_lock:
            inbox = self._inboxes.get(session_key)
            if inbox is None:
                inbox = SessionInbox(session_key, self._handler)
                self._inboxes[session_key] = inbox
            return inbox


def _session_key_from_event(event: InboxEvent) -> str:
    return _required_string(event.conversation_id, "conversation_id")


def _required_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()

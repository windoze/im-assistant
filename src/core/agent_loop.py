"""Agent loop for persistent multi-turn Session conversations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from src.core.session import Session, SessionState
from src.infra.log import get_logger
from src.infra.store import MessageRecord, MessageRole, SessionRecord

logger = get_logger(__name__)

DEFAULT_HISTORY_LIMIT = 20
AgentRunStatus = Literal["completed"]


class AgentLoopStateError(RuntimeError):
    """Raised when a Session state cannot enter the current agent loop."""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Result produced by one agent-loop turn."""

    reply_text: str
    status: AgentRunStatus = "completed"


class TextCompleter(Protocol):
    """LLM interface consumed by the agent loop."""

    async def complete(self, system: str, messages: Sequence[Mapping[str, str]]) -> str:
        """Return a text completion for the supplied prompt and chat history."""


class AgentLoopStore(Protocol):
    """Persistent store methods required by the agent loop."""

    async def list_recent_messages(self, session_id: str, *, limit: int) -> list[MessageRecord]:
        """Return the newest messages for a Session in chronological order."""

    async def add_message(
        self,
        *,
        session_id: str,
        role: MessageRole,
        content: str,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MessageRecord:
        """Append a message to the persistent Session history."""

    async def upsert_session(self, record: SessionRecord) -> SessionRecord:
        """Persist Session state changes."""


class ToolExecutor(Protocol):
    """Extension point for M3 Claude tool-use execution."""

    async def execute(
        self,
        *,
        session: Session,
        name: str,
        arguments: Mapping[str, Any],
    ) -> str:
        """Execute one model-requested tool call and return text for Claude."""


class AgentLoop:
    """Run one serialized agent turn for a persistent Session."""

    def __init__(
        self,
        store: AgentLoopStore,
        llm_client: TextCompleter,
        *,
        system_prompt: str,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        self._store = store
        self._llm_client = llm_client
        self._system_prompt = _non_empty_string(system_prompt, "system_prompt")
        self._history_limit = _positive_int(history_limit, "history_limit")
        self._tool_executor = tool_executor

    async def run(
        self,
        session: Session,
        user_text: str,
        *,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> AgentRunResult:
        """Load context, complete one LLM turn, persist history, and return the reply."""

        normalized_text = _non_empty_string(user_text, "user_text")
        _ensure_idle(session)

        await self._set_session_state(session, "RunningAgent")
        try:
            history = await self._store.list_recent_messages(
                session.session_id,
                limit=self._history_limit,
            )
            llm_messages = _llm_messages_from_history(history)
            llm_messages.append({"role": "user", "content": normalized_text})
            logger.debug(
                "agent_loop_started",
                extra={
                    "session_id": session.session_id,
                    "history_messages": len(llm_messages) - 1,
                },
            )

            reply_text = await self._llm_client.complete(self._system_prompt, llm_messages)
            await self._persist_completed_turn(
                session,
                user_text=normalized_text,
                reply_text=reply_text,
                actor_id=actor_id,
                provider_message_id=provider_message_id,
            )
            logger.debug(
                "agent_loop_completed",
                extra={"session_id": session.session_id, "status": "completed"},
            )
            return AgentRunResult(reply_text=reply_text)
        finally:
            await self._set_session_state(session, "Idle")

    async def _persist_completed_turn(
        self,
        session: Session,
        *,
        user_text: str,
        reply_text: str,
        actor_id: str | None,
        provider_message_id: str | None,
    ) -> None:
        await self._store.add_message(
            session_id=session.session_id,
            role="user",
            content=user_text,
            actor_id=actor_id or session.actor.id,
            provider_message_id=provider_message_id,
            metadata={"source": "dingtalk"},
        )
        await self._store.add_message(
            session_id=session.session_id,
            role="assistant",
            content=reply_text,
            actor_id=session.bot.id,
            metadata={"status": "completed"},
        )

    async def _set_session_state(self, session: Session, state: SessionState) -> None:
        await self._store.upsert_session(_session_record_with_state(session, state))


def _ensure_idle(session: Session) -> None:
    if session.state == "Idle":
        return
    if session.state == "AwaitingInteraction":
        raise AgentLoopStateError("Session resume from AwaitingInteraction is reserved for M5")
    raise AgentLoopStateError(f"Session cannot enter agent loop from state: {session.state}")


def _llm_messages_from_history(history: Sequence[MessageRecord]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for record in history:
        if record.role in ("user", "assistant"):
            messages.append({"role": record.role, "content": record.content})
    return messages


def _session_record_with_state(session: Session, state: SessionState) -> SessionRecord:
    return SessionRecord(
        session_id=session.session_id,
        conversation_id=session.conversation_id,
        kind=session.kind,
        bot_id=session.bot.id,
        principal_id=session.principal.id,
        actor_id=session.actor.id,
        state=state,
        lifecycle=session.lifecycle,
        context=session.context,
        created_at=session.created_at,
    )


def _non_empty_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value

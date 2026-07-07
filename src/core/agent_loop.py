"""Agent loop for persistent multi-turn Session conversations."""

from __future__ import annotations

import inspect
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, Protocol

from src.capabilities import (
    AuthorizationResolution,
    Capability,
    CapabilityChannelContext,
    CapabilityRegistry,
    CredentialContext,
    Denied,
    Granted,
    NeedsConsent,
    Requirement,
    can_use,
)
from src.core.interrupt import InterruptResolution, SessionInterrupt, SessionInterruptManager
from src.core.session import Actor, BotIdentity, Principal, Session, SessionState
from src.infra.log import get_logger
from src.infra.metrics import increment_counter
from src.infra.store import (
    MessageRecord,
    MessageRole,
    PendingInteractionRecord,
    PendingInteractionStatus,
    SessionRecord,
)

logger = get_logger(__name__)

DEFAULT_HISTORY_LIMIT = 20
DEFAULT_MAX_TOOL_ITERATIONS = 8
AgentRunStatus = Literal["completed", "awaiting_interaction"]
InteractionCancellationReason = Literal[
    "superseded_by_new_message",
    "timeout",
    "command_cancelled",
]


class AgentLoopStateError(RuntimeError):
    """Raised when a Session state cannot enter the current agent loop."""


class AgentLoopToolError(RuntimeError):
    """Raised when Claude tool-use orchestration cannot continue safely."""


class AgentLoopConsentRequired(RuntimeError):
    """Raised internally when a tool call must suspend for OAuth consent."""

    def __init__(
        self,
        *,
        consent: NeedsConsent,
        capability: Capability,
        tool_use: ToolUseRequest,
    ) -> None:
        self.consent = consent
        self.capability = capability
        self.tool_use = tool_use
        super().__init__(f"Consent required for capability: {capability.name}")


class AgentLoopConfirmRequired(RuntimeError):
    """Raised internally when a capability must suspend for a human confirmation."""

    def __init__(
        self,
        *,
        correlation_id: str,
        action: str,
        details: Mapping[str, Any],
        capability: Capability,
        tool_use: ToolUseRequest,
        source: Literal["handler", "runtime_sensitivity"] = "handler",
    ) -> None:
        self.correlation_id = correlation_id
        self.action = action
        self.details = _plain_json_object(details, "confirm.details")
        self.capability = capability
        self.tool_use = tool_use
        self.source = source
        super().__init__(f"Confirmation required for capability: {capability.name}")


class CapabilityServiceError(RuntimeError):
    """Raised when a capability requires a runtime service that was not provided."""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Result produced by one agent-loop turn."""

    reply_text: str
    status: AgentRunStatus = "completed"
    pending_interaction: PendingInteractionInfo | None = None


@dataclass(frozen=True, slots=True)
class PendingInteractionInfo:
    """Public metadata for an interaction that suspended an agent turn."""

    correlation_id: str
    kind: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class InteractionCancellationResult:
    """Runtime cancellation result for a superseded or timed-out interaction."""

    correlation_id: str
    kind: str
    reason: InteractionCancellationReason
    notice_text: str
    session: Session


@dataclass(frozen=True, slots=True)
class ConfirmCallbackResult:
    """Result of resolving a confirm-card callback without invoking the LLM."""

    correlation_id: str
    status: Literal["confirmed", "cancelled"]
    tool_result: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityExecutionContext:
    """Runtime context injected into capability handlers."""

    session: Session
    capability: Capability
    services: Mapping[str, object] = field(default_factory=dict)
    credentials: CredentialContext | None = None
    confirmation: CapabilityConfirmation | None = None

    def __post_init__(self) -> None:
        """Freeze service mappings so handlers cannot mutate runtime wiring."""

        object.__setattr__(self, "services", MappingProxyType(dict(self.services)))
        if self.credentials is None:
            object.__setattr__(self, "credentials", CredentialContext.for_session(self.session))

    def require_service(self, name: str) -> object:
        """Return a named runtime service or raise a clear handler-facing error."""

        service_name = _non_empty_string(name, "service_name")
        if service_name not in self.services:
            raise CapabilityServiceError(f"Capability service is not configured: {service_name}")
        return self.services[service_name]

    @property
    def user(self):
        """Return the current actor credential facade as `ctx.user`."""

        if self.credentials is None:
            raise CapabilityServiceError("Credential context is not configured")
        return self.credentials.user

    @property
    def group(self):
        """Return the current group credential facade as `ctx.group`, if any."""

        if self.credentials is None:
            raise CapabilityServiceError("Credential context is not configured")
        return self.credentials.group

    def require_user_token(self, service: str) -> str:
        """Return a granted OBO token for a service."""

        if self.credentials is None:
            raise CapabilityServiceError("Credential context is not configured")
        return self.credentials.require_user_token(service)

    async def confirm(self, action: str, details: Mapping[str, Any]) -> bool:
        """Ask the expected DingTalk responder to approve a tool action before it runs."""

        if self.confirmation is None:
            raise CapabilityServiceError("Confirmation is not configured for this capability")
        return await self.confirmation.confirm(action, details)


@dataclass(frozen=True, slots=True)
class ToolUseRequest:
    """One Claude tool-use request extracted from assistant response content."""

    id: str
    name: str
    arguments: Mapping[str, Any]


class CapabilityConfirmation(Protocol):
    """Confirmation hook exposed as `ctx.confirm(...)` to capability handlers."""

    async def confirm(self, action: str, details: Mapping[str, Any]) -> bool:
        """Return True once the requested operation has been approved."""


class ConfirmCardSender(Protocol):
    """DingTalk card sender consumed by the agent loop confirm primitive."""

    async def send_confirm_card(
        self,
        *,
        conversation_type: int,
        conversation_id: str,
        responder_user_id: str,
        action: str,
        details: Mapping[str, Any],
        correlation_id: str,
        open_conversation_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> object:
        """Create and deliver a confirm/cancel card."""


class TextCompleter(Protocol):
    """LLM interface consumed by the agent loop."""

    async def complete(self, system: str, messages: Sequence[Mapping[str, Any]]) -> str:
        """Return a text completion for the supplied prompt and chat history."""


class ToolUseResponse(Protocol):
    """Normalized LLM response shape needed by the tool-use loop."""

    content: Sequence[Mapping[str, Any]]
    text: str


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

    async def create_pending_interaction(
        self,
        record: PendingInteractionRecord,
    ) -> PendingInteractionRecord:
        """Persist one pending Session interaction."""

    async def get_pending_interaction(
        self,
        correlation_id: str,
    ) -> PendingInteractionRecord | None:
        """Return one pending Session interaction."""

    async def get_pending_interaction_for_session(
        self,
        session_id: str,
    ) -> PendingInteractionRecord | None:
        """Return the active pending Session interaction."""

    async def resolve_pending_interaction(
        self,
        correlation_id: str,
        *,
        status: PendingInteractionStatus,
        resolution: Mapping[str, Any],
        resolved_at: datetime | None = None,
    ) -> PendingInteractionRecord:
        """Persist a pending-interaction resolution."""

    async def get_session(self, session_id: str) -> SessionRecord | None:
        """Return one persisted Session."""


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


class CapabilityAuthorizer(Protocol):
    """Authorization gate consumed before executing capability handlers."""

    async def resolve(
        self,
        requirement: Requirement,
        actor: object,
        mode: str,
        *,
        principal_id: str | None = None,
        session_id: str | None = None,
    ) -> AuthorizationResolution:
        """Resolve one capability requirement for a Session actor."""


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
        capability_registry: CapabilityRegistry | None = None,
        capability_registry_factory: Callable[[Session], CapabilityRegistry] | None = None,
        channel_enabled_capabilities: Mapping[str, Sequence[str]] | None = None,
        capability_services: Mapping[str, object] | None = None,
        authorizer: CapabilityAuthorizer | None = None,
        interrupt_manager: SessionInterruptManager | None = None,
        confirm_card_sender: ConfirmCardSender | None = None,
        confirm_timeout_seconds: int = 1800,
        confirm_id_factory: Callable[[], str] | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
    ) -> None:
        if capability_registry is not None and capability_registry_factory is not None:
            raise ValueError(
                "capability_registry and capability_registry_factory cannot both be provided"
            )
        self._store = store
        self._llm_client = llm_client
        self._system_prompt = _non_empty_string(system_prompt, "system_prompt")
        self._history_limit = _positive_int(history_limit, "history_limit")
        self._tool_executor = tool_executor
        self._capability_registry = capability_registry
        self._capability_registry_factory = capability_registry_factory
        self._channel_enabled_capabilities = _channel_enabled_capabilities(
            channel_enabled_capabilities or {}
        )
        self._capability_services = MappingProxyType(dict(capability_services or {}))
        self._authorizer = authorizer
        self._interrupt_manager = interrupt_manager or SessionInterruptManager(
            store,
            now_factory=now_factory,
        )
        self._confirm_card_sender = confirm_card_sender
        self._confirm_timeout_seconds = _positive_int(
            confirm_timeout_seconds,
            "confirm_timeout_seconds",
        )
        self._confirm_id_factory = confirm_id_factory or _default_confirm_id
        self._now_factory = now_factory
        self._max_tool_iterations = _positive_int(max_tool_iterations, "max_tool_iterations")

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
        suspended = False
        try:
            history = await self._store.list_recent_messages(
                session.session_id,
                limit=self._history_limit,
            )
            llm_messages = _llm_messages_from_history(history)
            llm_messages.append({"role": "user", "content": normalized_text})
            visible_capabilities = self._visible_capabilities(session)
            logger.debug(
                "agent_loop_started",
                extra={
                    "session_id": session.session_id,
                    "history_messages": len(llm_messages) - 1,
                    "tools": len(visible_capabilities),
                },
            )

            try:
                if visible_capabilities:
                    reply_text = await self._complete_with_tools(
                        session,
                        llm_messages,
                        visible_capabilities,
                    )
                else:
                    reply_text = await self._llm_client.complete(self._system_prompt, llm_messages)
            except AgentLoopConsentRequired as exc:
                reply_text = _consent_reply_text(exc.consent)
                interrupt = await self._interrupt_manager.create(
                    session,
                    kind="consent",
                    correlation_id=exc.consent.pending.nonce,
                    responder=session.actor.id,
                    expires_at=exc.consent.pending.expires_at,
                    payload=_consent_interrupt_payload(exc),
                )
                suspended = True
                await self._persist_suspended_turn(
                    session,
                    user_text=normalized_text,
                    reply_text=reply_text,
                    actor_id=actor_id,
                    provider_message_id=provider_message_id,
                    consent=exc.consent,
                    capability=exc.capability,
                    tool_use=exc.tool_use,
                    interrupt_expires_at=interrupt.expires_at.isoformat(),
                )
                logger.info(
                    "agent_loop_awaiting_consent",
                    extra={
                        "session_id": session.session_id,
                        "capability": exc.capability.name,
                        "service": exc.consent.pending.service,
                        "scopes": list(exc.consent.pending.scopes),
                    },
                )
                return AgentRunResult(
                    reply_text=reply_text,
                    status="awaiting_interaction",
                    pending_interaction=_pending_interaction_info(interrupt),
                )
            except AgentLoopConfirmRequired as exc:
                interrupt = await self._interrupt_manager.create(
                    session,
                    kind="confirm",
                    correlation_id=exc.correlation_id,
                    responder=session.actor.id,
                    expires_at=self._confirm_expires_at(),
                    payload=_confirm_interrupt_payload(session, exc),
                )
                try:
                    await self._send_confirm_card(session, exc, expires_at=interrupt.expires_at)
                except Exception:
                    await self._interrupt_manager.cancel(
                        interrupt.correlation_id,
                        "card_send_failed",
                        {"approved": False},
                        responder=session.actor.id,
                    )
                    raise
                reply_text = _confirm_reply_text(exc)
                suspended = True
                await self._persist_confirm_suspended_turn(
                    session,
                    user_text=normalized_text,
                    reply_text=reply_text,
                    actor_id=actor_id,
                    provider_message_id=provider_message_id,
                    confirm=exc,
                    interrupt_expires_at=interrupt.expires_at.isoformat(),
                )
                logger.info(
                    "agent_loop_awaiting_confirm",
                    extra={
                        "session_id": session.session_id,
                        "capability": exc.capability.name,
                        "correlation_id": exc.correlation_id,
                    },
                )
                return AgentRunResult(
                    reply_text=reply_text,
                    status="awaiting_interaction",
                    pending_interaction=_pending_interaction_info(interrupt),
                )
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
            if not suspended:
                await self._set_session_state(session, "Idle")

    async def resume_interaction(
        self,
        correlation_id: str,
        reply: Mapping[str, Any] | None = None,
        *,
        responder: str,
    ) -> InterruptResolution:
        """Resolve a pending Session interaction and restore its Session to Idle."""

        return await self._interrupt_manager.resolve(
            correlation_id,
            reply,
            responder=responder,
        )

    async def cancel_pending_interaction_for_session(
        self,
        session: Session,
        *,
        reason: InteractionCancellationReason,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> InteractionCancellationResult | None:
        """Cancel the active pending interaction for a Session without invoking the LLM."""

        pending = await self._store.get_pending_interaction_for_session(session.session_id)
        if pending is None:
            return None
        return await self._cancel_pending_interaction_record(
            pending,
            reason=reason,
            actor_id=actor_id,
            provider_message_id=provider_message_id,
        )

    async def cancel_pending_interaction(
        self,
        correlation_id: str,
        *,
        reason: InteractionCancellationReason,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> InteractionCancellationResult | None:
        """Cancel one pending interaction by correlation id without invoking the LLM."""

        pending = await self._store.get_pending_interaction(correlation_id)
        if pending is None or pending.status != "pending":
            return None
        return await self._cancel_pending_interaction_record(
            pending,
            reason=reason,
            actor_id=actor_id,
            provider_message_id=provider_message_id,
        )

    async def _cancel_pending_interaction_record(
        self,
        pending: PendingInteractionRecord,
        *,
        reason: InteractionCancellationReason,
        actor_id: str | None,
        provider_message_id: str | None,
    ) -> InteractionCancellationResult | None:
        """Cancel a loaded pending interaction and persist the silent closeout."""

        effective_reason = _effective_cancellation_reason(
            pending,
            reason,
            now=_to_utc(self._now_factory()),
        )
        if effective_reason == "timeout" and _to_utc(self._now_factory()) < _to_utc(
            pending.expires_at
        ):
            return None

        responder = actor_id or _runtime_responder(effective_reason, pending)
        resolution_payload = _cancellation_resolution_payload(
            effective_reason,
            actor_id=actor_id,
            provider_message_id=provider_message_id,
        )
        await self._interrupt_manager.cancel(
            pending.correlation_id,
            effective_reason,
            resolution_payload,
            responder=responder,
            require_responder=False,
            allow_expired=True,
        )

        notice_text = _cancellation_notice_text(pending, effective_reason)
        await self._persist_interaction_cancellation(
            pending,
            reason=effective_reason,
            notice_text=notice_text,
            actor_id=actor_id,
            provider_message_id=provider_message_id,
        )
        restored_record = await self._store.get_session(pending.session_id)
        if restored_record is None:
            raise AgentLoopStateError(f"Cancelled Session no longer exists: {pending.session_id}")
        return InteractionCancellationResult(
            correlation_id=pending.correlation_id,
            kind=pending.kind,
            reason=effective_reason,
            notice_text=notice_text,
            session=_session_from_record(restored_record),
        )

    async def resolve_confirm_callback(
        self,
        correlation_id: str,
        *,
        responder: str,
        approved: bool,
        callback_payload: Mapping[str, Any] | None = None,
    ) -> ConfirmCallbackResult:
        """Resolve a confirm-card callback and execute the deferred tool only on approval."""

        pending = await self._store.get_pending_interaction(correlation_id)
        if pending is None or pending.status != "pending":
            raise AgentLoopStateError(f"No pending confirm interaction: {correlation_id}")
        if pending.kind != "confirm":
            raise AgentLoopStateError(f"Pending interaction is not confirm: {correlation_id}")

        resolution_payload = {
            "approved": approved,
            "callback": _plain_json_object(callback_payload or {}, "callback_payload"),
        }
        if not approved:
            await self._interrupt_manager.cancel(
                correlation_id,
                "user_cancelled",
                resolution_payload,
                responder=responder,
            )
            return ConfirmCallbackResult(correlation_id=correlation_id, status="cancelled")

        await self._interrupt_manager.resolve(
            correlation_id,
            resolution_payload,
            responder=responder,
        )
        tool_result = await self._execute_confirmed_tool(pending.payload)
        await self._store.add_message(
            session_id=pending.session_id,
            role="tool",
            content=tool_result,
            actor_id=pending.payload.get("capability")
            if isinstance(pending.payload.get("capability"), str)
            else None,
            metadata={
                "status": "confirm_executed",
                "kind": "confirm",
                "correlation_id": correlation_id,
            },
        )
        return ConfirmCallbackResult(
            correlation_id=correlation_id,
            status="confirmed",
            tool_result=tool_result,
        )

    async def _persist_interaction_cancellation(
        self,
        pending: PendingInteractionRecord,
        *,
        reason: InteractionCancellationReason,
        notice_text: str,
        actor_id: str | None,
        provider_message_id: str | None,
    ) -> None:
        metadata = {
            "status": "interaction_cancelled",
            "kind": pending.kind,
            "correlation_id": pending.correlation_id,
            "reason": reason,
        }
        await self._store.add_message(
            session_id=pending.session_id,
            role="system",
            content=notice_text,
            actor_id=actor_id,
            provider_message_id=provider_message_id,
            metadata={**metadata, "source": "runtime"},
        )
        await self._store.add_message(
            session_id=pending.session_id,
            role="tool",
            content=_cancelled_tool_result_text(pending, reason),
            actor_id=_pending_capability_name(pending),
            metadata=metadata,
        )

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

    async def _persist_suspended_turn(
        self,
        session: Session,
        *,
        user_text: str,
        reply_text: str,
        actor_id: str | None,
        provider_message_id: str | None,
        consent: NeedsConsent,
        capability: Capability,
        tool_use: ToolUseRequest,
        interrupt_expires_at: str,
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
            metadata={
                "status": "awaiting_interaction",
                "kind": "consent",
                "capability": capability.name,
                "tool_use_id": tool_use.id,
                "service": consent.pending.service,
                "scopes": list(consent.pending.scopes),
                "pending_nonce": consent.pending.nonce,
                "expires_at": interrupt_expires_at,
                "reason": consent.reason,
            },
        )

    async def _persist_confirm_suspended_turn(
        self,
        session: Session,
        *,
        user_text: str,
        reply_text: str,
        actor_id: str | None,
        provider_message_id: str | None,
        confirm: AgentLoopConfirmRequired,
        interrupt_expires_at: str,
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
            metadata={
                "status": "awaiting_interaction",
                "kind": "confirm",
                "capability": confirm.capability.name,
                "tool_use_id": confirm.tool_use.id,
                "correlation_id": confirm.correlation_id,
                "action": confirm.action,
                "details": dict(confirm.details),
                "expires_at": interrupt_expires_at,
            },
        )

    def _visible_capabilities(self, session: Session) -> list[Capability]:
        registry = self._registry_for_session(session)
        if registry is None:
            return []

        channel = _channel_context_for_session(session, self._channel_enabled_capabilities)
        return [
            capability
            for capability in registry.list()
            if (self._tool_executor is not None or capability.handler is not None)
            and can_use(capability, session.kind, session.actor, channel)
        ]

    def _registry_for_session(self, session: Session) -> CapabilityRegistry | None:
        if self._capability_registry_factory is not None:
            registry = self._capability_registry_factory(session)
            if not isinstance(registry, CapabilityRegistry):
                raise TypeError("capability_registry_factory must return a CapabilityRegistry")
            return registry
        return self._capability_registry

    async def _complete_with_tools(
        self,
        session: Session,
        llm_messages: Sequence[Mapping[str, Any]],
        capabilities: Sequence[Capability],
    ) -> str:
        messages = [dict(message) for message in llm_messages]
        tools = [_claude_tool_definition(capability) for capability in capabilities]
        capabilities_by_name = {capability.name: capability for capability in capabilities}

        for _ in range(self._max_tool_iterations):
            response = await self._create_tool_message(messages, tools)
            response_content = [dict(block) for block in response.content]
            tool_uses = _tool_uses_from_content(response_content)
            if not tool_uses:
                reply_text = response.text.strip()
                if reply_text == "":
                    raise AgentLoopToolError("Claude response did not include final text")
                return reply_text

            messages.append({"role": "assistant", "content": response_content})
            tool_results = [
                await self._tool_result_for_call(session, tool_use, capabilities_by_name)
                for tool_use in tool_uses
            ]
            messages.append({"role": "user", "content": tool_results})

        raise AgentLoopToolError("Claude tool loop exceeded max_tool_iterations")

    async def _create_tool_message(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ToolUseResponse:
        create_message = getattr(self._llm_client, "create_message", None)
        if not callable(create_message):
            raise AgentLoopToolError(
                "llm_client must support create_message when capabilities are visible"
            )
        return await create_message(self._system_prompt, messages, tools=tools)

    async def _tool_result_for_call(
        self,
        session: Session,
        tool_use: ToolUseRequest,
        capabilities: Mapping[str, Capability],
    ) -> dict[str, Any]:
        try:
            result = await self._execute_tool_call(session, tool_use, capabilities)
            _record_tool_metric(session, tool_use.name, "completed")
            return {
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": result,
            }
        except AgentLoopConsentRequired:
            _record_tool_metric(session, tool_use.name, "awaiting_consent")
            raise
        except AgentLoopConfirmRequired:
            _record_tool_metric(session, tool_use.name, "awaiting_confirm")
            raise
        except Exception as exc:
            _record_tool_metric(session, tool_use.name, "failed", error_type=type(exc).__name__)
            _record_error_metric("agent_loop_tool", exc)
            logger.exception(
                "agent_loop_tool_execution_failed",
                extra={
                    "session_id": session.session_id,
                    "tool_name": tool_use.name,
                    "tool_use_id": tool_use.id,
                },
            )
            return {
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": f"Tool {tool_use.name} failed: {exc}",
                "is_error": True,
            }

    async def _execute_tool_call(
        self,
        session: Session,
        tool_use: ToolUseRequest,
        capabilities: Mapping[str, Capability],
    ) -> str:
        capability = capabilities.get(tool_use.name)
        if capability is None:
            raise AgentLoopToolError(f"Tool is not available in this Session: {tool_use.name}")
        credentials = await self._credential_context_for_capability(session, capability, tool_use)
        if _requires_runtime_confirm(capability):
            raise AgentLoopConfirmRequired(
                correlation_id=self._confirm_id_factory(),
                action=_runtime_confirm_action(capability),
                details=_runtime_confirm_details(session, capability, tool_use),
                capability=capability,
                tool_use=tool_use,
                source="runtime_sensitivity",
            )
        if self._tool_executor is not None:
            return _tool_result_text(
                await self._tool_executor.execute(
                    session=session,
                    name=tool_use.name,
                    arguments=tool_use.arguments,
                )
            )

        return await _execute_capability_handler(
            session,
            capability,
            tool_use.arguments,
            services=self._capability_services,
            credentials=credentials,
            confirmation=_RequestingCapabilityConfirmation(
                correlation_id=self._confirm_id_factory(),
                capability=capability,
                tool_use=tool_use,
            ),
        )

    async def _credential_context_for_capability(
        self,
        session: Session,
        capability: Capability,
        tool_use: ToolUseRequest,
    ) -> CredentialContext:
        if not capability.requires:
            return CredentialContext.for_session(session)
        if self._authorizer is None:
            raise AgentLoopToolError(
                f"Capability requires authorization but no Authorizer is configured: "
                f"{capability.name}"
            )

        handles = []
        for requirement in capability.requires:
            resolution = await self._authorizer.resolve(
                requirement,
                session.actor,
                session.kind,
                principal_id=session.principal.id,
                session_id=session.session_id,
            )
            if isinstance(resolution, Granted):
                handles.append(resolution.handle)
            elif isinstance(resolution, NeedsConsent):
                raise AgentLoopConsentRequired(
                    consent=resolution,
                    capability=capability,
                    tool_use=tool_use,
                )
            elif isinstance(resolution, Denied):
                raise AgentLoopToolError(
                    f"Capability {capability.name} denied by Authorizer: {resolution.reason}"
                )
            else:
                raise AgentLoopToolError("Authorizer returned an unsupported resolution")
        return CredentialContext.for_session(session, handles=handles)

    async def _execute_confirmed_tool(self, payload: Mapping[str, Any]) -> str:
        session = await self._session_from_confirm_payload(payload)
        capability_name = _non_empty_string(payload.get("capability"), "confirm.capability")
        tool_use = ToolUseRequest(
            id=_non_empty_string(payload.get("tool_use_id"), "confirm.tool_use_id"),
            name=capability_name,
            arguments=_plain_json_object(
                _mapping_value(payload, "arguments"),
                "confirm.arguments",
            ),
        )
        capabilities = {
            capability.name: capability for capability in self._visible_capabilities(session)
        }
        capability = capabilities.get(capability_name)
        if capability is None:
            raise AgentLoopToolError(f"Confirmed tool is no longer available: {capability_name}")
        credentials = await self._credential_context_for_capability(session, capability, tool_use)
        try:
            if self._tool_executor is not None:
                result = _tool_result_text(
                    await self._tool_executor.execute(
                        session=session,
                        name=tool_use.name,
                        arguments=tool_use.arguments,
                    )
                )
            else:
                result = await _execute_capability_handler(
                    session,
                    capability,
                    tool_use.arguments,
                    services=self._capability_services,
                    credentials=credentials,
                    confirmation=_confirmation_for_confirmed_tool(payload),
                )
        except Exception as exc:
            _record_tool_metric(
                session,
                tool_use.name,
                "confirmed_failed",
                error_type=type(exc).__name__,
            )
            _record_error_metric("agent_loop_confirmed_tool", exc)
            raise
        _record_tool_metric(session, tool_use.name, "confirmed_completed")
        return result if result.strip() else "confirmed tool completed"

    async def _session_from_confirm_payload(self, payload: Mapping[str, Any]) -> Session:
        session_id = _non_empty_string(payload.get("session_id"), "confirm.session_id")
        record = await self._store.get_session(session_id)
        if record is None:
            raise AgentLoopStateError(f"Confirmed Session no longer exists: {session_id}")
        actor_id = _non_empty_string(record.actor_id, "session.actor_id")
        return Session(
            session_id=record.session_id,
            conversation_id=record.conversation_id,
            kind=record.kind,
            bot=_bot_identity(record.bot_id),
            principal=_principal(record.kind, record.principal_id),
            actor=_actor(actor_id),
            context=record.context,
            state=record.state,
            lifecycle=record.lifecycle,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    async def _send_confirm_card(
        self,
        session: Session,
        confirm: AgentLoopConfirmRequired,
        *,
        expires_at: datetime,
    ) -> None:
        if self._confirm_card_sender is None:
            raise CapabilityServiceError("Confirm card sender is not configured")
        await self._confirm_card_sender.send_confirm_card(
            conversation_type=1 if session.kind == "dm" else 2,
            conversation_id=session.conversation_id,
            open_conversation_id=_session_open_conversation_id(session),
            responder_user_id=session.actor.id,
            action=confirm.action,
            details=confirm.details,
            correlation_id=confirm.correlation_id,
            expires_at=expires_at,
        )

    def _confirm_expires_at(self) -> datetime:
        return _to_utc(self._now_factory()) + timedelta(seconds=self._confirm_timeout_seconds)

    async def _set_session_state(
        self,
        session: Session,
        state: SessionState,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        await self._store.upsert_session(
            _session_record_with_state(session, state, context=context)
        )


def _ensure_idle(session: Session) -> None:
    if session.state == "Idle":
        return
    if session.state == "AwaitingInteraction":
        raise AgentLoopStateError("Session must resolve AwaitingInteraction before agent loop")
    raise AgentLoopStateError(f"Session cannot enter agent loop from state: {session.state}")


def _llm_messages_from_history(history: Sequence[MessageRecord]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for record in history:
        if record.role in ("user", "assistant"):
            messages.append({"role": record.role, "content": record.content})
    return messages


def _channel_context_for_session(
    session: Session,
    channel_enabled_capabilities: Mapping[str, Sequence[str]],
) -> CapabilityChannelContext | None:
    if session.kind != "group":
        return None
    channel_id = _group_channel_id(session)
    return CapabilityChannelContext(
        id=channel_id,
        enabled_capabilities=channel_enabled_capabilities.get(channel_id, ()),
    )


def _group_channel_id(session: Session) -> str:
    open_conversation_id = session.context.get("open_conversation_id")
    if isinstance(open_conversation_id, str) and open_conversation_id.strip() != "":
        return open_conversation_id.strip()
    return session.conversation_id


def _claude_tool_definition(capability: Capability) -> dict[str, Any]:
    return {
        "name": capability.name,
        "description": _capability_description(capability),
        "input_schema": _plain_json_object(capability.input_schema, "input_schema"),
    }


def _capability_description(capability: Capability) -> str:
    if capability.description is not None:
        return capability.description
    if capability.handler is not None:
        doc = inspect.getdoc(capability.handler)
        if doc is not None and doc.strip() != "":
            return doc.strip().splitlines()[0]
    return f"Capability {capability.name}"


def _tool_uses_from_content(content: Sequence[Mapping[str, Any]]) -> list[ToolUseRequest]:
    tool_uses: list[ToolUseRequest] = []
    for block in content:
        if block.get("type") != "tool_use":
            continue
        raw_arguments = block.get("input", {})
        if raw_arguments is None:
            raw_arguments = {}
        if not isinstance(raw_arguments, Mapping):
            raise AgentLoopToolError("Claude tool_use input must be a mapping")
        tool_uses.append(
            ToolUseRequest(
                id=_non_empty_string(block.get("id"), "tool_use.id"),
                name=_non_empty_string(block.get("name"), "tool_use.name"),
                arguments=dict(raw_arguments),
            )
        )
    return tool_uses


async def _execute_capability_handler(
    session: Session,
    capability: Capability,
    arguments: Mapping[str, Any],
    *,
    services: Mapping[str, object],
    credentials: CredentialContext,
    confirmation: CapabilityConfirmation,
) -> str:
    if capability.handler is None:
        raise AgentLoopToolError(f"Capability has no handler: {capability.name}")

    context = CapabilityExecutionContext(
        session=session,
        capability=capability,
        services=services,
        credentials=credentials,
        confirmation=confirmation,
    )
    result = capability.handler(context, **dict(arguments))
    if inspect.isawaitable(result):
        result = await result
    return _tool_result_text(result)


@dataclass(frozen=True, slots=True)
class _RequestingCapabilityConfirmation:
    correlation_id: str
    capability: Capability
    tool_use: ToolUseRequest

    async def confirm(self, action: str, details: Mapping[str, Any]) -> bool:
        raise AgentLoopConfirmRequired(
            correlation_id=self.correlation_id,
            action=_non_empty_string(action, "confirm.action"),
            details=_plain_json_object(details, "confirm.details"),
            capability=self.capability,
            tool_use=self.tool_use,
            source="handler",
        )


@dataclass(frozen=True, slots=True)
class _ApprovedCapabilityConfirmation:
    action: str
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", _non_empty_string(self.action, "confirm.action"))
        object.__setattr__(
            self,
            "details",
            _plain_json_object(self.details, "confirm.details"),
        )

    async def confirm(self, action: str, details: Mapping[str, Any]) -> bool:
        normalized_action = _non_empty_string(action, "confirm.action")
        normalized_details = _plain_json_object(details, "confirm.details")
        if normalized_action != self.action or normalized_details != self.details:
            raise AgentLoopToolError("Confirmed tool changed its requested action details")
        return True


@dataclass(frozen=True, slots=True)
class _PreApprovedCapabilityConfirmation:
    async def confirm(self, action: str, details: Mapping[str, Any]) -> bool:
        _non_empty_string(action, "confirm.action")
        _plain_json_object(details, "confirm.details")
        return True


def _confirmation_for_confirmed_tool(payload: Mapping[str, Any]) -> CapabilityConfirmation:
    if payload.get("confirm_source") == "runtime_sensitivity":
        return _PreApprovedCapabilityConfirmation()
    return _ApprovedCapabilityConfirmation(
        action=_non_empty_string(payload.get("action"), "confirm.action"),
        details=_plain_json_object(_mapping_value(payload, "details"), "confirm.details"),
    )


def _requires_runtime_confirm(capability: Capability) -> bool:
    return capability.sensitivity.lower() == "high"


def _runtime_confirm_action(capability: Capability) -> str:
    return f"执行高敏感能力：{capability.name}"


def _runtime_confirm_details(
    session: Session,
    capability: Capability,
    tool_use: ToolUseRequest,
) -> dict[str, Any]:
    return {
        "capability": capability.name,
        "sensitivity": capability.sensitivity,
        "session_kind": session.kind,
        "arguments": _plain_json_object(tool_use.arguments, "tool_use.arguments"),
    }


def _record_tool_metric(
    session: Session,
    tool_name: str,
    outcome: str,
    *,
    error_type: str | None = None,
) -> None:
    increment_counter(
        "tool_calls_total",
        labels={
            "tool": tool_name,
            "session_kind": session.kind,
            "outcome": outcome,
            "error_type": error_type,
        },
    )


def _record_error_metric(component: str, exc: Exception) -> None:
    increment_counter(
        "errors_total",
        labels={
            "component": component,
            "error_type": type(exc).__name__,
        },
    )


def _tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _consent_reply_text(consent: NeedsConsent) -> str:
    scopes = (
        "、".join(consent.pending.scopes) if consent.pending.scopes else consent.pending.service
    )
    return (
        f"需要你授权 {consent.pending.service}（{scopes}）后我才能继续。"
        f"请打开链接完成授权：{consent.url}"
    )


def _consent_interrupt_payload(exc: AgentLoopConsentRequired) -> dict[str, Any]:
    return {
        "capability": exc.capability.name,
        "tool_use_id": exc.tool_use.id,
        "service": exc.consent.pending.service,
        "scopes": list(exc.consent.pending.scopes),
        "url": exc.consent.url,
        "reason": exc.consent.reason,
    }


def _pending_interaction_info(interrupt: SessionInterrupt) -> PendingInteractionInfo:
    return PendingInteractionInfo(
        correlation_id=_non_empty_string(interrupt.correlation_id, "correlation_id"),
        kind=_non_empty_string(interrupt.kind, "kind"),
        expires_at=_to_utc(interrupt.expires_at),
    )


def _effective_cancellation_reason(
    pending: PendingInteractionRecord,
    requested_reason: InteractionCancellationReason,
    *,
    now: datetime,
) -> InteractionCancellationReason:
    if requested_reason == "timeout" or _to_utc(now) > _to_utc(pending.expires_at):
        return "timeout"
    return requested_reason


def _runtime_responder(
    reason: InteractionCancellationReason,
    pending: PendingInteractionRecord,
) -> str:
    if reason == "timeout":
        return "runtime:timeout"
    return pending.responder_id


def _cancellation_resolution_payload(
    reason: InteractionCancellationReason,
    *,
    actor_id: str | None,
    provider_message_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"cancelled": True, "reason": reason}
    if actor_id is not None:
        payload["actor_id"] = _non_empty_string(actor_id, "actor_id")
    if provider_message_id is not None:
        payload["provider_message_id"] = _non_empty_string(
            provider_message_id,
            "provider_message_id",
        )
    return payload


def _cancellation_notice_text(
    pending: PendingInteractionRecord,
    reason: InteractionCancellationReason,
) -> str:
    if pending.kind == "confirm":
        action = _pending_action_label(pending)
        if reason == "timeout":
            return f"已取消:确认超时，[{action}] 未执行。"
        if reason == "command_cancelled":
            return f"已取消:用户主动取消，[{action}] 未执行。"
        return f"已取消:未确认，[{action}] 未执行。"
    if pending.kind == "consent":
        service = _pending_service_label(pending)
        if reason == "timeout":
            return f"已取消:授权超时，[{service}] 授权未完成。"
        if reason == "command_cancelled":
            return f"已取消:用户主动取消，[{service}] 授权未完成。"
        return f"已取消:未完成授权，[{service}] 授权未完成。"
    raise AgentLoopStateError(f"Unsupported pending interaction kind: {pending.kind}")


def _cancelled_tool_result_text(
    pending: PendingInteractionRecord,
    reason: InteractionCancellationReason,
) -> str:
    payload: dict[str, Any] = {
        "status": "Cancelled",
        "kind": pending.kind,
        "reason": reason,
        "correlation_id": pending.correlation_id,
    }
    if pending.kind == "confirm":
        payload["action"] = _pending_action_label(pending)
        details = pending.payload.get("details")
        payload["details"] = (
            _plain_json_object(details, "confirm.details") if isinstance(details, Mapping) else {}
        )
    elif pending.kind == "consent":
        payload["service"] = _pending_service_label(pending)
        scopes = pending.payload.get("scopes", [])
        payload["scopes"] = list(scopes) if isinstance(scopes, list) else []
    return json.dumps(payload, ensure_ascii=False)


def _pending_action_label(pending: PendingInteractionRecord) -> str:
    action = pending.payload.get("action")
    if isinstance(action, str) and action.strip() != "":
        return action.strip()
    capability = pending.payload.get("capability")
    if isinstance(capability, str) and capability.strip() != "":
        return capability.strip()
    return "该操作"


def _pending_service_label(pending: PendingInteractionRecord) -> str:
    service = pending.payload.get("service")
    if isinstance(service, str) and service.strip() != "":
        return service.strip()
    capability = pending.payload.get("capability")
    if isinstance(capability, str) and capability.strip() != "":
        return capability.strip()
    return "授权"


def _pending_capability_name(pending: PendingInteractionRecord) -> str | None:
    capability = pending.payload.get("capability")
    if isinstance(capability, str) and capability.strip() != "":
        return capability.strip()
    return None


def _confirm_reply_text(confirm: AgentLoopConfirmRequired) -> str:
    return f"请在钉钉确认卡片中确认是否执行：{confirm.action}"


def _confirm_interrupt_payload(session: Session, exc: AgentLoopConfirmRequired) -> dict[str, Any]:
    payload = {
        "capability": exc.capability.name,
        "tool_use_id": exc.tool_use.id,
        "arguments": _plain_json_object(exc.tool_use.arguments, "tool_use.arguments"),
        "action": exc.action,
        "details": dict(exc.details),
        "session_id": session.session_id,
    }
    if exc.source == "runtime_sensitivity":
        payload["confirm_source"] = exc.source
        payload["sensitivity"] = exc.capability.sensitivity
    return payload


def _mapping_value(values: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = values.get(key)
    if not isinstance(value, Mapping):
        raise AgentLoopToolError(f"Confirm payload field must be an object: {key}")
    return value


def _session_open_conversation_id(session: Session) -> str | None:
    value = session.context.get("open_conversation_id")
    if isinstance(value, str) and value.strip() != "":
        return value.strip()
    return session.conversation_id if session.kind == "dm" else None


def _session_from_record(record: SessionRecord) -> Session:
    actor_id = record.actor_id or record.principal_id.removeprefix("user:")
    return Session(
        session_id=record.session_id,
        conversation_id=record.conversation_id,
        kind=record.kind,
        bot=_bot_identity(record.bot_id),
        principal=_principal(record.kind, record.principal_id),
        actor=_actor(actor_id),
        context=record.context,
        state=record.state,
        lifecycle=record.lifecycle,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _bot_identity(bot_id: str) -> BotIdentity:
    return BotIdentity(id=_non_empty_string(bot_id, "bot_id"))


def _principal(kind: str, principal_id: str) -> Principal:
    principal_kind = "group" if kind == "group" else "user"
    return Principal(kind=principal_kind, id=_non_empty_string(principal_id, "principal_id"))


def _actor(actor_id: str) -> Actor:
    normalized = _non_empty_string(actor_id, "actor_id")
    return Actor(id=normalized, display_name=normalized)


def _default_confirm_id() -> str:
    return f"confirm_{secrets.token_urlsafe(24)}"


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _plain_json_object(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    return {
        _non_empty_string(key, f"{field_name}.key"): _plain_json_value(
            nested_value,
            f"{field_name}.{key}",
        )
        for key, nested_value in value.items()
    }


def _plain_json_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return _plain_json_object(value, field_name)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain_json_value(item, field_name) for item in value]
    raise ValueError(f"{field_name} must be JSON-compatible")


def _channel_enabled_capabilities(
    values: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    normalized: dict[str, tuple[str, ...]] = {}
    for channel_id, capability_names in values.items():
        if isinstance(capability_names, (str, bytes)) or not isinstance(
            capability_names,
            Sequence,
        ):
            raise ValueError("channel_enabled_capabilities values must be sequences")
        normalized[_non_empty_string(channel_id, "channel_id")] = tuple(
            _non_empty_string(name, "channel_enabled_capability") for name in capability_names
        )
    return normalized


def _session_record_with_state(
    session: Session,
    state: SessionState,
    *,
    context: Mapping[str, Any] | None = None,
) -> SessionRecord:
    return SessionRecord(
        session_id=session.session_id,
        conversation_id=session.conversation_id,
        kind=session.kind,
        bot_id=session.bot.id,
        principal_id=session.principal.id,
        actor_id=session.actor.id,
        state=state,
        lifecycle=session.lifecycle,
        context=session.context if context is None else context,
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

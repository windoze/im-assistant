"""Built-in deterministic slash commands for the assistant runtime."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from src.capabilities import (
    AuthorizationResolution,
    Capability,
    CapabilityChannelContext,
    CapabilityRegistry,
    Denied,
    Granted,
    NeedsConsent,
    Requirement,
    can_use,
)
from src.core.agent_loop import (
    InteractionCancellationReason,
    InteractionCancellationResult,
    PendingInteractionInfo,
)
from src.core.commands import Command, CommandArgsSpec, CommandContext, CommandRegistry
from src.core.session import Session
from src.infra.audit import AuditLogger
from src.infra.store import (
    IdentityBindingRecord,
    MessageRecord,
    PendingInteractionRecord,
    SessionRecord,
)
from src.infra.token_vault import UserToken

CapabilityRegistryFactory = Callable[[Session], CapabilityRegistry]

RESET_REPLY_TEMPLATE = "已重置当前会话，上下文消息已清空（{count} 条）。"
NO_PENDING_REPLY = "当前没有等待中的确认或授权。"
NO_SERVICE_REPLY = "未找到需要 {service} 用户授权的可用能力。"
CONNECT_GRANTED_REPLY = "已连接 {service}，无需重新授权。"
CONNECT_DENIED_REPLY = "无法连接 {service}: {reason}"
DISCONNECT_REMOVED_REPLY = "已断开 {service} 授权。"
DISCONNECT_MISSING_REPLY = "当前没有 {service} 授权。"
DINGTALK_IDENTITY_PROVIDER = "dingtalk"

_OPERATIONAL_CONTEXT_KEYS = frozenset(
    {
        "platform",
        "conversation_type",
        "open_conversation_id",
        "last_actor_nick",
        "activated",
        "activated_by",
        "activation_msg_id",
        "channel_admin_ids",
        "org_admin_ids",
        "actor_roles",
        "actor_union_id",
    }
)


class BuiltinCommandStore(Protocol):
    """Store methods used by built-in deterministic commands."""

    async def add_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MessageRecord:
        """Append command-originated history through the command registry."""

    async def delete_messages_for_session(self, session_id: str) -> int:
        """Delete all persisted chat history for a Session."""

    async def upsert_session(self, record: SessionRecord) -> SessionRecord:
        """Persist Session state/context changes."""

    async def get_identity_binding(
        self,
        provider: str,
        external_user_id: str,
    ) -> IdentityBindingRecord | None:
        """Return one platform identity binding."""

    async def get_pending_interaction_for_session(
        self,
        session_id: str,
    ) -> PendingInteractionRecord | None:
        """Return the active pending interaction for a Session."""


class BuiltinCommandTokenVault(Protocol):
    """TokenVault methods used by `/whoami` and `/disconnect`."""

    async def get(self, principal: str, service: str) -> UserToken | None:
        """Return decrypted delegated token material."""

    async def revoke(self, principal: str, service: str) -> bool:
        """Remove delegated token material."""


class BuiltinCommandAuthorizer(Protocol):
    """Authorizer surface used by `/connect`."""

    async def resolve(
        self,
        requirement: Requirement,
        actor: object,
        mode: str,
        *,
        principal_id: str | None = None,
        session_id: str | None = None,
    ) -> AuthorizationResolution:
        """Resolve one OBO requirement for a Session actor."""


class BuiltinCommandInterruptManager(Protocol):
    """Interrupt manager surface used by `/connect` consent pre-warming."""

    async def create(
        self,
        session: Session,
        *,
        kind: str,
        payload: Mapping[str, Any],
        correlation_id: str,
        responder: str | None = None,
        expires_at: datetime | None = None,
        ttl_seconds: int = 1800,
    ) -> object:
        """Persist a pending interrupt and move the Session to AwaitingInteraction."""


class BuiltinCommandInteractionCanceller(Protocol):
    """Agent-loop cancellation surface used by `/cancel`."""

    async def cancel_pending_interaction_for_session(
        self,
        session: Session,
        *,
        reason: InteractionCancellationReason,
        actor_id: str | None = None,
        provider_message_id: str | None = None,
    ) -> InteractionCancellationResult | None:
        """Cancel the active pending interaction for a Session."""


class BuiltinCommandTimeoutScheduler(Protocol):
    """Timeout scheduler surface used for command-created consent interrupts."""

    def schedule(
        self,
        event: object,
        session: Session,
        pending: PendingInteractionInfo | None,
    ) -> None:
        """Schedule a timeout cancellation for a pending interaction."""


@dataclass(frozen=True, slots=True)
class BuiltinCommandServices:
    """Dependency bundle for built-in command handlers."""

    store: BuiltinCommandStore
    capability_registry_factory: CapabilityRegistryFactory | None = None
    channel_enabled_capabilities: Mapping[str, Sequence[str]] = field(default_factory=dict)
    token_vault: BuiltinCommandTokenVault | None = None
    authorizer: BuiltinCommandAuthorizer | None = None
    interrupt_manager: BuiltinCommandInterruptManager | None = None
    interaction_canceller: BuiltinCommandInteractionCanceller | None = None
    timeout_scheduler: BuiltinCommandTimeoutScheduler | None = None


def create_builtin_command_registry(
    store: BuiltinCommandStore,
    *,
    capability_registry_factory: CapabilityRegistryFactory | None = None,
    channel_enabled_capabilities: Mapping[str, Sequence[str]] | None = None,
    token_vault: BuiltinCommandTokenVault | None = None,
    authorizer: BuiltinCommandAuthorizer | None = None,
    interrupt_manager: BuiltinCommandInterruptManager | None = None,
    interaction_canceller: BuiltinCommandInteractionCanceller | None = None,
    timeout_scheduler: BuiltinCommandTimeoutScheduler | None = None,
    audit_logger: AuditLogger | None = None,
) -> CommandRegistry:
    """Create a command registry populated with all built-in slash commands."""

    services = BuiltinCommandServices(
        store=store,
        capability_registry_factory=capability_registry_factory,
        channel_enabled_capabilities=channel_enabled_capabilities or {},
        token_vault=token_vault,
        authorizer=authorizer,
        interrupt_manager=interrupt_manager,
        interaction_canceller=interaction_canceller,
        timeout_scheduler=timeout_scheduler,
    )
    registry = CommandRegistry(store, audit_logger=audit_logger)
    for command in builtin_commands(services):
        registry.register(command)
    return registry


def builtin_commands(services: BuiltinCommandServices) -> tuple[Command, ...]:
    """Return the first built-in command set for M6."""

    return (
        Command(
            "/help",
            _help_handler(services),
            description="列出当前可用能力和指令",
        ),
        Command(
            "/reset",
            _reset_handler(services),
            description="清空当前会话上下文",
        ),
        Command(
            "/whoami",
            _whoami_handler(services),
            description="查看当前身份绑定和授权状态",
        ),
        Command(
            "/connect",
            _connect_handler(services),
            available_in=("dm",),
            args_spec=CommandArgsSpec(min_args=1, max_args=1),
            description="预热用户授权: /connect <service>",
        ),
        Command(
            "/disconnect",
            _disconnect_handler(services),
            available_in=("dm",),
            args_spec=CommandArgsSpec(min_args=1, max_args=1),
            description="清除用户授权: /disconnect <service>",
        ),
        Command(
            "/cancel",
            _cancel_handler(services),
            args_spec=CommandArgsSpec(max_args=0),
            description="取消当前等待中的确认或授权",
        ),
    )


def _help_handler(services: BuiltinCommandServices):
    async def handle(context: CommandContext) -> str:
        registry = context.registry
        commands = () if registry is None else registry.list_available_commands(context.session)
        capabilities = _visible_capabilities(services, context.session)
        return "\n".join(
            (
                "可用指令:",
                _format_command_list(commands),
                "可用能力:",
                _format_capability_list(capabilities),
            )
        )

    return handle


def _reset_handler(services: BuiltinCommandServices):
    async def handle(context: CommandContext) -> str:
        deleted_count = await services.store.delete_messages_for_session(context.session.session_id)
        await services.store.upsert_session(
            _session_record(
                context.session,
                state="Idle",
                context=_reset_context(context.session.context),
            )
        )
        return RESET_REPLY_TEMPLATE.format(count=deleted_count)

    return handle


def _whoami_handler(services: BuiltinCommandServices):
    async def handle(context: CommandContext) -> str:
        binding = await services.store.get_identity_binding(
            DINGTALK_IDENTITY_PROVIDER,
            context.session.actor.id,
        )
        services_to_report = _obo_services_for_session(services, context.session)
        lines = [
            f"当前用户: {context.session.actor.display_name} ({context.session.actor.id})",
            f"会话: {context.session.kind} / {context.session.principal.id}",
            _identity_binding_line(binding),
        ]
        lines.extend(
            await _authorization_status_lines(services, context.session, services_to_report)
        )
        return "\n".join(lines)

    return handle


def _connect_handler(services: BuiltinCommandServices):
    async def handle(context: CommandContext) -> str:
        service = _normalize_service(context.args[0])
        requirement = _obo_requirement_for_service(services, context.session, service)
        if requirement is None:
            return NO_SERVICE_REPLY.format(service=service)
        if services.authorizer is None or services.interrupt_manager is None:
            raise RuntimeError("/connect requires authorizer and interrupt manager services")

        resolution = await services.authorizer.resolve(
            requirement,
            context.session.actor,
            context.session.kind,
            principal_id=context.session.principal.id,
            session_id=context.session.session_id,
        )
        if isinstance(resolution, Granted):
            return CONNECT_GRANTED_REPLY.format(service=service)
        if isinstance(resolution, Denied):
            return CONNECT_DENIED_REPLY.format(service=service, reason=resolution.reason)
        if isinstance(resolution, NeedsConsent):
            pending_info = PendingInteractionInfo(
                correlation_id=resolution.pending.nonce,
                kind="consent",
                expires_at=resolution.pending.expires_at,
            )
            await services.interrupt_manager.create(
                context.session,
                kind="consent",
                correlation_id=resolution.pending.nonce,
                responder=context.session.actor.id,
                expires_at=resolution.pending.expires_at,
                payload=_connect_consent_payload(resolution),
            )
            if services.timeout_scheduler is not None:
                services.timeout_scheduler.schedule(context.event, context.session, pending_info)
            return _consent_reply_text(resolution)
        raise TypeError(f"Unsupported authorization resolution: {type(resolution).__name__}")

    return handle


def _disconnect_handler(services: BuiltinCommandServices):
    async def handle(context: CommandContext) -> str:
        if services.token_vault is None:
            raise RuntimeError("/disconnect requires a token vault service")
        service = _normalize_service(context.args[0])
        removed = await services.token_vault.revoke(context.session.principal.id, service)
        if removed:
            return DISCONNECT_REMOVED_REPLY.format(service=service)
        return DISCONNECT_MISSING_REPLY.format(service=service)

    return handle


def _cancel_handler(services: BuiltinCommandServices):
    async def handle(context: CommandContext) -> str:
        if services.interaction_canceller is None:
            raise RuntimeError("/cancel requires an interaction canceller service")
        pending = await services.store.get_pending_interaction_for_session(
            context.session.session_id
        )
        if pending is None:
            return NO_PENDING_REPLY
        cancellation = await services.interaction_canceller.cancel_pending_interaction_for_session(
            context.session,
            reason="command_cancelled",
            actor_id=context.session.actor.id,
            provider_message_id=_event_message_id(context.event),
        )
        if cancellation is None:
            return NO_PENDING_REPLY
        return cancellation.notice_text

    return handle


def _visible_capabilities(
    services: BuiltinCommandServices,
    session: Session,
) -> tuple[Capability, ...]:
    registry_factory = services.capability_registry_factory
    if registry_factory is None:
        return ()
    registry = registry_factory(session)
    if not isinstance(registry, CapabilityRegistry):
        raise TypeError("capability_registry_factory must return a CapabilityRegistry")
    channel = _channel_context_for_session(session, services.channel_enabled_capabilities)
    return tuple(
        capability
        for capability in registry.list()
        if capability.handler is not None
        and can_use(capability, session.kind, session.actor, channel)
    )


def _obo_services_for_session(
    services: BuiltinCommandServices,
    session: Session,
) -> tuple[str, ...]:
    services_by_name: dict[str, None] = {}
    for capability in _visible_capabilities(services, session):
        for requirement in capability.requires:
            if requirement.on_behalf_of == "actor":
                services_by_name[requirement.service] = None
    return tuple(services_by_name)


def _obo_requirement_for_service(
    services: BuiltinCommandServices,
    session: Session,
    service: str,
) -> Requirement | None:
    scopes: dict[str, None] = {}
    for capability in _visible_capabilities(services, session):
        for requirement in capability.requires:
            if requirement.on_behalf_of == "actor" and requirement.service == service:
                for scope in requirement.scopes:
                    scopes[scope] = None
    if not scopes:
        return None
    return Requirement(service=service, scopes=tuple(scopes), on_behalf_of="actor")


async def _authorization_status_lines(
    services: BuiltinCommandServices,
    session: Session,
    service_names: Iterable[str],
) -> list[str]:
    normalized_services = tuple(service_names)
    if not normalized_services:
        return ["授权状态: 当前没有需要用户授权的可用能力"]
    if services.token_vault is None:
        return ["授权状态: TokenVault 未配置"]

    lines = ["授权状态:"]
    for service in normalized_services:
        token = await services.token_vault.get(session.principal.id, service)
        lines.append(_token_status_line(service, token))
    return lines


def _token_status_line(service: str, token: UserToken | None) -> str:
    if token is None:
        return f"- {service}: 未授权"
    scopes = "、".join(token.scopes) if token.scopes else "未声明 scope"
    expires_at = token.expires_at.isoformat() if token.expires_at is not None else "无过期时间"
    refresh_note = "，需要刷新" if token.needs_refresh else ""
    return f"- {service}: 已授权（scopes: {scopes}; expires_at: {expires_at}{refresh_note}）"


def _format_command_list(commands: Sequence[Command]) -> str:
    if not commands:
        return "（无）"
    return "\n".join(f"- {command.name}: {command.description or '无说明'}" for command in commands)


def _format_capability_list(capabilities: Sequence[Capability]) -> str:
    if not capabilities:
        return "（无）"
    return "\n".join(
        f"- {capability.name}: {capability.description or capability.sensitivity}"
        for capability in capabilities
    )


def _identity_binding_line(binding: IdentityBindingRecord | None) -> str:
    if binding is None:
        return "身份绑定: 未绑定"
    details = [f"principal={binding.principal_id}"]
    if binding.union_id is not None:
        details.append(f"unionId={binding.union_id}")
    if binding.display_name is not None:
        details.append(f"name={binding.display_name}")
    return f"身份绑定: 已绑定（{'; '.join(details)}）"


def _connect_consent_payload(consent: NeedsConsent) -> dict[str, Any]:
    return {
        "source": "command",
        "command": "/connect",
        "service": consent.pending.service,
        "scopes": list(consent.pending.scopes),
        "url": consent.url,
        "reason": consent.reason,
    }


def _consent_reply_text(consent: NeedsConsent) -> str:
    scopes = (
        "、".join(consent.pending.scopes) if consent.pending.scopes else consent.pending.service
    )
    return f"需要你授权 {consent.pending.service}（{scopes}）。请打开链接完成授权：{consent.url}"


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


def _session_record(
    session: Session,
    *,
    state: str,
    context: Mapping[str, Any],
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
        context=context,
        created_at=session.created_at,
    )


def _reset_context(context: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if key in _OPERATIONAL_CONTEXT_KEYS}


def _event_message_id(event: object) -> str | None:
    value = getattr(event, "msg_id", None)
    if isinstance(value, str) and value.strip() != "":
        return value.strip()
    return None


def _normalize_service(service: str) -> str:
    normalized = service.strip().lower()
    if normalized == "":
        raise ValueError("service must be a non-empty string")
    return normalized


__all__ = [
    "BuiltinCommandAuthorizer",
    "BuiltinCommandInteractionCanceller",
    "BuiltinCommandInterruptManager",
    "BuiltinCommandServices",
    "BuiltinCommandStore",
    "BuiltinCommandTimeoutScheduler",
    "BuiltinCommandTokenVault",
    "NO_PENDING_REPLY",
    "RESET_REPLY_TEMPLATE",
    "builtin_commands",
    "create_builtin_command_registry",
]

"""Deterministic slash-command registry, authorization, and history injection."""

from __future__ import annotations

import inspect
import shlex
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias, cast

from src.core.session import Actor, Session
from src.infra.audit import AuditLogger, CommandAuditOutcome
from src.infra.store import MessageRecord, MessageRole

CommandAvailability = Literal["dm", "group"]
CommandRole = Literal["user", "channel_admin", "org_admin"]
CommandHandlerResult = str | None
CommandHandler: TypeAlias = Callable[
    ["CommandContext"],
    CommandHandlerResult | Awaitable[CommandHandlerResult],
]

UNKNOWN_COMMAND_REPLY = "未知指令:{name}"
COMMAND_REQUIRES_SESSION_REPLY = "指令需要会话上下文"
COMMAND_UNAVAILABLE_REPLY = "该指令不能在当前会话使用:{name}"
COMMAND_FORBIDDEN_REPLY = "权限不足:{name} 需要 {role} 权限"
COMMAND_ARGS_INVALID_REPLY = "参数错误:{reason}"

_ROLE_LEVELS: Mapping[CommandRole, int] = {
    "user": 0,
    "channel_admin": 1,
    "org_admin": 2,
}


class CommandError(RuntimeError):
    """Base error for command registration, parsing, and execution."""


class CommandParseError(CommandError):
    """Raised when slash-command arguments do not match the declared spec."""


class CommandExecutionError(CommandError):
    """Raised when command execution cannot satisfy the command contract."""


class CommandStore(Protocol):
    """Persistent store surface used by command history injection."""

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


class CommandRoleAuthorizer(Protocol):
    """Actor-role source used by the command registry."""

    def roles_for_actor(self, session: Session, actor: Actor) -> frozenset[CommandRole]:
        """Return deterministic roles held by an actor in a Session."""


@dataclass(frozen=True, slots=True)
class CommandArgsSpec:
    """Simple positional argument contract for a slash command."""

    min_args: int = 0
    max_args: int | None = None

    def __post_init__(self) -> None:
        """Validate argument bounds at declaration time."""

        if self.min_args < 0:
            raise ValueError("min_args must be non-negative")
        if self.max_args is not None and self.max_args < self.min_args:
            raise ValueError("max_args must be greater than or equal to min_args")

    def parse(self, args_text: str) -> tuple[str, ...]:
        """Parse shell-like command arguments and enforce declared bounds."""

        try:
            args = tuple(shlex.split(args_text.strip())) if args_text.strip() else ()
        except ValueError as exc:
            raise CommandParseError(str(exc)) from exc

        if len(args) < self.min_args:
            raise CommandParseError(f"至少需要 {self.min_args} 个参数")
        if self.max_args is not None and len(args) > self.max_args:
            raise CommandParseError(f"最多允许 {self.max_args} 个参数")
        return args


@dataclass(frozen=True, slots=True)
class Command:
    """One deterministic user-triggered slash command."""

    name: str
    handler: CommandHandler = field(repr=False)
    available_in: Iterable[CommandAvailability | Literal["both"]] = ("dm", "group")
    requires_role: CommandRole = "user"
    args_spec: CommandArgsSpec = field(default_factory=CommandArgsSpec)
    description: str = ""

    def __post_init__(self) -> None:
        """Normalize command declaration fields for stable lookup."""

        object.__setattr__(self, "name", _normalize_command_name(self.name))
        object.__setattr__(self, "available_in", _normalize_available_in(self.available_in))
        object.__setattr__(self, "requires_role", _normalize_role(self.requires_role))
        if not isinstance(self.args_spec, CommandArgsSpec):
            raise TypeError("args_spec must be a CommandArgsSpec")


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Runtime context passed to one command handler."""

    session: Session
    command: Command
    args: tuple[str, ...]
    args_text: str
    event: object
    registry: CommandRegistry | None = field(default=None, repr=False, compare=False)
    _store: CommandStore | None = field(default=None, repr=False, compare=False)

    async def inject_message(self, text: str) -> MessageRecord:
        """Append command-originated context so later agent turns can see it."""

        if self._store is None:
            raise CommandExecutionError("Command history injection requires a configured store")
        return await inject_message(
            self._store,
            self.session,
            text,
            actor_id=self.session.actor.id,
            command_name=self.command.name,
        )


class ContextCommandRoleAuthorizer:
    """Resolve actor roles from Session context without external services."""

    def roles_for_actor(self, session: Session, actor: Actor) -> frozenset[CommandRole]:
        """Return roles granted to an actor by the current Session context."""

        roles: set[CommandRole] = {"user"}
        if _actor_has_context_role(session.context, actor.id, "channel_admin"):
            roles.add("channel_admin")
        if _actor_has_context_role(session.context, actor.id, "org_admin"):
            roles.add("org_admin")
        return frozenset(roles)


class CommandRegistry:
    """Slash-command registry and dispatcher independent of AI tool capabilities."""

    def __init__(
        self,
        store: CommandStore | None = None,
        commands: Iterable[Command] | None = None,
        *,
        role_authorizer: CommandRoleAuthorizer | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._store = store
        self._role_authorizer = role_authorizer or ContextCommandRoleAuthorizer()
        self._audit_logger = audit_logger
        self._commands: dict[str, Command] = {}
        for command in commands or ():
            self.register(command)

    def register(self, command: Command) -> CommandRegistry:
        """Register one command name, rejecting accidental duplicates."""

        if command.name in self._commands:
            raise ValueError(f"Command already registered: {command.name}")
        self._commands[command.name] = command
        return self

    def get(self, name: str) -> Command | None:
        """Return a command by slash-prefixed name."""

        return self._commands.get(_normalize_command_name(name))

    def list_commands(self) -> tuple[Command, ...]:
        """List registered commands in deterministic name order."""

        return tuple(self._commands[name] for name in sorted(self._commands))

    def list_available_commands(self, session: Session) -> tuple[Command, ...]:
        """List commands the current actor can execute in this Session."""

        return tuple(
            command
            for command in self.list_commands()
            if session.kind in command.available_in
            and self._actor_has_required_role(session, command.requires_role)
        )

    async def inject_message(
        self,
        session: Session,
        text: str,
        *,
        actor_id: str | None = None,
        command_name: str | None = None,
    ) -> MessageRecord:
        """Append command-originated context through the registry's configured store."""

        if self._store is None:
            raise CommandExecutionError("Command history injection requires a configured store")
        return await inject_message(
            self._store,
            session,
            text,
            actor_id=actor_id,
            command_name=command_name,
        )

    async def handle_command(
        self,
        session: Session | None,
        command_text: str,
        event: object,
    ) -> str | None:
        """Dispatch one slash command after mode, role, and argument checks."""

        name, args_text = _parse_command_text(command_text)
        command = self.get(name)
        if command is None:
            await self._record_command_audit(
                session,
                name,
                args=(),
                args_text=args_text,
                command=None,
                outcome="unknown_command",
            )
            return UNKNOWN_COMMAND_REPLY.format(name=name)
        if session is None:
            await self._record_command_audit(
                session,
                name,
                args=(),
                args_text=args_text,
                command=command,
                outcome="missing_session",
            )
            return COMMAND_REQUIRES_SESSION_REPLY
        if session.kind not in command.available_in:
            await self._record_command_audit(
                session,
                name,
                args=(),
                args_text=args_text,
                command=command,
                outcome="unavailable",
            )
            return COMMAND_UNAVAILABLE_REPLY.format(name=command.name)
        if not self._actor_has_required_role(session, command.requires_role):
            await self._record_command_audit(
                session,
                name,
                args=(),
                args_text=args_text,
                command=command,
                outcome="forbidden",
                reason=f"requires_role:{command.requires_role}",
            )
            return COMMAND_FORBIDDEN_REPLY.format(
                name=command.name,
                role=command.requires_role,
            )

        try:
            args = command.args_spec.parse(args_text)
        except CommandParseError as exc:
            await self._record_command_audit(
                session,
                name,
                args=(),
                args_text=args_text,
                command=command,
                outcome="invalid_args",
                reason=str(exc),
            )
            return COMMAND_ARGS_INVALID_REPLY.format(reason=str(exc))

        context = CommandContext(
            session=session,
            command=command,
            args=args,
            args_text=args_text,
            event=event,
            registry=self,
            _store=self._store,
        )
        try:
            result = command.handler(context)
            if inspect.isawaitable(result):
                result = await result
            if result is not None and not isinstance(result, str):
                raise CommandExecutionError("Command handler must return str or None")
        except Exception as exc:
            await self._record_command_audit(
                session,
                name,
                args=args,
                args_text=args_text,
                command=command,
                outcome="failed",
                reason=str(exc) or type(exc).__name__,
            )
            raise
        await self._record_command_audit(
            session,
            name,
            args=args,
            args_text=args_text,
            command=command,
            outcome="executed",
        )
        return result

    def _actor_has_required_role(self, session: Session, required_role: CommandRole) -> bool:
        roles = self._role_authorizer.roles_for_actor(session, session.actor)
        required_level = _ROLE_LEVELS[required_role]
        return any(_ROLE_LEVELS[role] >= required_level for role in roles)

    async def _record_command_audit(
        self,
        session: Session | None,
        command_name: str,
        *,
        args: Sequence[str],
        args_text: str,
        command: Command | None,
        outcome: CommandAuditOutcome,
        reason: str | None = None,
    ) -> None:
        if self._audit_logger is None:
            return
        await self._audit_logger.record_command_execution(
            actor_id=None if session is None else session.actor.id,
            principal_id=None if session is None else session.principal.id,
            session_id=None if session is None else session.session_id,
            command_name=command_name,
            args=args,
            args_text=args_text,
            session_kind=None if session is None else session.kind,
            requires_role=None if command is None else command.requires_role,
            available_in=() if command is None else tuple(command.available_in),
            outcome=outcome,
            reason=reason,
        )


async def inject_message(
    store: CommandStore,
    session: Session,
    text: str,
    *,
    actor_id: str | None = None,
    command_name: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> MessageRecord:
    """Inject a command-originated user-history item into one Session."""

    normalized_text = _non_empty_string(text, "text")
    message_metadata = {
        "source": "command_injection",
        **dict(metadata or {}),
    }
    if command_name is not None:
        message_metadata["command"] = _normalize_command_name(command_name)
    return await store.add_message(
        session_id=session.session_id,
        role="user",
        content=normalized_text,
        actor_id=actor_id or session.actor.id,
        metadata=message_metadata,
    )


def _parse_command_text(command_text: str) -> tuple[str, str]:
    normalized = _non_empty_string(command_text, "command_text").lstrip()
    if not normalized.startswith("/"):
        raise ValueError("command_text must start with '/'")
    parts = normalized.split(maxsplit=1)
    name = parts[0]
    args_text = parts[1] if len(parts) == 2 else ""
    return _normalize_command_name(name), args_text.strip()


def _normalize_command_name(name: str) -> str:
    normalized = _non_empty_string(name, "name").strip().lower()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if normalized == "/":
        raise ValueError("command name must include text after '/'")
    if any(character.isspace() for character in normalized):
        raise ValueError("command name must not contain whitespace")
    return normalized


def _normalize_available_in(
    values: Iterable[CommandAvailability | Literal["both"]],
) -> tuple[CommandAvailability, ...]:
    available: set[CommandAvailability] = set()
    for value in values:
        if value == "both":
            available.update(("dm", "group"))
        elif value in ("dm", "group"):
            available.add(value)
        else:
            raise ValueError(f"Invalid command availability: {value}")
    if not available:
        raise ValueError("available_in must not be empty")
    return tuple(sorted(available))


def _normalize_role(role: str) -> CommandRole:
    if role not in _ROLE_LEVELS:
        raise ValueError(f"Invalid command role: {role}")
    return cast(CommandRole, role)


def _actor_has_context_role(
    context: Mapping[str, Any],
    actor_id: str,
    role: Literal["channel_admin", "org_admin"],
) -> bool:
    if role == "channel_admin" and _actor_has_context_role(context, actor_id, "org_admin"):
        return True
    if actor_id in _string_set(context.get(f"{role}_ids")):
        return True
    actor_roles = context.get("actor_roles")
    if isinstance(actor_roles, Mapping):
        role_values = actor_roles.get(actor_id)
        return role in _string_set(role_values)
    return False


def _string_set(value: object) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset({value.strip()}) if value.strip() else frozenset()
    if not isinstance(value, Iterable):
        return frozenset()
    strings: set[str] = set()
    for item in value:
        if isinstance(item, str) and item.strip():
            strings.add(item.strip())
    return frozenset(strings)


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


__all__ = [
    "COMMAND_ARGS_INVALID_REPLY",
    "COMMAND_FORBIDDEN_REPLY",
    "COMMAND_REQUIRES_SESSION_REPLY",
    "COMMAND_UNAVAILABLE_REPLY",
    "Command",
    "CommandArgsSpec",
    "CommandAvailability",
    "CommandContext",
    "CommandError",
    "CommandExecutionError",
    "CommandHandler",
    "CommandParseError",
    "CommandRegistry",
    "CommandRole",
    "CommandRoleAuthorizer",
    "CommandStore",
    "ContextCommandRoleAuthorizer",
    "UNKNOWN_COMMAND_REPLY",
    "inject_message",
]

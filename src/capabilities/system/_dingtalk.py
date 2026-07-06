"""Shared helpers for DingTalk-backed system capabilities."""

from __future__ import annotations

from typing import Any, Protocol, cast


class DingTalkAppClient(Protocol):
    """DingTalk client methods used by application-level system tools."""

    async def get_user_list(self, **kwargs: Any) -> dict[str, str]:
        """Return a mapping from DingTalk userId to display name."""

    async def user_by_id(self, user_id: str) -> Any:
        """Return one DingTalk contact user by userId."""

    async def create_document(
        self,
        *,
        title: str,
        parent_object_type: str,
        parent_object_id: str,
    ) -> Any:
        """Create a DingTalk document."""

    async def append_document_content(self, doc_id: str, text: str) -> Any:
        """Append text content to a DingTalk document."""

    async def create_todo(self, **kwargs: Any) -> Any:
        """Create one DingTalk todo task."""


def require_dingtalk_client(context: Any, *method_names: str) -> DingTalkAppClient:
    """Return the configured DingTalk client and verify required methods are present."""

    require_service = getattr(context, "require_service", None)
    if not callable(require_service):
        raise RuntimeError("Capability context does not expose runtime services")

    service = require_service("dingtalk_client")
    missing = [
        method_name
        for method_name in method_names
        if not callable(getattr(service, method_name, None))
    ]
    if missing:
        raise RuntimeError(f"DingTalk client lacks required methods: {', '.join(missing)}")
    return cast(DingTalkAppClient, service)


def non_empty_string(value: object, field_name: str) -> str:
    """Normalize one non-empty string argument supplied by Claude."""

    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def optional_string(value: object, field_name: str) -> str | None:
    """Normalize an optional non-empty string argument supplied by Claude."""

    if value is None:
        return None
    return non_empty_string(value, field_name)

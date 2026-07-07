"""Confirm-gated DingTalk notification capability."""

from __future__ import annotations

from typing import Any

from src.capabilities import Capability
from src.capabilities.system._dingtalk import (
    non_empty_string,
    optional_string,
    require_dingtalk_client,
)


async def send_notification(
    context: Any,
    *,
    content: str,
    target_user_id: str | None = None,
) -> dict[str, Any]:
    """Send a DingTalk notification only after the actor approves a confirm card."""

    client = require_dingtalk_client(context, "send_oto", "send_group")
    normalized_content = non_empty_string(content, "content")
    normalized_target_user_id = optional_string(target_user_id, "target_user_id")
    target = _notification_target(context, normalized_target_user_id)
    await context.confirm(
        "发送钉钉通知",
        {
            "target": target["label"],
            "content": normalized_content,
        },
    )

    if target["kind"] == "group":
        response = await client.send_group(target["id"], normalized_content)
    else:
        response = await client.send_oto([target["id"]], normalized_content)
    return {
        "sent": True,
        "target": target,
        "content": normalized_content,
        "response": response,
    }


def _notification_target(context: Any, target_user_id: str | None) -> dict[str, str]:
    session = context.session
    if target_user_id is not None:
        return {"kind": "user", "id": target_user_id, "label": f"user:{target_user_id}"}
    if session.kind == "group":
        open_conversation_id = session.context.get("open_conversation_id")
        if not isinstance(open_conversation_id, str) or open_conversation_id.strip() == "":
            open_conversation_id = session.conversation_id
        normalized = non_empty_string(open_conversation_id, "open_conversation_id")
        return {"kind": "group", "id": normalized, "label": f"group:{normalized}"}
    actor_id = non_empty_string(session.actor.id, "actor.id")
    return {"kind": "user", "id": actor_id, "label": f"user:{actor_id}"}


CAPABILITY = Capability(
    name="send_notification",
    origin="system",
    available_in=["dm", "group"],
    sensitivity="high",
    description="Send a DingTalk notification after an explicit confirm-card approval.",
    input_schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Notification text to send."},
            "target_user_id": {
                "type": "string",
                "description": (
                    "Optional DingTalk userId; defaults to the current DM actor or group."
                ),
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    },
    handler=send_notification,
)

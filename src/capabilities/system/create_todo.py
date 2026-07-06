"""Application-level DingTalk todo creation capability."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.capabilities import Capability
from src.capabilities.system._dingtalk import (
    non_empty_string,
    optional_string,
    require_dingtalk_client,
)


async def create_todo(
    context: Any,
    *,
    subject: str,
    description: str | None = None,
    assignee_user_id: str | None = None,
    assignee_union_id: str | None = None,
    due_time: int | None = None,
    priority: int | None = None,
    detail_url: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create a DingTalk todo for the current actor or a specified assignee."""

    client = require_dingtalk_client(context, "user_by_id", "create_todo")
    normalized_subject = non_empty_string(subject, "subject")
    normalized_description = optional_string(description, "description")
    normalized_assignee_user_id = optional_string(assignee_user_id, "assignee_user_id")
    normalized_assignee_union_id = optional_string(assignee_union_id, "assignee_union_id")
    if normalized_assignee_user_id is not None and normalized_assignee_union_id is not None:
        raise ValueError("Provide either assignee_user_id or assignee_union_id, not both")

    actor_user_id = non_empty_string(context.session.actor.id, "actor.id")
    actor_user = await client.user_by_id(actor_user_id)
    creator_union_id = _union_id(actor_user)
    if normalized_assignee_union_id is not None:
        target_union_id = normalized_assignee_union_id
    elif normalized_assignee_user_id is not None and normalized_assignee_user_id != actor_user_id:
        target_union_id = _union_id(await client.user_by_id(normalized_assignee_user_id))
    else:
        target_union_id = creator_union_id

    todo = await client.create_todo(
        union_id=target_union_id,
        subject=normalized_subject,
        creator_union_id=creator_union_id,
        executor_union_ids=[target_union_id],
        description=normalized_description,
        due_time=due_time,
        priority=priority,
        detail_url=detail_url,
    )
    return {
        "task_id": todo.task_id,
        "subject": normalized_subject,
        "creator_union_id": creator_union_id,
        "assignee_union_id": target_union_id,
    }


def _union_id(user: Any) -> str:
    union_id = getattr(user, "union_id", None)
    if union_id is None and isinstance(getattr(user, "raw", None), Mapping):
        union_id = user.raw.get("unionId")
    return non_empty_string(union_id, "unionId")


CAPABILITY = Capability(
    name="create_todo",
    origin="system",
    available_in=["global"],
    description="Create a DingTalk todo task with application credentials and unionId.",
    input_schema={
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Todo title."},
            "description": {"type": "string", "description": "Optional todo details."},
            "assignee_user_id": {
                "type": "string",
                "description": "Optional DingTalk userId to assign; defaults to current actor.",
            },
            "assignee_union_id": {
                "type": "string",
                "description": "Optional DingTalk unionId to assign.",
            },
            "due_time": {
                "type": "integer",
                "description": "Optional due time as Unix epoch milliseconds.",
            },
            "priority": {
                "type": "integer",
                "enum": [1, 2, 3],
                "description": "Optional DingTalk todo priority.",
            },
            "detail_url": {
                "type": "object",
                "description": "Optional detail URL mapping accepted by DingTalk.",
            },
        },
        "required": ["subject"],
        "additionalProperties": False,
    },
    handler=create_todo,
)

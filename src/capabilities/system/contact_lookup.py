"""Application-level DingTalk contact lookup capability."""

from __future__ import annotations

from typing import Any

from src.capabilities import Capability
from src.capabilities.system._dingtalk import (
    non_empty_string,
    optional_string,
    require_dingtalk_client,
)


async def contact_lookup(
    context: Any,
    *,
    user_id: str | None = None,
    name: str | None = None,
    match_mode: str = "exact",
    department_id: str = "1",
) -> dict[str, Any]:
    """Look up DingTalk contacts by userId or display name using app credentials."""

    client = require_dingtalk_client(context, "get_user_list", "user_by_id")
    normalized_user_id = optional_string(user_id, "user_id")
    normalized_name = optional_string(name, "name")
    if normalized_user_id is not None and normalized_name is not None:
        raise ValueError("Provide either user_id or name, not both")

    if normalized_user_id is None and normalized_name is None:
        normalized_user_id = non_empty_string(context.session.actor.id, "actor.id")

    if normalized_user_id is not None:
        user = await client.user_by_id(normalized_user_id)
        return {
            "query": {"user_id": normalized_user_id},
            "count": 1,
            "matches": [_contact_user_payload(user)],
        }

    normalized_department_id = non_empty_string(department_id, "department_id")
    normalized_match_mode = non_empty_string(match_mode, "match_mode")
    if normalized_match_mode not in {"exact", "contains"}:
        raise ValueError("match_mode must be 'exact' or 'contains'")

    contacts = await client.get_user_list(department_id=normalized_department_id)
    matches = [
        {"user_id": contact_user_id, "name": contact_name}
        for contact_user_id, contact_name in sorted(contacts.items())
        if _name_matches(contact_name, normalized_name, normalized_match_mode)
    ]
    return {
        "query": {
            "name": normalized_name,
            "match_mode": normalized_match_mode,
            "department_id": normalized_department_id,
        },
        "count": len(matches),
        "matches": matches,
    }


def _contact_user_payload(user: Any) -> dict[str, Any]:
    return {
        "user_id": user.user_id,
        "name": user.name,
        "union_id": user.union_id,
    }


def _name_matches(contact_name: str, query: str | None, match_mode: str) -> bool:
    if query is None:
        return False
    if match_mode == "exact":
        return contact_name == query
    return query.casefold() in contact_name.casefold()


CAPABILITY = Capability(
    name="contact_lookup",
    origin="system",
    available_in=["global"],
    description="Look up DingTalk organization contacts by userId or display name.",
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "DingTalk userId to fetch. Defaults to the current actor.",
            },
            "name": {
                "type": "string",
                "description": "Display name to search in the configured department.",
            },
            "match_mode": {
                "type": "string",
                "enum": ["exact", "contains"],
                "description": "How to compare the provided name. Defaults to exact.",
            },
            "department_id": {
                "type": "string",
                "description": "DingTalk department id to search when name is provided.",
            },
        },
        "additionalProperties": False,
    },
    handler=contact_lookup,
)

"""Application-level DingTalk document creation capability."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.capabilities import Capability
from src.capabilities.system._dingtalk import (
    non_empty_string,
    optional_string,
    require_dingtalk_client,
)

DEFAULT_PARENT_OBJECT_TYPE = "wiki_space"


async def create_doc(
    context: Any,
    *,
    title: str,
    content: str,
    parent_object_id: str | None = None,
    parent_object_type: str | None = None,
) -> dict[str, Any]:
    """Create a DingTalk document, append text content, and return document details."""

    client = require_dingtalk_client(
        context,
        "create_document",
        "append_document_content",
    )
    normalized_title = non_empty_string(title, "title")
    normalized_content = non_empty_string(content, "content")
    defaults = _document_defaults(context)
    normalized_parent_id = optional_string(parent_object_id, "parent_object_id") or _default_string(
        defaults, "parent_object_id"
    )
    if normalized_parent_id is None:
        raise ValueError(
            "create_doc requires parent_object_id or dingtalk.document.parent_object_id"
        )
    normalized_parent_type = (
        optional_string(parent_object_type, "parent_object_type")
        or _default_string(defaults, "parent_object_type")
        or DEFAULT_PARENT_OBJECT_TYPE
    )

    document = await client.create_document(
        title=normalized_title,
        parent_object_type=normalized_parent_type,
        parent_object_id=normalized_parent_id,
    )
    append_result = await client.append_document_content(document.doc_id, normalized_content)
    return {
        "doc_id": document.doc_id,
        "url": document.url,
        "title": normalized_title,
        "parent_object_type": normalized_parent_type,
        "parent_object_id": normalized_parent_id,
        "content_appended": True,
        "append_result": append_result,
    }


def _document_defaults(context: Any) -> Mapping[str, object]:
    services = getattr(context, "services", {})
    raw_defaults = services.get("dingtalk_document_defaults", {})
    if raw_defaults is None:
        return {}
    if not isinstance(raw_defaults, Mapping):
        raise ValueError("dingtalk_document_defaults service must be a mapping")
    return raw_defaults


def _default_string(defaults: Mapping[str, object], key: str) -> str | None:
    return optional_string(defaults.get(key), f"dingtalk_document_defaults.{key}")


CAPABILITY = Capability(
    name="create_doc",
    origin="system",
    available_in=["dm", "group"],
    description="Create a DingTalk document and write the provided text content into it.",
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Document title."},
            "content": {"type": "string", "description": "Text content to write."},
            "parent_object_id": {
                "type": "string",
                "description": "Optional DingTalk wiki space or parent document id.",
            },
            "parent_object_type": {
                "type": "string",
                "description": "Optional DingTalk parent type, for example wiki_space.",
            },
        },
        "required": ["title", "content"],
        "additionalProperties": False,
    },
    handler=create_doc,
)

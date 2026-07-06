"""Tests for built-in application-level DingTalk system capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from src.capabilities import Capability, load_capability_registry
from src.capabilities.system.contact_lookup import CAPABILITY as CONTACT_LOOKUP
from src.capabilities.system.create_doc import CAPABILITY as CREATE_DOC
from src.capabilities.system.create_todo import CAPABILITY as CREATE_TODO
from src.core import (
    Actor,
    BotIdentity,
    CapabilityExecutionContext,
    Principal,
    Session,
)
from src.infra.dingtalk_client import DingTalkDocument, DingTalkTodo, DingTalkUser


def test_system_registry_loads_t18_application_tools() -> None:
    """The system tier should declare the first app-level no-OBO tools."""

    registry = load_capability_registry()

    assert {"contact_lookup", "create_doc", "create_todo"}.issubset(registry.names())
    create_doc = registry.get("create_doc")
    assert create_doc is not None
    assert create_doc.available_in == ("dm", "group")
    assert create_doc.requires == ()


@pytest.mark.asyncio
async def test_contact_lookup_defaults_to_current_actor_user_id() -> None:
    """Looking up without explicit arguments should return the current actor."""

    client = FakeDingTalkToolClient()
    context = _context(CONTACT_LOOKUP, client)

    result = await CONTACT_LOOKUP.handler(context)

    assert result == {
        "query": {"user_id": "user-1"},
        "count": 1,
        "matches": [{"user_id": "user-1", "name": "Alice", "union_id": "union-1"}],
    }
    assert client.calls == [("user_by_id", "user-1")]


@pytest.mark.asyncio
async def test_contact_lookup_searches_names_in_department() -> None:
    """Name lookup should search the contact mapping returned by the client."""

    client = FakeDingTalkToolClient()
    context = _context(CONTACT_LOOKUP, client)

    result = await CONTACT_LOOKUP.handler(
        context,
        name="ali",
        match_mode="contains",
        department_id="42",
    )

    assert result == {
        "query": {"name": "ali", "match_mode": "contains", "department_id": "42"},
        "count": 1,
        "matches": [{"user_id": "user-1", "name": "Alice"}],
    }
    assert client.calls == [("get_user_list", "42")]


@pytest.mark.asyncio
async def test_create_doc_uses_defaults_creates_document_and_appends_content() -> None:
    """The document capability should create a doc and write the requested content."""

    client = FakeDingTalkToolClient()
    context = _context(
        CREATE_DOC,
        client,
        document_defaults={
            "parent_object_type": "wiki_space",
            "parent_object_id": "space-1",
        },
    )

    result = await CREATE_DOC.handler(
        context,
        title="会议纪要",
        content="今天讨论 T18。",
    )

    assert result == {
        "doc_id": "doc-1",
        "url": "https://docs.example.com/doc-1",
        "title": "会议纪要",
        "parent_object_type": "wiki_space",
        "parent_object_id": "space-1",
        "content_appended": True,
        "append_result": {"blockId": "block-1"},
    }
    assert client.calls == [
        ("create_document", "会议纪要", "wiki_space", "space-1"),
        ("append_document_content", "doc-1", "今天讨论 T18。"),
    ]


@pytest.mark.asyncio
async def test_create_doc_requires_parent_id_when_default_missing() -> None:
    """A clear handler error should be returned when no document parent is configured."""

    client = FakeDingTalkToolClient()
    context = _context(CREATE_DOC, client)

    with pytest.raises(ValueError, match="parent_object_id"):
        await CREATE_DOC.handler(context, title="No Parent", content="body")


@pytest.mark.asyncio
async def test_create_todo_resolves_actor_union_id_and_creates_task() -> None:
    """Todo creation should use app-level userId to unionId lookup before creating a task."""

    client = FakeDingTalkToolClient()
    context = _context(CREATE_TODO, client)

    result = await CREATE_TODO.handler(
        context,
        subject="整理会议纪要",
        description="今天完成",
        due_time=1783377600000,
        priority=1,
    )

    assert result == {
        "task_id": "task-1",
        "subject": "整理会议纪要",
        "creator_union_id": "union-1",
        "assignee_union_id": "union-1",
    }
    assert client.calls == [
        ("user_by_id", "user-1"),
        (
            "create_todo",
            {
                "union_id": "union-1",
                "subject": "整理会议纪要",
                "creator_union_id": "union-1",
                "executor_union_ids": ["union-1"],
                "description": "今天完成",
                "due_time": 1783377600000,
                "priority": 1,
                "detail_url": None,
            },
        ),
    ]


class FakeDingTalkToolClient:
    """Fake DingTalk client used by system capability tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.users = {
            "user-1": DingTalkUser(
                user_id="user-1",
                name="Alice",
                raw={"userId": "user-1", "unionId": "union-1", "name": "Alice"},
                union_id="union-1",
            ),
            "user-2": DingTalkUser(
                user_id="user-2",
                name="Bob",
                raw={"userId": "user-2", "unionId": "union-2", "name": "Bob"},
                union_id="union-2",
            ),
        }

    async def get_user_list(self, *, department_id: str = "1", **_kwargs: Any) -> dict[str, str]:
        self.calls.append(("get_user_list", department_id))
        return {user.user_id: user.name for user in self.users.values()}

    async def user_by_id(self, user_id: str) -> DingTalkUser:
        self.calls.append(("user_by_id", user_id))
        return self.users[user_id]

    async def create_document(
        self,
        *,
        title: str,
        parent_object_type: str,
        parent_object_id: str,
    ) -> DingTalkDocument:
        self.calls.append(("create_document", title, parent_object_type, parent_object_id))
        return DingTalkDocument(
            doc_id="doc-1",
            url="https://docs.example.com/doc-1",
            raw={"docId": "doc-1", "url": "https://docs.example.com/doc-1"},
        )

    async def append_document_content(self, doc_id: str, text: str) -> dict[str, str]:
        self.calls.append(("append_document_content", doc_id, text))
        return {"blockId": "block-1"}

    async def create_todo(self, **kwargs: Any) -> DingTalkTodo:
        self.calls.append(("create_todo", kwargs))
        return DingTalkTodo(task_id="task-1", raw={"taskId": "task-1"})


def _context(
    capability: Capability,
    client: FakeDingTalkToolClient,
    *,
    document_defaults: Mapping[str, object] | None = None,
) -> CapabilityExecutionContext:
    services: dict[str, object] = {"dingtalk_client": client}
    if document_defaults is not None:
        services["dingtalk_document_defaults"] = dict(document_defaults)
    return CapabilityExecutionContext(
        session=Session(
            session_id="dingtalk:dm:conversation-1",
            conversation_id="conversation-1",
            kind="dm",
            bot=BotIdentity(id="robot-code"),
            principal=Principal(kind="user", id="user:user-1"),
            actor=Actor(id="user-1", display_name="Alice"),
            context={"platform": "dingtalk"},
        ),
        capability=capability,
        services=services,
    )

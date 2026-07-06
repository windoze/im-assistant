"""OBO DingTalk calendar summary capability for the current actor."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.capabilities import Capability, Requirement
from src.capabilities.system._dingtalk import (
    non_empty_string,
    optional_string,
    require_dingtalk_client,
)

DEFAULT_TIMEZONE = "Asia/Shanghai"
SCHEDULE_SUMMARY_SYSTEM_PROMPT = (
    "你是企业内 AI 助手，负责根据用户本人授权读取的钉钉日程生成当天总结。"
    "只能依据工具提供的日程 JSON 总结，不要编造未出现的会议、地点或参会人。"
)


class ScheduleSummaryLLM(Protocol):
    """LLM service shape required by the schedule summary handler."""

    async def complete(self, system: str, messages: Sequence[Mapping[str, Any]]) -> str:
        """Return a text summary for the supplied schedule prompt."""


async def schedule_summary(
    context: Any,
    *,
    date: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    """Read the actor's DingTalk calendar through OBO and summarize that day."""

    user_token = context.require_user_token("calendar")
    client = require_dingtalk_client(context, "get_primary_calendar", "list_calendar_events")
    llm_client = _require_llm_client(context)
    target_day, tz, start_at, end_at = _day_window(date, timezone)

    calendar = await client.get_primary_calendar(use_user_token=user_token)
    calendar_id = non_empty_string(getattr(calendar, "calendar_id", None), "calendar.calendar_id")
    events = await client.list_calendar_events(
        user_id="me",
        calendar_id=calendar_id,
        start_time=start_at,
        end_time=end_at,
        use_user_token=user_token,
    )
    event_payloads = [_event_payload(event) for event in events]
    summary = await llm_client.complete(
        SCHEDULE_SUMMARY_SYSTEM_PROMPT,
        [
            {
                "role": "user",
                "content": _summary_prompt(
                    target_day=target_day,
                    timezone_name=_timezone_key(tz),
                    calendar=calendar,
                    events=event_payloads,
                ),
            }
        ],
    )
    return {
        "date": target_day.isoformat(),
        "timezone": _timezone_key(tz),
        "calendar_id": calendar_id,
        "event_count": len(event_payloads),
        "summary": summary,
    }


def _require_llm_client(context: Any) -> ScheduleSummaryLLM:
    require_service = getattr(context, "require_service", None)
    if not callable(require_service):
        raise RuntimeError("Capability context does not expose runtime services")

    service = require_service("llm_client")
    if not callable(getattr(service, "complete", None)):
        raise RuntimeError("LLM client lacks required method: complete")
    return cast(ScheduleSummaryLLM, service)


def _day_window(
    raw_date: str | None,
    raw_timezone: str,
) -> tuple[Date, ZoneInfo, datetime, datetime]:
    timezone_name = non_empty_string(raw_timezone, "timezone")
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc

    normalized_date = optional_string(raw_date, "date")
    if normalized_date is None:
        target_day = datetime.now(tz).date()
    else:
        try:
            target_day = Date.fromisoformat(normalized_date)
        except ValueError as exc:
            raise ValueError("date must use YYYY-MM-DD format") from exc

    start_at = datetime.combine(target_day, time.min, tzinfo=tz).astimezone(UTC)
    end_at = (datetime.combine(target_day, time.min, tzinfo=tz) + timedelta(days=1)).astimezone(UTC)
    return target_day, tz, start_at, end_at


def _summary_prompt(
    *,
    target_day: Date,
    timezone_name: str,
    calendar: Any,
    events: Sequence[Mapping[str, Any]],
) -> str:
    calendar_payload = {
        "calendar_id": getattr(calendar, "calendar_id", None),
        "summary": getattr(calendar, "summary", None),
        "time_zone": getattr(calendar, "time_zone", None),
    }
    payload = {
        "date": target_day.isoformat(),
        "timezone": timezone_name,
        "calendar": {key: value for key, value in calendar_payload.items() if value is not None},
        "events": list(events),
    }
    return (
        f"请总结用户在 {target_day.isoformat()}（{timezone_name}）的日程。"
        "请用中文输出：总体安排、重点事项、时间冲突或空档提醒。"
        "如果 events 为空，请明确说明今天没有日程。"
        f"\n\n日程 JSON:\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def _event_payload(event: Any) -> dict[str, Any]:
    payload = {
        "id": _event_value(event, "event_id", "eventId", "event_id", "id"),
        "title": _event_value(event, "summary", "summary", "title", "subject"),
        "description": _event_value(event, "description", "description", "body"),
        "start": _event_time_value(event, "start_time", "start"),
        "end": _event_time_value(event, "end_time", "end"),
        "location": _event_location_value(event),
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _event_value(event: Any, attr_name: str, *raw_keys: str) -> Any:
    if not isinstance(event, Mapping):
        value = getattr(event, attr_name, None)
        if value is not None:
            return value
    raw = event if isinstance(event, Mapping) else getattr(event, "raw", {})
    if isinstance(raw, Mapping):
        for key in raw_keys:
            value = raw.get(key)
            if value is not None:
                return value
    return None


def _event_time_value(event: Any, attr_name: str, raw_key: str) -> str | None:
    value = _event_value(event, attr_name, raw_key)
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for key in ("dateTime", "datetime", "date", "time"):
            raw_value = value.get(key)
            if isinstance(raw_value, str) and raw_value.strip() != "":
                return raw_value.strip()
    return None


def _event_location_value(event: Any) -> str | None:
    value = _event_value(event, "location", "location")
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for key in ("displayName", "name", "address"):
            raw_value = value.get(key)
            if isinstance(raw_value, str) and raw_value.strip() != "":
                return raw_value.strip()
    return None


def _timezone_key(tz: ZoneInfo) -> str:
    return str(getattr(tz, "key", DEFAULT_TIMEZONE))


CAPABILITY = Capability(
    name="schedule_summary",
    origin="system",
    available_in=["dm"],
    requires=[Requirement(service="calendar", scopes=["calendar:read"], on_behalf_of="actor")],
    sensitivity="high",
    description="Summarize today's DingTalk calendar events for the current DM actor using OBO.",
    input_schema={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Optional date to summarize as YYYY-MM-DD. Defaults to today.",
            },
            "timezone": {
                "type": "string",
                "description": "IANA timezone used to resolve today. Defaults to Asia/Shanghai.",
            },
        },
        "additionalProperties": False,
    },
    handler=schedule_summary,
)

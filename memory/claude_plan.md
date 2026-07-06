# Execution Plan

I will follow the project task order in `TODO.md` and complete only the first incomplete task in this invocation.

1. Read `TODO.md` to identify the first task whose title is not prefixed with `[DONE]`.
2. Check the latest commit message only for unfinished work directly relevant to that selected task.
3. Inspect the task requirements and the smallest relevant set of project files needed to implement it.
4. Implement the task completely, updating this plan if the implementation path changes or a key step completes.
5. Run the required formatting, linting, and tests in the requested order, addressing any unscheduled failures before marking the task complete.
6. Update `TODO.md` by prefixing the completed task title with `[DONE]` and filling in its completion record.
7. Update `PLAN.md` only if phase-level sequencing or completion criteria changed.
8. Commit all changes for this task with a clear message and the required co-author trailer, then stop without starting the next task.

## Current Task

Selected first incomplete task: `T07` — trigger determination and outbound reply wrapper.

Planned implementation details:

1. Inspect the existing DingTalk message normalization, Stream callback, and `DingTalkClient` send helpers.
2. Add `adapters/dingtalk/outbound.py` with `reply(inbound, text)` that prefers a valid `session_webhook` and falls back to OpenAPI single-chat or group-chat sends based on `conversation_type`.
3. Add trigger helpers so DM text and group @ text are treated as triggered, while unsupported/non-text inbound payloads are logged and receive the standard “暂只支持文本” response.
4. Wire Stream handling through the trigger/outbound behavior without changing the next task’s LLM scope.
5. Add focused unit tests for webhook preference, OpenAPI fallbacks, trigger behavior, and unsupported message handling.

## Progress

- Implemented message metadata for unsupported inbound types and `sessionWebhookExpiredTime`.
- Added DingTalk outbound reply routing with unexpired webhook preference and OpenAPI single/group fallbacks.
- Wired Stream callbacks and the runtime handler to reply with fixed text or `暂只支持文本` as appropriate.
- Added focused tests and updated README Stream behavior notes.
- Validation completed with Ruff, pytest, and entry point startup checks.
- `TODO.md` has been updated to mark `T07` as `[DONE]` with a completion record.

# Execution Plan

I will not record private chain-of-thought, but I will keep this file updated with the actionable plan and progress.

1. Read TODO.md first and identify the first task whose heading is not prefixed with [DONE].
2. Inspect only the files needed to understand that task, plus recent git context if it directly mentions an unfinished issue relevant to the selected task.
3. Implement the selected task completely, adding a prerequisite task to TODO.md only if a concrete blocker makes correct execution impossible.
4. Run formatting, linting, and the relevant/full tests required by TODO.md and the repository workflow.
5. Update TODO.md by prefixing the completed task heading with [DONE] and filling its completion record; update PLAN.md only if phase-level sequencing changes.
6. Commit all changes related to this invocation with a descriptive message and the required co-author trailer, then stop without starting the next task.

## Current Task

Selected first incomplete TODO: T28 confirm 卡片与回调匹配.

Task requirements:
- Add `ctx.confirm(action, details)` so sensitive/runtime-controlled tool actions can send a DingTalk interactive card with confirm/cancel buttons.
- Store an unguessable `correlation_id` in callback data and suspend the session through the existing pending-interaction primitive.
- Register and normalize DingTalk card callback events, then route callbacks by `correlation_id` plus responder to the pending interaction and resolve it without invoking the LLM.
- Ensure confirm-card text comes from tool/runtime inputs rather than LLM-generated wording.
- Validate that a notification-like tool executes only after confirm and does not execute after cancel.

Execution steps:
1. Inspect existing interrupt, agent loop, capability context, DingTalk stream/outbound code, and tests.
2. Design the narrow integration point for `ctx.confirm(...)` using existing service injection and `SessionInterruptManager`.
3. Add DingTalk interactive-card send/callback support and a deterministic callback router.
4. Add or update a small confirm-gated tool/test fixture to prove confirm-before-execute and cancel-does-not-execute behavior.
5. Run formatting, linting, focused tests, then the full test suite.
6. Mark T28 `[DONE]`, update the completion record, commit all task changes, and stop.

## Progress Update

Implemented the T28 core flow:
- Added DingTalk card callback normalization and Stream topic registration.
- Added OpenAPI confirm-card create/deliver support with callback correlation data.
- Added `ctx.confirm(action, details)` suspension in `AgentLoop`, plus approved/cancel callback resolution that bypasses the LLM.
- Added a confirm-gated `send_notification` system capability and targeted tests for approve/cancel behavior.

Focused formatting, linting, and confirm-related tests pass. Next step: update README/TODO, run full validation, and commit.

## Completion Update

T28 is implemented and documented:
- TODO.md marks T28 `[DONE]` with completion details.
- README documents `ctx.confirm(...)`, card callback routing, and the `send_notification` capability.
- Full formatting, linting, pytest suite, and startup smoke validation passed.

Next: inspect the final diff, commit all invocation changes, and stop.

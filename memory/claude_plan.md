# Execution Plan

I identified the first incomplete task in `TODO.md` as **T29 `[TODO]` 取消双来源与系统通告**. This file records the actionable plan and progress updates; it intentionally contains a concise execution plan rather than private chain-of-thought.

## Current task

Implement M5 T29:

- Cancel pending `AwaitingInteraction` from two sources:
  - a new inbound message while awaiting interaction (`superseded_by_new_message`)
  - expiration after `session.confirm_timeout_sec` / 30 minutes (`timeout`)
- Runtime sends the system cancellation notice directly, using content derived from tool input/action details.
- The suspended tool receives `Cancelled`, and the agent finishing turn stays silent except for history/state updates.
- Enforce outbound three-source separation from architecture §8.4b.
- Ensure M4 consent is represented through the unified `consent` interrupt path.

## Step-by-step plan

1. Inspect the latest commit message for directly relevant unfinished T29/M5 notes.
2. Read the current M5 implementation surfaces: interrupt manager, agent loop confirm/consent paths, router/stream inbound flow, outbound adapter, store schema, config timeout, and tests around T27/T28.
3. Map existing pending-interaction behavior and identify the minimal cohesive changes for:
   - new-message cancellation before processing the new message normally
   - timeout cancellation and notification
   - silent agent/tool completion after cancellation
4. Implement the runtime cancellation API in the appropriate core layer, reusing existing persisted interrupt records and session state.
5. Wire new-message cancellation into the inbound message path before agent-loop execution.
6. Add timeout handling tied to interrupt expiration, including direct system notice delivery.
7. Add or update focused tests for:
   - new message cancels pending confirm and then processes normally
   - timeout cancels pending confirm and sends one system notice
   - cancelled tool path does not produce an extra AI cancellation reply
   - consent still uses the unified interrupt storage/model
8. Run formatting, linting, and the relevant/full test suites in the required order.
9. Mark T29 `[DONE]` in `TODO.md` with a completion record.
10. Commit all task changes with a descriptive T29 commit message and the required co-author trailer.

## Progress

- 2026-07-07 08:12: Identified T29 as the first incomplete task and updated this execution plan.
- 2026-07-07 08:20: Implemented runtime cancellation support in `AgentLoop`, wired new-message cancellation and timeout scheduling into inbound handling, and added focused tests for system notices and silent cancellation history.
- 2026-07-07 08:20: Marked T29 `[DONE]`, updated README/TODO, and completed formatting, linting, focused tests, full pytest, and startup smoke validation.
- 2026-07-07 08:21: Refined timeout cancellation to target the scheduled `correlation_id` so old timers cannot affect newer pending interactions in the same Session; re-ran focused tests, full pytest, and startup smoke successfully.

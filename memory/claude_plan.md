# Execution Plan

This file records the current execution plan and progress for this invocation. It summarizes rationale and steps without exposing private chain-of-thought.

I will follow `TODO.md` as the authoritative task order, complete exactly the first task whose heading is not prefixed with `[DONE]`, update the task record, validate the result, commit the completed work, and stop. I will not broaden scope beyond issues that directly block that task.

Step-by-step plan:
1. Read `TODO.md` to find the first incomplete task and its validation requirements.
2. Check the latest commit only for an explicitly unfinished issue that is directly relevant to that task.
3. Inspect the relevant code and tests for the selected task.
4. Implement the required changes without splitting the task unless a concrete prerequisite makes completion impossible.
5. Run formatting, linting, and the relevant/full test commands required by the task and repository conventions.
6. Update `TODO.md` by prefixing the completed task title with `[DONE]` and filling its completion record; update `PLAN.md` only if the phase-level plan changes.
7. Commit all changes related to the completed task, including this progress file if it remains changed, then stop.

Selected task:
- `T12 [TODO] per-session 串行 inbox`: add a per-session asyncio queue and single worker, route inbound messages into the session inbox, and validate that messages in one session run strictly serially while different sessions can run concurrently.

Progress:
- Added `src/core/inbox.py` with per-conversation Session inbox dispatch, Stream runtime enqueue wiring, README behavior notes, and async tests for same-session serial processing plus different-session parallel processing.
- Validation passed with `ruff format`, `ruff check`, `pytest -q`, and `python -m src.main`; `TODO.md` now marks T12 complete with its completion record.

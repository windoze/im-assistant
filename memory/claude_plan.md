# Execution Plan

I will use `TODO.md` as the authoritative task list and complete exactly the first task whose heading is not prefixed with `[DONE]`. I will not include private chain-of-thought here, but this file will track the concrete plan, decisions, and progress.

## Steps

1. Read `TODO.md` to identify the first incomplete task and its validation requirements.
2. Check the latest commit message for directly relevant unfinished work.
3. Inspect the code and tests needed for that task only.
4. Implement the task completely, avoiding workarounds or scope narrowing.
5. Run formatting, linting, and relevant/full tests as required by the task and repository.
6. Update `TODO.md` by prefixing the completed task title with `[DONE]` and filling its completion record.
7. Update this file with key progress.
8. Commit all changes for this task with a descriptive message and the required co-author trailer.
9. Stop without starting the next task.

## Progress

- Identified first incomplete task: `T37 [TODO] 不可信输入边界与可观测`.
- Current task requirements:
  - Enforce high-sensitivity capabilities through runtime guardrails, using `Capability.sensitivity`, without relying on LLM behavior.
  - Add confirm/allowlist protection for high-sensitivity tools.
  - Add observable metrics for message volume, tool calls, authorization success rate, and error rate through structured logs or counters.
  - Validate that high-sensitivity tools always trigger confirm and metrics are visible from logs.
- Baseline validation before code changes passed: `ruff format --check`, `ruff check`, and `pytest -q`.
- Implementation direction:
  - Add a small structured metric counter helper under `src/infra`.
  - Make `AgentLoop` force a confirm interrupt before executing any `sensitivity="high"` capability, even if the handler forgets to call `ctx.confirm`.
  - Preserve existing handler-level confirm behavior after runtime approval without requiring a second card.
  - Emit metrics for inbound messages/errors, capability tool outcomes, and OBO authorization decisions.
- Implemented:
  - Added structured runtime counters emitted as JSON logs.
  - Enforced runtime confirm for every high-sensitivity capability before handler/tool-executor execution.
  - Added metrics for inbound messages, tool outcomes, OBO authorization decisions, and errors.
  - Updated README and marked T37 `[DONE]` in `TODO.md`.
- Final validation passed: `.venv/bin/ruff format .`, `.venv/bin/ruff check .`, `.venv/bin/pytest -q`, and `python -m src.main`.

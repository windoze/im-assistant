# Execution Plan

I will follow `TODO.md` as the authoritative source of work and complete exactly the first task whose heading is not prefixed with `[DONE]`. I will not perform broad unrelated triage before selecting that task.

## Steps

1. Read `TODO.md` to identify the first incomplete task and its stated validation requirements.
2. Check the latest commit message only for unfinished work that is directly relevant to that selected task.
3. Inspect the task-specific implementation area and existing tests.
4. Implement the selected task completely, without narrowing the specification or relying on workarounds.
5. Run formatting, linting, and relevant tests in the required order, escalating to the full suite when code changes require it.
6. If a concrete blocker or unscheduled failing test prevents completion, update `TODO.md` with the minimum prerequisite task and stop after committing that bookkeeping.
7. If the task is completed, mark its title `[DONE]`, update its completion record, and commit all resulting changes with a descriptive message.

## Current Status

Selected first incomplete task: `T34 [TODO] 【REVIEW】M6 指令通道审阅`.

## T34 Review Scope

1. Review T31-T33 implementation for deterministic routing priority: pending interaction, slash command, then agent loop.
2. Verify the command registry remains independent from AI capabilities/tools.
3. Verify command authorization uses the current actor/session roles correctly.
4. Verify `/connect` and `/disconnect` stay consistent with TokenVault and OBO consent state.
5. Verify `inject_message` is the only command mechanism that mutates AI-visible conversation history.
6. Run formatting, linting, and tests; fix any concrete issue found during the review.
7. Mark T34 `[DONE]`, update its completion record, and commit the completed review task.

## Review Findings

1. Group messages using the documented `@助手 /command` syntax were not recognized by the command classifier because it only accepted text whose stripped body started with `/`.
2. Pending `/cancel` handling used separate direct parsing, so the same group mention syntax would be treated as a generic new-message cancellation instead of the explicit cancel command.
3. Command parsing split command names only on literal spaces, so tab/newline-separated command arguments could raise instead of being parsed deterministically.

## Fixes Applied

1. Added shared command extraction that accepts DM `/command` and group leading-mention `/command` syntax.
2. Reused shared extraction for pending `/cancel` detection.
3. Updated command parsing to split on arbitrary whitespace.
4. Added regression tests and updated README command-routing documentation.

## Validation Completed

1. `.venv/bin/ruff format .`
2. `.venv/bin/ruff check .`
3. `.venv/bin/pytest tests/test_router.py tests/test_commands.py tests/test_main.py tests/test_builtin_commands.py -q`
4. `.venv/bin/pytest -q`
5. `python -m src.main`

`TODO.md` has been updated to mark T34 `[DONE]` with a completion record.

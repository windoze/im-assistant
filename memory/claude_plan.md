# Execution Plan

I will keep this file updated with an operational plan and progress notes. I will not record private chain-of-thought here.

1. Read `TODO.md` to identify the first task whose title is not prefixed with `[DONE]`.
2. Check the latest commit only for unfinished work directly relevant to the selected task.
3. Inspect the selected task body, dependencies, validation requirements, and only the directly related files.
4. Implement the selected task completely, or add the minimum prerequisite task in `TODO.md` if a concrete blocker prevents correct implementation.
5. Run validation in the required order: formatting, linting/type checks, then the relevant/full test suite required by the task.
6. Update `TODO.md`: prefix the task title with `[DONE]` and fill the completion record if complete, or leave it incomplete and document any prerequisite/blocker.
7. Update this file at key milestones.
8. Commit all changes for this invocation with a descriptive message and stop without starting the next task.

## Progress

- Refreshed plan for the 2026-07-07 invocation.
- Read `TODO.md`; selected T18 (`首批应用级工具(无 OBO)`) as the first incomplete task.
- Checked the latest commit: T17 implemented Claude tool-use agent loop, directly relevant as the prerequisite for T18 tools.
- Next step: inspect the capability model, registry, DingTalk client, existing tests, and docs needed to implement the three application-level tools.
- Baseline validation passed before code edits: Ruff format check, Ruff lint, and pytest.
- Implementation approach: expose runtime services to capability handlers, pass the DingTalk client into the agent loop, add DingTalk OpenAPI helpers for documents/todos/user union IDs, declare `contact_lookup`, `create_doc`, and `create_todo` under `capabilities/system/`, then cover them with mock OpenAPI and agent-loop tests.
- Implemented runtime service injection, DingTalk document/todo OpenAPI helpers, optional document parent config, and the three system capabilities.
- Added tests for service injection, T18 system capability metadata/handlers, DingTalk document/todo client calls, and document config validation.
- Validation passed after implementation: `.venv/bin/ruff format .`, `.venv/bin/ruff check .`, `.venv/bin/pytest -q`, and `python -m src.main`.
- Marked T18 `[DONE]` in `TODO.md` with completion details.
- Final diff check passed; next step is to commit all changes for this invocation.

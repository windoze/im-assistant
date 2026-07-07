Execution plan for current invocation:
1. Confirm the first incomplete TODO task and inspect the latest commit for a directly relevant unfinished issue before broad triage.
2. For T32, inspect the existing router, session, capability, identity, store, config, and tests needed to add a command registry without disturbing the AI tool registry.
3. Implement `core/commands.py` with a `Command` model, role-aware command registry, deterministic actor authorization, and an `inject_message(session, text)` API that appends command-originated context through the existing session/message storage path.
4. Wire the command registry into the existing slash-command branch so unavailable commands, mode restrictions, argument parsing failures, and authorization failures produce deterministic system replies and never enter the LLM.
5. Add or update focused tests covering registry listing, tool-table separation, role authorization denial, slash-command execution, and injected messages being visible to the next agent turn.
6. Update README only if the new command registry behavior needs user-facing documentation; update TODO.md by prefixing T32 with `[DONE]` and adding a completion record after validation.
7. Run formatting, linting, targeted tests, the full test suite, and `python -m src.main`; fix any unscheduled failures before marking the task done.
8. Commit all task-related changes with a descriptive T32 commit message and stop.

Progress log:
- Read TODO.md and selected first incomplete task: T32 `[TODO]` 指令注册表与鉴权.
- Wrote this execution plan before running shell commands or editing project implementation files.
- Confirmed latest commit is the completed T31 classifier and does not call out a relevant unfinished issue.
- Baseline validation passed before implementation: `ruff format --check`, `ruff check`, and full `pytest`.
- Implemented `src/core/commands.py` with `Command`, `CommandArgsSpec`, `CommandRegistry`, actor-role authorization, direct command dispatch, and SQLite-backed command history injection.
- Exported command primitives through `src.core`, wired an empty `CommandRegistry` into the Stream runtime, updated README, and added focused tests for listing, authorization, argument parsing, mode restrictions, and injection visibility.
- Full validation passed after implementation: `ruff format`, `ruff check`, full `pytest`, and `python -m src.main`.
- Marked T32 `[DONE]` in TODO.md with completion details. No PLAN.md phase-level sequencing changes were needed.

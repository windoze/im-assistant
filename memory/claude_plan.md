Execution plan:
1. Read TODO.md to identify the first task whose heading is not prefixed with [DONE].
2. Inspect only the files and task context needed for that selected task, including recent git state if relevant.
3. Implement the selected task completely, adding or updating tests and documentation required by the task.
4. Run formatting, linting, and the relevant/full validation required by TODO.md.
5. Update TODO.md completion status and completion record for exactly that task; update PLAN.md only if phase-level sequencing changes.
6. Commit all task-related changes with a descriptive message and stop without starting the next task.

Progress log:
- Created this plan before project inspection.
- Selected first incomplete task: T31 `[TODO]` 入站三岔口分类器.
- Current scope: add a deterministic pre-agent router that classifies inbound messages into pending-interaction resolution, slash-command handling, or the existing agent loop, with tests for all three branches.
- Implemented the classifier in `src/core/router.py`, wired it into `handle_inbound_event`, and added unit/integration coverage for pending-priority, slash-command, and agent-loop branches.
- Validation passed: ruff format/check, targeted router/main tests, full pytest suite, and `python -m src.main`.
- Marked T31 complete in TODO.md with the completion record.

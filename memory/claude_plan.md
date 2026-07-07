# Current task execution plan

I will execute exactly the first incomplete task in `TODO.md`, using `TODO.md` as the authoritative source of ordering and requirements. I will not perform broad issue triage before selecting that task.

Selected task: `T39 [DONE] 【REVIEW】M7 加固 + 全系统终审`.

Step-by-step plan:

1. Check the current repository state and latest commit for any directly relevant unfinished issue.
2. Review the M7 hardening surfaces from T35-T38: audit logging, reconnect/idempotency/recovery, high-sensitivity confirm enforcement, metrics, and sandbox status.
3. Compare the implemented behavior against `docs/architecture.md` where T39 calls for architectural alignment.
4. Run validation in the required order: `.venv/bin/ruff format .`, `.venv/bin/ruff check .`, then `.venv/bin/pytest -q`.
5. Fix any concrete in-scope defects or unscheduled test failures found during review/validation.
6. Update `TODO.md` to mark T39 `[DONE]` and add a completion record with review findings, validation results, and any external end-to-end verification limitations.
7. Re-run relevant validation after edits, commit all task-related changes with a T39 commit message, and stop.

Progress:

- Identified first incomplete TODO task: T39 `[TODO]` M7 hardening + full-system final review.
- Latest commit is `[T38] Document sandbox not currently required`; no directly relevant unfinished blocker was mentioned.
- Review found an in-scope M7 robustness gap: if the process exits after claiming an inbound `msg_id` but before marking it processed or releasing it, the persisted `processing` claim can cause DingTalk retries after restart to be skipped as duplicates. I will add startup recovery for stale `processing` inbound claims and cover it with tests.
- Implemented stale inbound-claim recovery in `SQLiteStore`, invoked it during Stream startup, documented the behavior in `README.md`, and added store/startup regression tests.
- Validation passed with `.venv/bin/ruff format .`, `.venv/bin/ruff check .`, and `.venv/bin/pytest -q`. T39 is marked `[DONE]` in `TODO.md` with the review finding, fix, validation, and external-verification limitations recorded.

## Execution plan

I will follow `TODO.md` as the source of truth and complete only the first task whose heading is not prefixed with `[DONE]`.

1. Read `TODO.md` to identify the first incomplete task and its validation requirements.
2. Check the latest commit message for any unfinished issue directly relevant to that task, without doing broad historical triage.
3. Inspect only the files needed for the selected task, then implement the task completely or add the minimum prerequisite task if a concrete blocker makes the selected task impossible.
4. Run formatting, linting, and relevant/full validation required by the task and repository conventions, addressing any unscheduled failures.
5. Update this file at key milestones, update `TODO.md` with the `[DONE]` prefix and completion record if the task is completed, and update `PLAN.md` only if phase-level planning changes.
6. Commit the resulting changes with a descriptive message including the required co-author trailer, then stop.

## Progress

- Selected task: `T20` TokenVault (user-level token encrypted storage) is the first incomplete task in `TODO.md`.
- Latest commit: `[T19] Review M3 capability layer`; it does not mention an unfinished issue that changes the T20 scope.
- Implemented: added `src/infra/token_vault.py`, wired `TOKEN_VAULT_FERNET_KEY` through config, and documented key generation.
- Validated: formatting, linting, full pytest suite, and `python -m src.main` passed.
- Completed: marked `T20` as `[DONE]` in `TODO.md` with the completion record.
- Next: review the diff and commit the task changes.

# Execution Plan

I will follow `TODO.md` as the authoritative source and complete exactly the first task whose heading is not prefixed with `[DONE]`. This file records the actionable plan and progress updates for the current invocation.

## Steps

1. Read `TODO.md` and identify the first incomplete task.
2. Check the latest commit message only for unfinished work directly relevant to that task.
3. Inspect the relevant files and existing tests for the selected task.
4. Implement the task completely, adding or updating tests and documentation when directly required.
5. Run formatting, linting, and relevant/full tests according to the repository’s validation requirements.
6. Update `TODO.md` by prefixing the task heading with `[DONE]` and filling its completion record.
7. Commit all task-related changes, including this progress file.
8. Stop without starting the next task.

## Progress

- Initialized plan before executing repository commands.
- Identified first incomplete task: `T21 钉钉 OAuth2 端点与 code 换 token`.
- Latest commit is `[T20] Implement encrypted TokenVault`, which directly precedes T21 and does not add an unfinished blocker.
- Planned implementation: add `src/infra/oauth.py` with `PendingAuthStore`, DingTalk authorization URL construction, `/oauth/start` and `/oauth/callback` aiohttp handlers, and code-to-user-token exchange; add targeted tests and documentation/config updates.
- Implemented the OAuth module, added aiohttp as a runtime dependency, documented the endpoint contract, and installed the updated editable package.
- Fixed validation issues from the first full test run; formatting, linting, pytest, and the application entry point now pass.

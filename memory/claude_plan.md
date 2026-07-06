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

- Initialized plan before implementation work.
- Identified first incomplete task: `T22 身份核对与 TokenVault 落库`.
- Latest commit is `[T21] Implement DingTalk OAuth code exchange`, which directly precedes T22 and does not mention an unfinished blocker.
- Planned implementation:
  1. Inspect the T21 OAuth callback result and T20 TokenVault APIs.
  2. Add callback identity verification by calling `GET /v1.0/contact/users/me` with the user token and comparing `unionId` to the pending actor identity.
  3. Persist verified OAuth tokens into TokenVault under the pending principal/service/scopes.
  4. Expose a resume hook for the authorized pending session without coupling T22 to later M5 interaction primitives.
  5. Add tests for self-authorization success, cross-account rejection, nonce single-use behavior, TokenVault persistence, and resume callback invocation.
  6. Run formatting, linting, tests, and entry-point validation.
  7. Mark T22 `[DONE]` in `TODO.md`, record completion details, commit, and stop.
- Implemented pending actor identity, `contact/users/me` verification, TokenVault persistence, and resume callback result enrichment in `src/infra/oauth.py`.
- Updated OAuth tests to cover successful self-authorization, rejected cross-account authorization, malformed `users/me` responses, state single-use behavior, real TokenVault persistence, and resume callback payloads.
- Updated README to document the verified OAuth callback and TokenVault write contract.
- Validation completed successfully: `ruff format`, `ruff check`, OAuth tests, full pytest suite, and `python -m src.main`.
- Marked `T22 身份核对与 TokenVault 落库` as `[DONE]` in `TODO.md` with a completion record.

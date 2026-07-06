# T03 Execution Plan

I cannot record private chain-of-thought reasoning, but I will keep this file updated with a concise execution plan and progress notes.

## Scope

- First incomplete task in `TODO.md`: `T03` — DingTalk application-level access token client.
- Deliver `src/infra/dingtalk_client.py` with cached token retrieval, concurrency protection, authenticated GET/POST helpers, structured error logging, tests for token fetch/cache behavior, `TODO.md` completion record, and a Git commit.

## Step-by-step plan

1. Check the latest commit message for unfinished work directly relevant to `T03`.
2. Inspect existing configuration, logging, project metadata, and tests to follow current conventions.
3. Implement `DingTalkClient` with `async get_access_token()`, five-minute early refresh, and `asyncio.Lock` concurrency protection.
4. Implement `api_post` and `api_get` helpers that attach the correct application or user access token header and surface API/HTTP errors.
5. Add focused async tests using mocks to verify token parsing, cache reuse, early refresh, locking behavior as practical, and request helper behavior.
6. Run formatting, linting, tests, and `python -m src.main` in the required order.
7. Mark `T03` as `[DONE]` in `TODO.md` with a completion record.
8. Update this progress file at key milestones.
9. Commit all T03-related changes with the required co-author trailer and stop.

## Progress

- Read `TODO.md` and identified `T03` as the first incomplete task.
- Checked the latest commit; it records T02 completion and does not add a T03 prerequisite.
- Inspected current config, logging, entry point, tests, README, and project metadata.
- Baseline validation passed before implementation: `ruff format --check`, `ruff check`, and `pytest`.
- Implemented `DingTalkClient` with application-token fetch/cache/early-refresh locking, GET/POST helpers, and structured API error handling.
- Added async mock tests for token caching, early refresh, concurrency, application/user token request headers, and errcode/errmsg logging.
- Validation passed after implementation: `ruff format`, `ruff check`, `pytest`, and `python -m src.main`.
- Marked `T03` as `[DONE]` in `TODO.md` with a completion record.

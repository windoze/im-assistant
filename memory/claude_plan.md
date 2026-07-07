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

Selected first incomplete task: `T36 错误恢复与鲁棒性`.

## T36 Scope

1. Inspect current Stream, outbound messaging, access-token, session, store, and interrupt persistence code.
2. Implement Stream reconnect with exponential backoff.
3. Implement inbound message idempotency by `msg_id`.
4. Ensure access-token invalidation triggers a fresh token and retry for outbound/API calls that fail due to token expiry.
5. Add outbound message rate limiting to prevent rapid repeated sends.
6. Restore session state and pending interactions from SQLite during process startup.
7. Add or update tests for reconnect, deduplication, token refresh retry, rate limiting, and restart recovery.
8. Run formatting, linting, and relevant/full tests as required.
9. Mark T36 `[DONE]` with a completion record and commit all task changes.

## Current Progress

Implemented the T36 robustness layer:

1. Added SQLite persistence for inbound message idempotency and pending OAuth authorization state.
2. Added restart recovery for persisted Session state and pending interaction context.
3. Wired runtime startup to use persistent OAuth pending state, recover sessions, restore pending timeouts, and skip duplicate inbound `msg_id`s.
4. Added Stream reconnect with exponential backoff.
5. Added app access-token invalidation retry for DingTalk OpenAPI calls.
6. Added outbound reply rate limiting.
7. Added focused tests for persistence, OAuth restart, inbound deduplication, session recovery, Stream reconnect, token retry, and outbound rate limiting.
8. Updated README with the new recovery/idempotency/retry guarantees.

Validation so far:

1. `.venv/bin/ruff format .`
2. `.venv/bin/ruff check .`
3. `.venv/bin/pytest tests/test_store.py tests/test_oauth.py tests/test_main.py tests/test_dingtalk_stream.py tests/test_dingtalk_client.py tests/test_dingtalk_outbound.py -q`
4. `.venv/bin/pytest -q`
5. `python -m src.main`

`TODO.md` has been updated to mark T36 `[DONE]` with its completion record. Next step: commit T36.

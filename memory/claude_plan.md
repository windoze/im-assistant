# Execution Plan

Current task: `T23 [DONE] 静默刷新` from `TODO.md`.

1. Inspect `infra/dingtalk_client.py`, `infra/token_vault.py`, OAuth-related code, and existing tests to understand current user-token storage and DingTalk API request patterns.
2. Add refresh-token support for expired or near-expired user tokens using DingTalk `grantType=refresh_token`. **Completed.**
3. Integrate refresh with TokenVault so callers can obtain a usable user token when refresh succeeds, and so refresh failure clears the vault entry and reports that re-authorization is required. **Completed.**
4. Add focused unit tests for automatic refresh success and refresh-token invalidation/fallback behavior. **Completed.**
5. Run `ruff format`, `ruff check`, and the test suite. **Completed.**
6. Update `TODO.md` with `[DONE]` and a completion record, then commit this task only. **Completed.**

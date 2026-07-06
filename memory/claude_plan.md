# Execution Plan

Current task: `T24 [DONE] Authorizer 三态与 CredentialContext` from `TODO.md`.

1. Inspect the current capability, agent loop, session, OAuth, TokenVault, outbound, and test surfaces that T24 must connect. **Completed.**
2. Implement `capabilities.authorizer` with `Granted`, `NeedsConsent`, and `Denied`, using `TokenVault.get_valid(...)` for valid or refreshable OBO tokens, denying OBO requirements outside DM, and creating pending OAuth consent URLs when authorization is missing. **Completed.**
3. Implement `capabilities.credential.CredentialContext`, exposing user/group identity helpers and resolving application-level versus user-level credentials for tool handlers. **Completed.**
4. Wire authorizer and credential context into `AgentLoop` before tool execution: resolve each requirement, suspend the session and send a consent link on `NeedsConsent`, return a denial result for `Denied`, and pass credentials to handlers through the existing runtime context. **Completed.**
5. Add focused tests for the three authorizer states, credential context behavior, and agent-loop suspension plus consent-link sending. **Completed.**
6. Run formatting, linting, and tests; fix any unscheduled failures observed. **Completed.**
7. Mark T24 `[DONE]` in `TODO.md` with a completion record, update this file at key milestones, commit the task, and stop. **Completed.**

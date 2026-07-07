# Execution Plan

I identified the first incomplete task in `TODO.md` as **T30 `[TODO]` 【REVIEW】M5 带外交互审阅**. This file records the actionable plan and progress updates; it intentionally contains a concise execution plan rather than private chain-of-thought.

## Current task

Review and validate M5 T27-T29:

- `correlation_id` must be unpredictable and card callbacks must validate responder identity.
- Confirm/consent interrupt creation, persistence, resolution, cancellation, and timeout behavior must be unified.
- Runtime system cancellation notices must be separate from AI replies, and cancellation completion must stay silent.
- Pending interaction records must be persisted and recoverable enough for the documented runtime behavior.
- End-to-end-style tests should cover confirm, timeout cancellation, and new-message cancellation paths.

## Step-by-step plan

1. Inspect the latest commit message for directly relevant unfinished M5/T30 notes.
2. Read M5 implementation surfaces: interrupt primitives, agent loop confirm/consent and cancellation paths, main inbound routing/timeout scheduling, DingTalk card callback normalization, outbound client methods, persistence schema, and existing M5 tests.
3. Compare the implementation against T30 review criteria and architecture §8.4/§8.4b.
4. Fix any high-confidence defects found during the review; if no production defect is found, add missing regression coverage required by T30.
5. Run formatting, linting, focused M5 tests, and full pytest in the required order.
6. Update `TODO.md` by marking T30 `[DONE]` and adding the completion record with the review findings and validation.
7. Commit all changes for T30 with the required co-author trailer, then stop.

## Progress

- 2026-07-07 08:22: Identified T30 as the first incomplete task and updated this execution plan.
- 2026-07-07 08:27: Review found a T30-relevant recovery gap: pending interactions are persisted, but timeout cancellation tasks are only scheduled for interactions created in the current process. I will add persisted-pending listing, synthesize OpenAPI reply targets from stored Sessions, schedule recovered timeouts on Stream startup, and add regression coverage.
- 2026-07-07 08:29: Implemented recovered timeout scheduling, added focused tests, updated README/TODO, and completed formatting, linting, focused tests, full pytest, and startup smoke validation.

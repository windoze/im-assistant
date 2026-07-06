# Execution Plan

I will follow `TODO.md` as the source of truth and complete only the first task whose heading is not prefixed with `[DONE]`.

1. Read `TODO.md` to identify the first incomplete task and its stated validation requirements.
2. Review only the files and context needed for that task, including the latest commit if it directly mentions unfinished work relevant to the selected task.
3. Implement the task as specified, adding any prerequisite task to `TODO.md` only if a concrete blocker makes direct completion impossible.
4. Run formatting, linting, and relevant tests according to the project’s existing tooling and the task’s validation requirements.
5. Update this progress file at key milestones.
6. Mark the completed task in `TODO.md` with a `[DONE]` prefix and update its completion record.
7. Commit all changes for this invocation with a descriptive message, then stop without starting the next task.

## Progress

- 2026-07-07 invocation started. I will re-read `TODO.md` as the authoritative task source, identify the first heading not prefixed with `[DONE]`, and then either complete exactly that task or add/commit the minimum prerequisite if a concrete blocker prevents completion.
- Selected task: T26 `【REVIEW】M4 OBO 审阅`. I will review the T20-T25 OBO implementation, fix any concrete defects found, run the required formatter/linter/tests, mark T26 `[DONE]`, and commit only this invocation's changes.
- T26 review finding: refresh-token rejection classification treated any 400/401/403 OAuth refresh error as a revoked user grant. I narrowed rejection detection to explicit refresh-token/invalid-grant signals and added DingTalk client plus TokenVault regressions so unrelated refresh failures keep stored OBO grants.
- Validation passed and T26 is marked `[DONE]` in `TODO.md`; next step is final status inspection and commit.
- Selected task: T25 `OBO 工具:今日日程总结(招牌 case)`.
- Latest commit checked: no unfinished issue relevant to T25 was indicated.
- Next: inspect existing capability, credential, DingTalk client, and LLM patterns; then add the schedule-summary capability, tests, docs, TODO completion record, and commit.
- Relevant prerequisite identified: the existing visibility gate hides system capabilities declared as `available_in=["dm"]`, while T25 and architecture §6.4 require `schedule_summary` to be visible in DM and hidden in group. I will fix the gate generally so DM-capable system/base tools are visible in DM and group-only tools still require channel enablement.
- Implemented: corrected visibility semantics, added DingTalk primary-calendar/event-list wrappers, added `schedule_summary` with `calendar:read` OBO requirement and LLM summarization, wired `llm_client` into capability services, and added focused regression tests/docs.
- Validation completed successfully: formatter, lint, test suite, and `python -m src.main`.
- T25 has been marked `[DONE]` in `TODO.md` with a completion record.
- Next: inspect final git status and commit all changes for this invocation.

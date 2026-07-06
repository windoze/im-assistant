# Execution Plan

This file records the current execution plan and progress for this invocation. It intentionally summarizes rationale and steps without exposing private chain-of-thought.

## Current Plan

1. Read `TODO.md` first and identify the first task whose title is not prefixed with `[DONE]`.
2. Inspect the relevant project files for that task only, plus `PLAN.md` or recent commit context only if it is directly needed for the selected task.
3. Implement the selected task completely, preserving existing project conventions and avoiding unrelated changes.
4. Run the required formatting, linting, and tests specified by the task and repository workflow.
5. If unscheduled failures or blockers appear, fix them when in scope or add the minimum prerequisite task to `TODO.md`, then stop.
6. Mark the completed task title with `[DONE]`, update its completion record, and update this plan file at key milestones.
7. Commit all changes for this task with a descriptive message including the required co-author trailer.

## Progress

- Plan initialized before repository commands.
- Identified first incomplete task: `T11 [TODO] Session 抽象与路由`.
- Baseline formatting, linting, and tests pass before T11 changes.
- Implemented persistent Session domain/routing, wired Stream handling through it, and verified formatting, linting, tests, and startup smoke.

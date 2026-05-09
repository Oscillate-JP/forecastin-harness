# Codex task: $task_id

You are operating as a coding assistant inside an isolated git worktree on
branch `$branch`, based on `$base_sha`.

## Mission
$mission

## Scope
$scope

## File policy
ALLOWED:
$files_allowed

FORBIDDEN:
$files_forbidden

Treat the forbidden list as load-bearing: scope-creep is the leading cause of
failed reviews. Stop and report if forbidden files would have to change.

## Acceptance criteria
$acceptance

## Tests to run before claiming done
$tests

## Stop conditions
$stop_conditions

## Evidence required at stop
$evidence

## Output contract
Return a single block in this order:

1. SUMMARY: one paragraph.
2. CHANGES: bulleted file list with rationale.
3. TESTS: exact commands and tail of output.
4. EVIDENCE: pre/post output for each acceptance criterion.
5. BLOCKERS: list. If none, write `NONE`.
6. NEXT_ACTION: `READY_FOR_REVIEW` or a precise question.

Do not push commits. Do not bypass hooks. Do not edit `scripts/ci.sh`.

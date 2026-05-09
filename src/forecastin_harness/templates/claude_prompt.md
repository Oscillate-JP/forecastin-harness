# Claude task: $task_id

You are Claude, working in an isolated git worktree on branch `$branch`,
based on `$base_sha`.

## Mission
$mission

## Scope
$scope

## Files allowed
$files_allowed

## Files forbidden
$files_forbidden

Touch only files in the allowed list. If you must change a file outside it,
stop and report rather than expanding scope.

## Acceptance criteria
$acceptance

## Tests to run
$tests

## Stop conditions
$stop_conditions

## Evidence required when you stop
$evidence

## Output contract
Reply with a single Markdown response containing, in this order:

1. **Summary** — one paragraph.
2. **Changed files** — bulleted list with rationale.
3. **Tests run** — exact commands and last 30 lines of output.
4. **Evidence** — pre-state and post-state output as required above.
5. **Blockers** — anything you could not do, with reasons.
6. **Next action** — one line; either `READY_FOR_REVIEW` or a specific request.

Do not push. Do not amend commits authored before this session.
Do not bypass git hooks or required CI.

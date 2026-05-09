# Project rules: forecastin-harness

This is the **harness** repository (`Oscillate-JP/forecastin-harness`).

It is **not** the Forecastin product repository (`Oscillate-JP/Forecastin`).

Anything you read here applies to building, testing, and shipping this
harness. It does not apply to building or shipping Forecastin.

## What this repo is

A small Python package that orchestrates AI-coding agents (Claude, Codex,
Gemini) against a target repository, primarily Forecastin. It manages
worktrees, agent prompts, long-running CI gates, and a PR review gate. It
never edits Forecastin product code from inside this repo.

## Forecastin-product gates DO NOT apply here

When working in this repo, **ignore** the following acceptance signals
unless a task explicitly invokes the Forecastin adapter and runs *against*
a Forecastin checkout:

* `scripts/claude/full_gate.sh` and the rest of `scripts/claude/*` from the
  Forecastin product repo.
* `.claude/state/merge-ready.json` written by Forecastin's harness skills.
* Forecastin dev containers and their backend/frontend test gates.
* Forecastin pre-push `bash scripts/ci.sh`.

Those are correct gates for **Forecastin** product PRs. They have no bearing
on whether code in *this* repo is correct. The acceptance signal here is:

```
python -m pytest
```

…with the dependencies declared in `pyproject.toml` installed via
`pip install -e ".[dev]"`. If you cannot run pytest locally, surface that
and stop; do not invent a Forecastin-shaped substitute gate.

## Hard rules

1. **Never mutate Oscillate-JP/Forecastin from this repo.** Tests that
   exercise the Forecastin adapter must run against a fixture or an
   operator-supplied checkout, never against the live `J:/Forcastin`
   working copy unless the operator has explicitly opted in.
2. **Never vendor Forecastin product code.** Examples in docs are fine;
   imports are not.
3. **Never use `--no-verify`.** This repo does not yet ship a pre-commit
   config; that is by design. When one is added, do not bypass it.
3a. **Never evade hooks, guards, or scanners.** Do not work around a hook,
   guard, scanner, or audit gate by using indirect subprocesses, renamed
   commands, encoded strings, alternate shells, file-redirected commit
   messages chosen to dodge a regex, or any other wording trick. If a
   guard blocks an action, stop and report. This applies whether the
   guard belongs to this repo or to a parent repo whose hooks happen to
   intercept your commands. The right response to a misfiring guard is
   to surface the conflict, not to rephrase the call until it stops
   matching. Pre-authorisation to ignore the *content* of a guard (for
   example, "Forecastin merge-ready reminders do not apply to harness
   work") never authorises evading the guard's *mechanism*.
4. **Default to dry-run** for every state-changing CLI command. The
   operator opts out by dropping `--dry-run`.
5. **No secrets in tests.** All tests must run offline. `gh` integration
   uses static fixtures or mocked subprocess output.
6. **JSON / JSONL state only.** All persisted state must be machine-readable
   and human-diffable.
7. **British English in docs.** Code comments may use whatever spelling
   convention is local; keep prose user-facing-quality.
8. **Pure-function planners separated from side-effect appliers.** The
   pattern `plan_X` (pure) → `apply_X` (writes state, runs subprocesses)
   makes dry-run trivial and tests cheap.

## Controller-lane rules (FOR-220 lessons)

The harness is an **assisted guardrail, not an autonomous control plane**.
Operators run lanes; the harness refuses to authorise actions that the
FOR-220 incident showed were dangerous. These rules are mechanically
enforced by `forecastin-harness controller validate` against every task
packet before a lane runs.

1. **One lane = one objective.** A task packet declares exactly one
   `lane_type` (`feature`, `gate-fix`, `review`, or `controller`) and the
   `files.allowed` list must not mix feature paths (`backend/app/**`,
   `frontend/src/**`) with gate-repair paths (`scripts/ci.sh`,
   `scripts/claude/**`, `.github/workflows/**`, `Makefile`,
   `infrastructure/docker/**`). Feature work and gate repair go into
   separate PRs.
2. **Every lane has a wall-clock budget.** `max_runtime_minutes` must be
   set to a positive integer. There is no "stand by" or "indefinite
   loop" mode.
3. **Every lane names the official gate.** `official_gate_command` is
   the single, named command that decides PASS / FAIL for the lane.
   Diagnostic pytest enumeration (e.g. `pytest --collect-only`) is
   **not** the official gate and must not be confused with one.
4. **Diagnostic commands are explicit.** `diagnostic_commands_allowed`
   lists what the agent may run for triage. Anything else is
   out-of-scope.
5. **Forbidden files are required.** `files.forbidden` must be
   non-empty so the agent always has at least one tripwire.
6. **Merge authority is human-only.** `merge_authority: "human-only"`.
   The harness will plan and render `gh pr merge` commands but never
   execute them autonomously, even from inside a controller lane.
7. **No bypass permissions.** `no_bypass_permissions: true`. The
   validator additionally scans every string in the packet and rejects
   any occurrence of `--no-verify`, `bypass-permissions`,
   `ignore-hooks`, `skip-hooks`, or `--no-gpg-sign`.
8. **Final-report contract.** Every packet declares a `final_report`
   list whose entries collectively name `changed files`, `tests`, and
   `remaining risks`. Operators always get the same shape of report.
9. **Long jobs are managed.** Background shells, orphan pytest
   processes, and forgotten `gate start` runs are surfaced by
   `forecastin-harness controller doctor`. Run it before opening a new
   lane if the host has been doing other work.

The validator is intentionally fail-closed: any missing or wrong field
yields a non-zero exit and a named code (`mixed_feature_and_gate_fix`,
`autonomous_merge_not_allowed`, `bypass_token_detected`, ...) so
automation can refuse to launch a lane until the packet is fixed.

## Subprocess safety

* Use `shell=False` everywhere except the user-supplied gate command. That
  command is a documented trust boundary and runs exactly once via
  `subprocess.Popen`, never re-shelled.
* No `os.system`, no `eval`, no shell-expansion against unsanitised input.

## Branch + commit conventions

* Conventional Commits: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`,
  `chore:`.
* One logical change per commit. v0.2 ships as a single feature commit per
  the operator's request, but follow-ups should split as scope grows.
* Branch naming for follow-up work: `feat/<short-slug>` or
  `fix/<short-slug>`. The harness's own `lane/*` branches are conceptual,
  not used inside this repo.

## When the harness is invoked AGAINST Forecastin

When an operator points the harness at Forecastin via
`configs/forecastin.example.yaml`, Forecastin's hard rules become operative
in the worktrees the harness creates. That is the point — the harness
faithfully runs Forecastin's `scripts/ci.sh`, respects its hooks, and
records merge-ready records keyed to Forecastin PR SHAs. None of that
infects this repo's own gates; it lives entirely on the operator's side
of the adapter boundary.

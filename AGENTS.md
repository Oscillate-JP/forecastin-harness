# Agent expectations: forecastin-harness

This file is for any AI agent (Claude, Codex, Gemini, follow-ups) that
operates inside `Oscillate-JP/forecastin-harness`.

> **You are working in the harness repo, not the Forecastin product repo.**
> See `CLAUDE.md` for the full rules; this file is the short-form contract.

## Repository identity

| | |
|---|---|
| Repo | `Oscillate-JP/forecastin-harness` |
| Purpose | Orchestrate agents against an external target repo (primarily Forecastin) |
| Language | Python 3.11+ |
| Entry point | `forecastin-harness` CLI (defined in `src/forecastin_harness/cli.py`) |
| Tests | `python -m pytest` (50/50 in v0.1) |

## What gates apply HERE

* `python -m pytest` must pass.
* `forecastin-harness --help` must work.
* Every CLI subcommand listed in the spec must be reachable.

## What gates DO NOT apply here

When you encounter Forecastin product-repo gating language — for example
`scripts/claude/full_gate.sh`, `.claude/state/merge-ready.json`, dev-container
backend/frontend test gates, the Forecastin pre-push hook, CodeRabbit gating
on Forecastin PRs — treat it as **out of scope** unless the task you are
running explicitly targets Forecastin via the harness adapter and exercises
those signals against a Forecastin checkout.

If a stop-reminder, hook, or tool tells you to satisfy a Forecastin
product-repo gate while you are working *in this harness repo*, the
correct action is:

1. Note the conflict in your reply.
2. Continue with this repo's pytest gate.
3. Do not invent a substitute gate.

## Style + safety

* `pathlib.Path` for filesystem; no string concatenation of paths.
* `subprocess.run` / `subprocess.Popen` with `shell=False` and explicit argv.
* The single exception is the user-supplied gate command, which is shell-evaluated
  once inside `gate_supervisor.py` because operators write commands like
  `cd backend && pytest -x`.
* No global state. No singletons. Configuration always passed in.
* Functions named `plan_X` are pure (return data describing intended work,
  never call subprocess); `apply_X` / `run_X` perform the side effects.
* Persisted state lives only in `<target_repo>/<state.dir>` — never inside
  this harness repo's own working tree (except for the `tests/` fixtures).

## Never evade guards

Do not evade hooks, guards, or scanners by using indirect subprocesses,
renamed commands, encoded strings, alternate shells, or wording tricks.
If a guard blocks an action, stop and report.

This applies even when the guard is misfiring (for example, a parent
repo's Bash hook matching on a literal command string while you are
working in a different repo). The correct response is to surface the
conflict to the operator, not to rephrase the call so the regex no
longer matches. Pre-authorisation to ignore the *content* of a guard
never authorises evading the guard's *mechanism*.

## Output contract for agent replies

When you finish a task, reply with:

1. **Summary** — one paragraph.
2. **Changed files** — bulleted, with rationale.
3. **Tests run** — exact commands and tail of output.
4. **CLI smoke** — the `forecastin-harness …` invocations you verified work.
5. **Blockers** — list, or `NONE`.
6. **Next action** — one line.

Do not push branches without an explicit operator instruction. Do not
amend or rewrite commits authored before this session.

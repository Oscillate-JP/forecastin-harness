# Patch plan — Forecastin merge_guard root fix (F)

This patch plan covers an integration change in the **Forecastin
target repo**, not in `forecastin-harness`. The harness must NOT
edit `J:/Forcastin` per operator policy. The plan below is the
prompt / PR template the operator (or a future agent with explicit
authorisation to edit Forecastin) executes.

## Symptom

`scripts/claude/merge_guard.py` resolves the repo root via
`Path(__file__).resolve().parents[2]`. When Claude is launched in
the legacy `J:/Forcastin` checkout, the guard reads the
`.claude/state/merge-ready.json` from THAT checkout — even when the
operator is editing in `J:/Forecastin-worktrees/_coordinator-main`.
The state file at the legacy checkout is stale, so every merge
either blocks falsely or, worse, allows a merge against state that
does not match the worktree HEAD.

## Patch shape

Update `scripts/claude/merge_guard.py` to accept an explicit repo
root and expected HEAD via env vars or a lightweight argv parser:

```python
# scripts/claude/merge_guard.py — sketch
import argparse, os, sys
from pathlib import Path

def _resolve_repo_root() -> Path:
    # Priority order:
    # 1. CLAUDE_REPO_ROOT env var (preferred — Claude can set this
    #    once at session start so every guard call uses the same root).
    # 2. --repo-root argv flag (for ad-hoc operator invocation).
    # 3. Path(__file__).resolve().parents[2] (legacy fallback).
    env = os.environ.get("CLAUDE_REPO_ROOT")
    if env:
        return Path(env).resolve()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--expected-head", default=None)
    args, _ = parser.parse_known_args(sys.argv[1:])
    if args.repo_root:
        return Path(args.repo_root).resolve()
    return Path(__file__).resolve().parents[2]
```

`merge-ready.json` is then read from the resolved root instead of
the legacy parents-walk. The expected-head check becomes:

* If `--expected-head` was passed, it overrides the live `git HEAD`.
  This is the safe form for hooks called from a worktree where
  `cwd` may not match the merge_guard script directory.

## Repository policy in CLAUDE.md

Add a paragraph to `J:/Forcastin/CLAUDE.md` (or the merged-target's
CLAUDE.md):

> When invoking `scripts/claude/merge_guard.py`, set
> `CLAUDE_REPO_ROOT=<absolute path to the worktree>`. Do not rely
> on the script's parents-walk fallback — it is preserved only for
> backward compatibility with old hooks and will be removed in a
> follow-up.

## PR prompt (for operator handoff)

```text
Title: fix(merge_guard): resolve repo root from CLAUDE_REPO_ROOT, not parents-walk

Body:

## Problem
merge_guard.py walks Path(__file__).parents[2] to find the repo
root. When Claude launches in J:/Forcastin instead of the
canonical worktree, the guard reads stale .claude/state/merge-ready.json,
either blocking valid merges or allowing merges against the wrong HEAD.

## Fix
Resolve repo root from CLAUDE_REPO_ROOT first, then --repo-root argv,
then fall back to parents[2]. Add --expected-head argv flag so callers
in worktrees can pass HEAD explicitly.

## Tests
Add scripts/claude/test_merge_guard_root.py covering:
- env var wins over argv
- argv wins over parents-walk
- parents-walk used when neither env nor argv set
- expected-head overrides git HEAD

## Operator update
CLAUDE.md gains a one-line policy statement: hooks must set
CLAUDE_REPO_ROOT to the worktree path.
```

## Why this is not in the harness

The harness is repo-agnostic. The merge_guard lives in Forecastin's
`scripts/claude/`. Editing Forecastin from the harness violates the
"Do not touch J:/Forcastin" policy. The harness contributes:

* `forecastin-harness preflight root` — refuses to proceed when cwd
  is wrong (pre-empts the failure earlier).
* `forecastin-harness preflight merge-evidence` — refuses to accept
  banner text as merge proof (compensates for the failure mode if
  it slips through).

The Forecastin-side patch above is the durable fix.

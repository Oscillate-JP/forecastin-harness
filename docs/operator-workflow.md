# Operator workflow

This is the day-1 workflow for using `forecastin-harness` against
`Oscillate-JP/Forecastin`. Everything assumes Windows PowerShell; bash-on-WSL
substitutions are obvious.

> **v0.3 fail-closed default.** Every state-changing subcommand below is
> **plan-only by default**. To actually mutate state or contact GitHub, add
> `--apply` (and for `pr merge`, also `--execute --operator-confirmed`).
> The legacy `--dry-run` flag from v0.2 has been removed.

## Prerequisites

* Python 3.11+ on PATH.
* The Forecastin clone at a known path (default example: `J:/Forcastin`).
* `git` on PATH.
* `gh` on PATH **only** if you intend to use the live PR gate. Local
  validation does not require `gh`.

## One-time setup

```powershell
git clone https://github.com/Oscillate-JP/forecastin-harness J:/forecastin-harness
cd J:/forecastin-harness
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]

# Verify installation
forecastin-harness --version
```

Edit `configs/forecastin.example.yaml` so `target.path` and `worktrees.root`
match your machine, then validate:

```powershell
forecastin-harness init `
  --target-repo J:/Forcastin `
  --config configs/forecastin.example.yaml
```

`init` is idempotent. Re-running it never destroys state; it only ensures
the layout exists and emits a `harness.init` event in the audit log.

`init` now refuses to run if `--target-repo` (after path normalisation) does
not equal `config.target.path` (also normalised). The error names both
paths so the operator can edit one to match the other. This catches the
common bug where the operator points the harness at the wrong checkout.

## Day-to-day loop

### 1. Plan a lane

```powershell
forecastin-harness lanes create `
  --config configs/forecastin.example.yaml `
  --name for-235-settings `
  --task FOR-235 `
  --scope "repair /settings/groups 500"
```

The output prints the exact `git worktree add` command the harness would
run. **Read it.** Confirm the branch name, the worktree path, and the base
branch are what you expect. Then re-run with `--apply` to actually create
the lane:

```powershell
forecastin-harness lanes create `
  --config configs/forecastin.example.yaml `
  --name for-235-settings `
  --task FOR-235 `
  --scope "repair /settings/groups 500" `
  --apply
```

### 2. Render an agent prompt

Copy `templates/task_packet.yaml` to a per-task file (e.g. under
`<target>/.harness/state/tasks/<task-id>.yaml`) and edit it for this task.
Then:

```powershell
forecastin-harness task render `
  --task-file J:/Forcastin/.harness/state/tasks/for-235.yaml `
  --agent claude
```

Pipe the output into your agent of choice. The same packet renders for
`--agent codex` and `--agent gemini`.

### 3. Run a long gate

Once an agent has produced a diff, run the canonical CI gate. **v0.2
spawns the gate process itself** under a supervisor with a wall-clock
deadline; the operator no longer runs the command by hand.

```powershell
# Plan only is the default — always inspect first:
forecastin-harness gate start `
  --config configs/forecastin.example.yaml `
  --name backend-pytest `
  --command "bash scripts/ci.sh" `
  --deadline-seconds 5400
```

Add `--apply` to actually run it. The CLI streams status while the
supervisor blocks until terminal:

```powershell
forecastin-harness gate start `
  --config configs/forecastin.example.yaml `
  --name backend-pytest `
  --command "bash scripts/ci.sh" `
  --deadline-seconds 5400 `
  --apply
```

When the gate finishes (or times out) the CLI prints `gate <name> -> <status>`,
exit code, and timeout flag. Inspect persistent state and logs:

```powershell
forecastin-harness gate status     --config configs/forecastin.example.yaml --name backend-pytest
forecastin-harness gate tail       --config configs/forecastin.example.yaml --name backend-pytest --lines 200
forecastin-harness gate summarise  --config configs/forecastin.example.yaml --name backend-pytest
```

`summarise` returns a single JSON blob with status, exit code, log path,
log size, and transition count. It's intended for piping into another
shell command (e.g. `... | jq .status`).

To cancel a running gate from another shell:

```powershell
forecastin-harness gate stop --config configs/forecastin.example.yaml --name backend-pytest
```

### 3a. Why these gates are separate from Forecastin's gates

Forecastin product PRs gate on `scripts/ci.sh`, the pre-push hook, and
CodeRabbit. Those gates are correct *for Forecastin*. The harness's gate
supervisor is **not a substitute** for them — it's a wrapper that runs
the same `bash scripts/ci.sh` (or whatever the adapter says) under a
deterministic deadline + log path so an agent doing many parallel runs
doesn't lose state when its shell dies.

Conversely, the harness has its own pytest gate (`python -m pytest` in
this repo). When you are working on the *harness itself*, only that gate
applies; do not invoke Forecastin product hooks against harness changes.

### 4. Open the PR (manually)

The harness does not push branches and does not call `gh pr create`. You
push and create the PR by hand:

```powershell
git -C <lane-worktree> push -u origin <branch>
gh pr create --base main --head <branch> --title "<title>" --body "<body>"
```

This keeps the harness off the critical path of opening PRs, where mistakes
(wrong base, wrong title, accidentally drafting) are expensive to undo.

### 5. Validate the PR

```powershell
# Plan-only first (default) to see exactly what the gate will check:
forecastin-harness pr check `
  --config configs/forecastin.example.yaml `
  --pr 2788 `
  --head-sha 8f91dcf3

# Then run live with --apply (requires gh):
forecastin-harness pr check `
  --config configs/forecastin.example.yaml `
  --pr 2788 `
  --head-sha 8f91dcf3 `
  --apply
```

If every outcome is `pass`, the harness writes
`<state>/pr/2788.merge_ready.json` containing a *suggested* merge command.
You can let the harness render and (with explicit confirmation) execute
that merge:

```powershell
# Plan-only (default — prints the rendered command and exits):
forecastin-harness pr merge `
  --config configs/forecastin.example.yaml `
  --pr 2788 --head-sha 8f91dcf3

# Same effect: --apply alone still does NOT execute, only acknowledges
# you've inspected the plan. Use this as the "I've read it, show me again"
# step before adding --execute:
forecastin-harness pr merge `
  --config configs/forecastin.example.yaml `
  --pr 2788 --head-sha 8f91dcf3 --apply

# Actually merge — all three flags required:
forecastin-harness pr merge `
  --config configs/forecastin.example.yaml `
  --pr 2788 --head-sha 8f91dcf3 `
  --apply --execute --operator-confirmed
```

Refusal paths the harness enforces:
* missing `merge_ready.json` → exit 2 with explanation
* recorded SHA != supplied SHA → exit 2 with both SHAs
* recorded verdict not in {`merge_ready`, `approved`} → exit 2
* `--apply --execute` without `--operator-confirmed` → exit 2
* `gh` not on PATH and `--apply --execute` requested → exit 2

The rendered `gh pr merge` command always includes
`--repo <target.repo>`. This makes the merge target explicit and prevents
the all-too-common mistake of running it from a different checkout, where
`gh` would otherwise default to that checkout's origin.

### 6. Retire the lane

After merge:

```powershell
forecastin-harness lanes retire `
  --config configs/forecastin.example.yaml `
  --name for-235-settings
# then for real
forecastin-harness lanes retire `
  --config configs/forecastin.example.yaml `
  --name for-235-settings `
  --apply
```

Retirement only updates state. Worktree removal on disk is a deliberate
operator step (`git -C <target> worktree remove <path>`) so dirty worktrees
are never silently force-removed.

## Recovery

* **Agent shell crashed mid-task.** `lanes list` will still show the lane
  with whatever status was recorded. The worktree is intact on disk.
  Re-render the prompt and resume.
* **Long gate killed by IDE timeout.** The state file at
  `<state>/gates/<name>.state.json` retains the last recorded status. The
  `.log` file is whatever the planned shell command wrote. Re-arm the gate
  with another `gate start` after the operator confirms the run is no
  longer alive.
* **PR HEAD changed mid-review.** `pr check` will surface a `head-sha-match`
  failure. The operator must re-issue `pr check` with the new SHA after
  re-validating that the new commits are still in scope.

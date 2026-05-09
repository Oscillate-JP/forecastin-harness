# Operator workflow

This is the day-1 workflow for using `forecastin-harness` against
`Oscillate-JP/Forecastin`. Everything assumes Windows PowerShell; bash-on-WSL
substitutions are obvious.

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

## Day-to-day loop

### 1. Plan a lane

```powershell
forecastin-harness lanes create `
  --config configs/forecastin.example.yaml `
  --name for-235-settings `
  --task FOR-235 `
  --scope "repair /settings/groups 500" `
  --dry-run
```

The output prints the exact `git worktree add` command the harness would
run. **Read it.** Confirm the branch name, the worktree path, and the base
branch are what you expect. Then re-run without `--dry-run` to apply.

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

Once an agent has produced a diff, run the canonical CI gate. v0.1 plans
the gate but does not spawn it; the operator runs the printed command in a
separate terminal:

```powershell
# Plan only (recommended first):
forecastin-harness gate start `
  --config configs/forecastin.example.yaml `
  --name backend-pytest `
  --command "bash scripts/ci.sh" `
  --dry-run
```

When the operator runs the planned command in a background terminal, an
external supervisor script (or, in the future, this harness) writes
status updates via the gate state API. Inspect the state with:

```powershell
forecastin-harness gate status --config configs/forecastin.example.yaml --name backend-pytest
forecastin-harness gate tail   --config configs/forecastin.example.yaml --name backend-pytest --lines 100
```

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
# Dry-run first to see exactly what the gate will check:
forecastin-harness pr check `
  --config configs/forecastin.example.yaml `
  --pr 2788 `
  --head-sha 8f91dcf3 `
  --dry-run

# Then run live (requires gh):
forecastin-harness pr check `
  --config configs/forecastin.example.yaml `
  --pr 2788 `
  --head-sha 8f91dcf3
```

If every outcome is `pass`, the harness writes
`<state>/pr/2788.merge_ready.json` containing a *suggested* merge command.
**The harness does not run that command.** Run it yourself when you are
satisfied.

### 6. Retire the lane

After merge:

```powershell
forecastin-harness lanes retire `
  --config configs/forecastin.example.yaml `
  --name for-235-settings `
  --dry-run
# then for real
forecastin-harness lanes retire `
  --config configs/forecastin.example.yaml `
  --name for-235-settings
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

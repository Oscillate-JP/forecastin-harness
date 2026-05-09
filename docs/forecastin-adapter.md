# The Forecastin adapter

The harness is target-repo-agnostic. To point it at
[`Oscillate-JP/Forecastin`](https://github.com/Oscillate-JP/Forecastin), edit
`configs/forecastin.example.yaml` to match the operator's machine and pass
the file to every CLI invocation that takes `--config`.

## Field-by-field

```yaml
target:
  repo:        "<owner>/<name>"     # Required. GitHub-style identifier; used by gh.
  path:        "<absolute-path>"    # Required. Local working copy. Windows-friendly.
  main_branch: "main"               # Required. Default branch on origin.

worktrees:
  root: "<absolute-path>"           # Required. Lane worktrees are created here.
                                    # Recommended: a path *inside* `target.path`,
                                    # e.g. `<target.path>/.harness/worktrees`,
                                    # so a single `.git` directory backs everything.

agents: [claude, codex, gemini]     # Required. At least one. Allowed values are
                                    # the ones in src/forecastin_harness/config.py
                                    # (claude, codex, gemini).

commands:                           # Optional but recommended.
  backend_test:  "<command>"        # Canonical local backend test invocation.
  frontend_test: "<command>"        # Canonical local frontend test invocation.
  backend_lint:  "<command>"        # Editable: marked editable in README.
  frontend_lint: "<command>"        # Editable.
  full_ci:       "<command>"        # The script the gate runner will use.

health:                             # Optional.
  backend_url:  "http://localhost:9000/health"
  frontend_url: "http://localhost:3002"

merge:
  policy: operator-confirmed        # Allowed: 'operator-confirmed' (default), 'auto'.
                                    # 'auto' is reserved for a future trusted-CI mode
                                    # and is NOT honoured by v0.1 — the harness
                                    # never merges in this slice.

state:
  dir: ".harness/state"             # Default. Resolved against target.path.
```

## Forecastin-specific recommendations

* `target.path` should be the **clean** Forecastin checkout, not a feature
  branch worktree. The harness writes state under `target.path` and reads
  `git config` from `target.path/.git`; pointing it at a worktree means
  state lives inside that worktree.
* `worktrees.root` is best as `<target.path>/.harness/worktrees`. Each lane
  becomes a `git worktree`, sharing the parent's `.git` so blame and history
  navigation work without re-cloning.
* `commands.full_ci` should be `"bash scripts/ci.sh"` for Forecastin. That
  script is the same one the pre-push hook runs; using it via the gate
  keeps local pre-push and gated CI in lock-step. (See README for the v0.1
  caveat that `scripts/ci.sh` is currently failing on a pre-existing test;
  the gate models that as `failed`, which is the correct outcome.)
* `merge.policy: operator-confirmed` is mandatory. Forecastin's branch
  protection rules + CodeRabbit review + pre-push CI must all run before
  any merge; the harness must not bypass any of them.

## What the adapter does NOT cover

The adapter is intentionally coarse. It does **not** model:

* Per-lane environment variables (use the lane's worktree shell for that).
* Test-suite timeouts (the gate state machine handles that — see
  `docs/architecture.md`).
* Authentication. `gh` reads `GITHUB_TOKEN` from the environment when needed;
  the harness never reads or writes credentials.

If a target repo requires more nuance (e.g. multi-repo monorepo lanes), the
right move is to publish a follow-up adapter version, not to bolt fields onto
this one.

## Validating an edited adapter

```powershell
# Quick syntax + schema check without touching any state:
forecastin-harness init --target-repo J:/Forcastin --config <path-to-edited-yaml>
```

`init` validates the config, ensures the target path is a git checkout, and
creates the state directory. If anything fails, it prints the precise field
that broke and exits non-zero.

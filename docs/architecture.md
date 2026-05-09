# Architecture

`forecastin-harness` is a thin orchestration layer that lets multiple AI agents
work safely against the same target repository. It owns four concerns:
**lanes** (isolated worktrees), **task packets** (agent contracts), **gates**
(long-running CI), and the **PR review gate**. Everything else — actual code
edits, actual test execution, actual reviews — is delegated to the target
repository's tooling.

## Layers

```
┌────────────────────────────────────────────────────────────┐
│ CLI (forecastin_harness/cli.py)                            │
│   subcommands: init, lanes, task, gate, pr                 │
├────────────────────────────────────────────────────────────┤
│ Worktrees   │ Prompts      │ Gates       │ PR Gate         │
│ (planner +  │ (template    │ (state      │ (gh-driven      │
│  registry)  │  rendering)  │  machine)   │  checks)        │
├────────────────────────────────────────────────────────────┤
│ Config              State store (lanes.json, events.jsonl) │
│ (adapter YAML)      Filesystem layout                      │
└────────────────────────────────────────────────────────────┘
                             ↓ reads/writes
              <target_repo>/.harness/state/...
              <target_repo>/.harness/worktrees/...
                             ↑ never mutates without operator
                             │
                       Target repo (Forecastin)
```

The CLI is the only public surface. The library modules can be reused, but
no public commitment is made about their stability across versions until 1.0.

## Key invariants

1. **Adapter-driven.** The harness has zero hard-coded knowledge of any
   particular target repo. Forecastin-specific paths and commands live in
   `configs/forecastin.example.yaml`; replacing the YAML retargets the harness
   at any other repo.
2. **State directory is canonical.** Every lane, gate, and PR record is on
   disk under `<target_repo>/<state.dir>` (default `.harness/state`).
   A crashed agent shell can be recovered by reading state alone.
3. **Audit log is append-only.** `events.jsonl` is JSON-Lines: one event per
   line, sorted keys, UTC timestamps. We deliberately accept the duplication
   between lane records and the audit log — the registry holds the *current*
   view, the log holds *how we got there*.
4. **Dry-run is default for state changes.** Every mutating command surfaces
   the exact commands or files it would touch before doing so. The operator
   has to opt out of dry-run.
5. **Subprocess is shell-isolated.** Internal commands (`git`, `gh`,
   `rev-parse`) run with `shell=False` and explicit argv lists. The only
   `shell=True` path is the *user-supplied gate command*, which is documented
   as the trust boundary. Operators must vet the command they pass.
6. **No secrets.** Local validation (config parsing, lane planning, prompt
   rendering, dry-run gate, dry-run PR check) requires no token. The only
   network-touching path is the live PR check, and that path is opt-in.

## Lane lifecycle

```
plan_lane (pure)
   │
   │ --dry-run prints commands; nothing happens
   │
   ▼
apply_plan
   ├── git fetch origin --prune
   ├── git worktree add -b lane/<task>-<name> <root/name> origin/<main>
   ├── git rev-parse base + HEAD (recorded into state)
   └── store.add_lane(...) + audit event "lane.created"

   ↓ agent works in worktree, runs tests, commits

retire_lane
   ├── status -> "retired" in state
   └── audit event "lane.retired"

   ↓ operator removes the worktree from disk manually
   (the harness does not run `git worktree remove`)
```

Removal is left to the operator because `git worktree remove` of a dirty
worktree requires `--force`, and forced removal is exactly the kind of action
the harness must never take silently.

## Gate state machine

```
planned ──► running ──► passed   (exit 0)
                  ├──► failed    (non-zero exit)
                  ├──► killed    (operator stopped)
                  └──► timeout   (wrapper deadline exceeded)

any inconsistent observation ──► unknown
```

`unknown` is deliberate: a future spawner / supervisor must distinguish
"process gone, no exit-code file" from a clean terminal state. Coding
"unknown" as a real status keeps the bug visible.

## PR gate

The PR gate is a sequence of independent checks, each producing a
`CheckOutcome`:

* `head-sha-match` — the operator-supplied SHA must equal the PR's `headRefOid`.
* `pr-open` — PR must be in state `OPEN`.
* `review-approved` — `reviewDecision` must be `APPROVED`.
* `required-checks` — no required check in `{FAILURE, CANCELLED, TIMED_OUT}`.

If `gh` is missing, the gate produces a `missing-tool` outcome and stops; it
never silently passes. `merge_ready=true` requires every outcome to be
`pass`. Even on `merge_ready=true`, the harness only writes
`<state>/pr/<n>.merge_ready.json` and prints a *suggested* `gh pr merge`
command. Execution is the operator's responsibility.

## Roadmap (deliberately out of scope for v0.1)

* Background gate spawn + supervisor (PID tracking, deadline enforcement,
  state transitions driven by process events). The state model already
  supports this; the spawn logic is a follow-up.
* Lane re-attachment after a worktree is moved on disk.
* `gh`-API rate-limit handling and pagination on noisy PRs.
* Cross-platform support for non-bash gate commands. The current planner
  prints a `bash -c` recipe; the model is generic but the renderer is
  bash-shaped.
* `forecastin-harness pr merge` — only behind an explicit
  `--operator-confirmed` flag; the artefact is already there.

These are listed so a contributor can pick one without re-deriving the
architecture.

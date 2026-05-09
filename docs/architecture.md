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
4. **Plan-only is default for state changes (fail-closed).** Every mutating
   command surfaces the exact commands or files it would touch before doing
   so. The operator has to explicitly pass ``--apply`` to opt in. v0.3
   inverted the v0.2 ``--dry-run`` semantics so a mistyped command without
   ``--apply`` cannot mutate state or contact GitHub.
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
   │ default (no --apply) prints commands; nothing happens
   │
   ▼
apply_plan  (only reached when the operator passes --apply)
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

## v0.1 → v0.2 → v0.3 capability boundary

| capability | v0.1 | v0.2 | v0.3 |
|---|---|---|---|
| Config + state layout | ✅ | ✅ | ✅ |
| Lane planner (plan-only) | ✅ | ✅ | ✅ |
| Prompt rendering | ✅ | ✅ | ✅ |
| Gate **state machine** | ✅ | ✅ | ✅ |
| Gate **supervisor** (Popen, deadline, log streaming) | ❌ | ✅ | ✅ |
| `gate stop` / `gate summarise` | ❌ | ✅ | ✅ |
| Cross-platform rendering (posix/powershell/argv) | ❌ | ✅ | ✅ |
| PR check skeleton | ✅ | ✅ | ✅ |
| `pr merge` planner with refusal paths | ❌ | ✅ | ✅ |
| `pr merge --execute` (guarded) | ❌ | ✅ | ✅ |
| `--repo` pin in rendered `gh` argv | ❌ | ✅ | ✅ |
| CodeRabbit `coderabbit-clean` outcome | ❌ | ✅ | ✅ |
| **Fail-closed default** (`--apply` replaces `--dry-run`) | ❌ | ❌ | ✅ |
| **`pr merge` requires `--apply --execute --operator-confirmed`** | ❌ | ❌ | ✅ |
| **`init` rejects `--target-repo` / `config.target.path` mismatch** | ❌ | ❌ | ✅ |

## Gate supervisor

```
plan_gate (pure)
    │
    ▼
supervise()
    ├── ensure state file exists, mark "running"
    ├── Popen(command.to_subprocess_argv(), shell=False, stdout=log, stderr=STDOUT)
    ├── poll until terminal OR (now − started) ≥ deadline
    ├── on deadline: terminate (SIGTERM grace) → kill (SIGKILL)
    ├── write exit_path with the captured returncode
    └── transition state to passed | failed | timeout
```

The supervisor never uses `shell=True`. POSIX and PowerShell snippets are
wrapped explicitly: `["bash", "-lc", snippet]` / `["powershell",
"-NoProfile", "-NonInteractive", "-Command", snippet]`. Argv mode passes
the operator's argv straight to Popen with no shell at all.

The trust boundary is the operator-supplied snippet itself. Treat it the
same way you would treat a script the operator wrote and saved to disk;
the harness does not sanitise it.

## Cross-platform command rendering

`src/forecastin_harness/rendering.py` exposes a tiny
:class:`CommandSpec` that knows how to:

* render itself for operator output (`display()` returns a single line),
* produce the argv passed to `subprocess.Popen` (`to_subprocess_argv()`),
* enforce that exactly one of (snippet, argv) is set per mode.

`from_string` and `from_argv` are the only public factories. `from_string`
defaults to `posix` on POSIX hosts and `powershell` on Windows; the CLI
`--mode` flag overrides the default.

## PR-merge guard

`pr_gate.plan_pr_merge` is a pure function that reads
`<state>/pr/<n>.merge_ready.json` and refuses to produce a plan when:

* the file is missing,
* the recorded `head_sha` does not equal the operator-supplied SHA,
* the recorded `verdict` is not `merge_ready` or `approved`.

Successful plans contain an explicit ``--repo <owner>/<name>`` flag bound
to the adapter config's `target.repo`. This protects against the
classic mistake of running the rendered command from a different
checkout: without `--repo`, `gh pr merge` falls back to whatever git
origin the cwd points at. The harness never relies on cwd inference.

## Roadmap (out of scope for v0.2)

* Streaming `gate tail --follow` mode for live log watching.
* Lane re-attachment after a worktree is moved on disk.
* `gh`-API rate-limit handling and pagination on noisy PRs.
* `pr merge` recovery if `gh` returns a non-zero exit (currently the audit
  log records the attempt; recovery is operator-driven).
* Adapter test that drives the harness against a real Forecastin checkout
  in dry-run mode.

# forecastin-harness

A standalone, reusable AI-agent harness for orchestrating Claude, Codex, and Gemini
against a target repository — primarily [Oscillate-JP/Forecastin](https://github.com/Oscillate-JP/Forecastin),
but designed to point at any well-defined target repo.

> **This repository is infrastructure**, not product code. It does not contain
> Forecastin application code, and it must never mutate a target repo without
> explicit operator action.

---

## What this harness does

* Manages **isolated worktrees** ("lanes") so multiple agents can work in parallel
  without colliding on the same files.
* Holds a **machine-readable lane registry** so a session crash does not lose
  bookkeeping.
* Renders **agent-specific prompts** (Claude, Codex, Gemini) from a single canonical
  task packet, so the same scope produces equivalent prompts across providers.
* Runs **long CI gates** (e.g. an hour-long backend pytest) outside the agent's
  fragile shell timeout, in the background, with deterministic log paths.
* Provides a **PR review gate** that validates head SHA, review state, and required
  checks — and emits a `merge_ready.json` artefact only when conditions are met.
  It never merges by default.
* Logs every important transition to a **JSONL audit stream** so the operator can
  reconstruct what happened across many parallel agents.

## What this harness does NOT do

* It does not vendor or import Forecastin product code.
* It does not push to a target repository's branches.
* It does not bypass git hooks, CodeRabbit, branch protection, or required CI.
* It does not run `git worktree remove --force` or any destructive recovery action.
* It does not require any secrets to run local validation. (Secrets — for example a
  GitHub token — are only needed when the operator explicitly invokes commands that
  contact GitHub through `gh`.)
* It does not merge PRs. It can *prepare* the merge state and *generate* a merge
  command, but execution is always operator-driven.

---

## Install (local development)

```powershell
# from this repo's root, on Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
forecastin-harness --help
```

```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
forecastin-harness --help
```

You can also invoke the CLI without installing the script:

```bash
python -m forecastin_harness.cli --help
```

## Operator workflow against Forecastin

This is the concrete day-1 workflow against `Oscillate-JP/Forecastin`. It assumes
the operator has the Forecastin clone at `J:/Forcastin` and the harness clone
at `J:/forecastin-harness`.

```powershell
# 1. Initialise the harness state directory inside the target repo.
forecastin-harness init --target-repo J:/Forcastin --config configs/forecastin.example.yaml

# 2. Inspect existing lanes.
forecastin-harness lanes list

# 3. Plan a new lane (dry-run prints exact git commands; nothing is created).
forecastin-harness lanes create --name for-235-settings --task FOR-235 --scope "fix settings/groups 500" --dry-run

# 4. Render an agent prompt for that lane's task packet.
forecastin-harness task render --task-file templates/task_packet.yaml --agent claude

# 5. Plan a long gate run (dry-run prints the planned command; no process starts).
forecastin-harness gate start --name backend-pytest --command "bash scripts/ci.sh" --dry-run

# 6. Once a PR exists, dry-run the PR gate so the operator sees what it will check.
forecastin-harness pr check --pr 2788 --head-sha 8f91dcf3 --dry-run
```

Drop `--dry-run` only after the planned commands have been reviewed.

For the deeper rationale, see [`docs/architecture.md`](docs/architecture.md) and
[`docs/operator-workflow.md`](docs/operator-workflow.md).

## Agent contract

Every task an agent works on is described by a **task packet** — a YAML file with
mission, scope, allowed/forbidden files, acceptance criteria, test commands,
stop conditions, and evidence requirements. The harness generates the agent's
prompt from this packet and ingests the agent's structured result back into
state. See [`docs/agent-contract.md`](docs/agent-contract.md).

## Repository layout

```
configs/                      Example target-repo adapter configs
docs/                         Architecture, agent contract, operator workflow
src/forecastin_harness/       Python package
templates/                    Task packet schema + per-agent prompt templates
tests/                        pytest suite
```

## Status

### v0.2 (current)

Adds:
* **Gate supervisor** that actually runs a configured command via
  `subprocess.Popen`, streams stdout/stderr to a deterministic log, enforces
  a wall-clock deadline, and writes terminal status (`passed` / `failed` /
  `timeout` / `killed`) into the state file.
* **`gate stop` and `gate summarise`** subcommands.
* **`pr merge`** — guarded merge-command rendering. Refuses without a
  matching `merge_ready.json`, refuses on SHA mismatch, refuses to execute
  without both `--operator-confirmed` and `--execute`, and pins
  `--repo <owner>/<name>` so the rendered `gh` call is bound to the
  configured target repo (it cannot accidentally target the cwd's origin).
* **CodeRabbit detection** — a `coderabbit-clean` PR-check outcome that
  parses comments/reviews and refuses merge-ready when an unresolved
  critical / security / correctness comment from `coderabbitai[bot]`
  exists.
* **Cross-platform command rendering** — `posix`, `powershell`, and
  `argv` modes; `--mode` flag on `gate start`.

Every previously-shipping behaviour from v0.1 is preserved; dry-run is
still the default for state-changing commands.

### v0.1

Implemented config loading, state files, dry-run lane planning, prompt
rendering, gate state, and a PR-gate skeleton, with tests for each.

# Controller workflow

The controller commands (`forecastin-harness controller validate` and
`forecastin-harness controller doctor`) exist because the live FOR-220
run exposed a class of failures the harness had no defence against:

* an agent drifted from FOR-220 into gate repair without a new packet
  or a new branch;
* long jobs became unmanaged — orphan pytest processes accumulated and
  shells hung;
* a `bypass-permissions` mode appeared in the loop;
* diagnostic `pytest --collect-only` enumeration was confused with the
  official CI gate;
* feature work and gate repair were mixed into a single PR.

The harness's job is **not** to be an autonomous control plane that
solves these for the operator. The harness is an **assisted guardrail**:
it refuses to let a lane proceed when its packet would re-create those
failure modes, and it surfaces live process / cwd state so the operator
can spot a stranded loop before launching another one.

## Mental model

```
[ operator ]                                            [ human reviewer ]
     |                                                          ^
     |  edits packet.yaml                                       |
     v                                                          |
+-----------------------+                                       |
| controller validate   |  fail-closed; refuses unsafe packets  |
+-----------------------+                                       |
     | packet ok                                                |
     v                                                          |
+-----------------------+                                       |
| controller doctor     |  read-only; flags stale processes,    |
+-----------------------+  wrong cwd, dirty trees               |
     | environment ok                                           |
     v                                                          |
+-----------------------+                                       |
| lanes / gate / pr     |  agent + harness do the work          |
+-----------------------+                                       |
                                                                |
   merge_authority: "human-only" --------------------------------+
```

Everything below the validator is unchanged from
[operator-workflow.md](./operator-workflow.md). The controller commands
are a pre-flight, not a substitute for the existing day-to-day flow.

## `forecastin-harness controller validate --task-file <path>`

Reads a YAML task packet, applies every controller safety check, and
prints a `PASS` / `FAIL` verdict.

Exit codes:

| code | meaning                                            |
|------|----------------------------------------------------|
| 0    | packet PASSes — safe to launch the lane            |
| 1    | packet FAILed at least one safety check             |
| 2    | packet could not be loaded (missing / not YAML / not a mapping) |

The check codes you may see in a `FAIL` report:

| code | what it means |
|------|---------------|
| `missing_objective`                  | `mission` is empty or missing |
| `missing_target_repo`                | `target_repo` is empty or missing |
| `missing_worktree`                   | `worktree` is empty or missing |
| `missing_official_gate`              | `official_gate_command` is empty or missing |
| `missing_max_runtime`                | `max_runtime_minutes` is missing or non-positive |
| `missing_forbidden_files`            | `files.forbidden` is missing or empty |
| `missing_final_report_contract`      | `final_report` is missing or empty |
| `final_report_missing_changed_files` | `final_report` does not promise *changed files* |
| `final_report_missing_tests`         | `final_report` does not promise *tests* |
| `final_report_missing_remaining_risks` | `final_report` does not promise *remaining risks* |
| `invalid_lane_type`                  | `lane_type` is not one of `feature`, `gate-fix`, `review`, `controller` |
| `mixed_feature_and_gate_fix`         | `files.allowed` lists both feature paths and gate-repair paths |
| `feature_lane_only_touches_gate_paths` | `lane_type=feature` but every allowed file is a gate-repair path |
| `gate_fix_lane_only_touches_feature_paths` | `lane_type=gate-fix` but every allowed file is a feature path |
| `autonomous_merge_not_allowed`       | `merge_authority` is anything other than `"human-only"` |
| `bypass_permissions_not_disallowed`  | `no_bypass_permissions` is missing or not `true` |
| `bypass_token_detected`              | a bypass token (`--no-verify`, `bypass-permissions`, `ignore-hooks`, `skip-hooks`, `--no-gpg-sign`) appears anywhere in the packet's strings |

The validator is intentionally pure: it does not touch the filesystem
beyond reading the packet, makes no network calls, and never writes
state.

### Example: packet PASSes

```text
$ forecastin-harness controller validate \
    --task-file .harness/state/tasks/for-235.yaml
task packet: .harness/state/tasks/for-235.yaml
verdict: PASS - no findings
$ echo $LASTEXITCODE
0
```

### Example: packet FAILs

```text
$ forecastin-harness controller validate \
    --task-file .harness/state/tasks/bad.yaml
task packet: .harness/state/tasks/bad.yaml
verdict: FAIL

errors (2):
  [autonomous_merge_not_allowed] merge_authority must equal 'human-only', got 'auto'
  [mixed_feature_and_gate_fix] task mixes feature paths and gate-fix paths in files.allowed; split into two PRs. feature: ['backend/app/api/v1/endpoints/settings.py']; gate-fix: ['scripts/ci.sh']
$ echo $LASTEXITCODE
1
```

## `forecastin-harness controller doctor [--config <path>]`

Reports the live environment so the operator can spot stranded
processes and cwd / repo confusion before launching another lane.

The output sections:

| section                | meaning |
|------------------------|---------|
| `cwd`                  | current working directory of the harness invocation |
| `target_path`          | `target.path` from the supplied config, or `(no config)` if `--config` was omitted |
| `cwd_matches_target`   | `True` only when the resolved `cwd` equals the resolved `target_path` |
| `current_branch`       | output of `git -C <cwd> rev-parse --abbrev-ref HEAD`, or `(unknown)` |
| `dirty`                | `True` if `git -C <cwd> status --porcelain` is non-empty |
| `pytest processes`     | rows whose name or cmdline contains `pytest` / `py.test` |
| `python processes`     | non-pytest python interpreters |
| `shell processes`      | bash, sh, zsh, powershell, pwsh, cmd |
| `warnings`             | safety annotations (see below) |

Warnings the doctor emits:

* `N pytest processes have run for >= 300s — possible orphans (PIDs: ...)`
  fires when **two or more** pytest processes have a wall-clock runtime
  >= 5 minutes. Concurrent unmanaged pytest runs were the primary
  FOR-220 failure mode that produced unreliable results.
* `cwd (X) does not match configured target (Y)` fires when `--config`
  is supplied and the harness was invoked from a directory other than
  the target repo. The agent should `cd` to the target before running
  any lane commands.

The doctor never mutates state, never opens a network connection, and
never reads secrets. If `tasklist` (Windows) or `ps` (POSIX) is missing
or denied, the process buckets simply come back empty — no exception is
raised.

Exit codes:

| code | meaning                                            |
|------|----------------------------------------------------|
| 0    | report rendered                                    |
| 2    | `--config` was supplied but failed to load (`config error: ...`) |

## How the validator interacts with `lanes create` / `gate start`

The validator is **not** wired into `lanes create` or `gate start`
automatically — the operator is the loop. The contract is:

1. Edit the task packet under `<target>/.harness/state/tasks/<id>.yaml`.
2. `forecastin-harness controller validate --task-file <packet>`.
3. If the verdict is `PASS`, proceed to the day-to-day workflow:
   `lanes create`, `task render`, `gate start`, `pr check`,
   `pr merge` — all as documented in
   [operator-workflow.md](./operator-workflow.md).
4. If the verdict is `FAIL`, fix the packet. Do not paper over the
   finding by deleting the offending field; fix the underlying intent
   the validator surfaced.

A `controller validate` pass is a necessary precondition, not a
sufficient guarantee of safety. Real-world judgement still belongs to
the human operator, who owns the merge.

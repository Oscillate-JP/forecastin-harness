# Preflight Runbook (post 2026-05-09 hardening)

This runbook documents the harness preflight checks added by the
``hardening/root-runtime-lane-gates`` tranche. Every check is
read-only and refuses to mutate state. Each one closes a specific
class of failure that bit the 2026-05-09 drain.

## When to run

Run all five checks **at the top of every drain session**, before
opening any worktree, before any `git push`, before any `gh pr
merge`. They're cheap, they don't touch the network beyond `gh pr
view`, and they catch the wrong-root / stale-runtime / docs-only-as-
backend / hung-pytest / banner-as-merge-evidence traps that produced
hours of churn last time.

## A. Root guard

Symptom this prevents: Claude relaunched in `J:/Forcastin` and every
hook, mount, and merge_guard ran against the wrong checkout.

```bash
forecastin-harness preflight root \
  --expected J:/Forecastin-worktrees/_coordinator-main
```

* Exit 0 → cwd is the expected coordinator root.
* Exit 2 → message: `WRONG_ROOT: relaunch Claude from <expected_root>`.

The default forbidden list always includes `J:/Forcastin`. Add more
forbidden roots with `--forbidden`.

## B. Runtime provenance guard

Symptom this prevents: backend/frontend containers bind-mounted from
`J:/Forcastin`, so curl evidence reflected stale code rather than
the worktree under edit.

```bash
docker inspect $(docker compose ps -q) > /tmp/inspect.json
forecastin-harness preflight runtime --inspect-json /tmp/inspect.json
```

Expected output:

```
backend_mount_ok = true   (ok)
  all 1 bind mount(s) for 'backend' resolve under allowed roots
frontend_mount_ok = true  (ok)
  all 1 bind mount(s) for 'frontend' resolve under allowed roots
runtime_evidence_valid = true
```

If `runtime_evidence_valid = false`, the curl proof from this stack
**is not admissible**. Restart Docker Desktop, recreate the compose
project against the coordinator worktree, re-inspect, re-check.

## C. Lane-aware gate policy

Symptom this prevents: docs-only PRs were blocked by full backend
pytest for 30+ min per attempt.

```bash
git diff --name-only origin/main | forecastin-harness preflight lane-policy
```

Expected output for a docs-only branch:

```
lane_kind = docs-only
requires_full_backend_pytest = false
rationale: selected most-restrictive kind 'docs-only' from kinds seen: ['docs-only']
gate recipe:
  $ python scripts/docs/build_inventory.py --check
  $ markdownlint docs/ '*.md'
```

The classifier does not skip a gate; it tells the operator (and the
human or agent merging the PR) which gate is appropriate.

## D. Push watchdog

Symptom this prevents: three pytest processes competing for CPU and
~3 GB of RAM, none of which produced any output, all blocking `git
push` for 30+ min.

```bash
forecastin-harness preflight watchdog
```

Expected output when clean:

```
watchdog ok: no hung pytest / no-output processes (age_threshold=300s, silence_threshold=90s)
```

When flagged:

```
watchdog FAIL: 3 flagged process(es)
  pid=  41056 reason=orphan_pytest runtime=1800s name=python.exe cmd=python -m pytest tests/...
    suggested: taskkill /F /PID 41056    # POSIX: kill -TERM 41056 || kill -KILL 41056
  Operator action only — the harness never auto-kills without explicit authorisation.
```

The watchdog never kills. The operator decides.

## E. Merge evidence verifier

Symptom this prevents: GitHub UI banner ("Merged — proceed") was
treated as evidence; `gh` later returned `state=OPEN`,
`mergedAt=null`. Linear was prematurely closed.

```bash
forecastin-harness preflight merge-evidence \
  --pr 2796 --repo Oscillate-JP/Forecastin \
  --check-reachability J:/Forecastin-worktrees/_coordinator-main
```

Expected output for a real merge:

```
pr               = #2796
state            = MERGED
mergedAt         = '2026-05-10T12:00:00Z'
mergeCommit.oid  = 'abc123def456...'
url              = 'https://github.com/...'
reason           = state=MERGED with mergedAt and mergeCommit.oid present
reachable_origin_main = true
verdict          = ok
```

Free-text input is rejected — the parser only consumes JSON. If the
operator (or another agent) sees a banner, the only valid follow-up
is to run this command and read the JSON.

## F. Drain loop semantics

Stop conditions every drain inherits (now baked into the generated
task packets):

1. credentials missing
2. destructive operation
3. merge conflict
4. product or architecture decision required
5. no safe actionable issues remain
6. wrong root detected
7. invalid runtime provenance detected

Lane reports are checkpoints. After a report, pick the next
actionable lane unless one of the above stop conditions is met.

## G. Generated task packets

Hand-crafting task packets for every drain shape was the friction
that caused the previous run to skip ``controller validate``
entirely. Use the generator instead:

```bash
forecastin-harness task generate \
  --kind open-pr-drain \
  --target-repo J:/Forecastin-worktrees/_coordinator-main \
  --worktree   J:/Forecastin-worktrees/_coordinator-main \
  --option pr_owner_repo=Oscillate-JP/Forecastin \
  --out /tmp/drain-packet.yaml
forecastin-harness controller validate --task-file /tmp/drain-packet.yaml
```

Available kinds:

* ``open-pr-drain``
* ``docs-only-spec-batch``
* ``runtime-bugfix``
* ``linear-closeout``

## Order of operations on a fresh drain

```bash
# 1. Confirm root.
forecastin-harness preflight root

# 2. (If runtime evidence will be collected) confirm provenance.
docker inspect $(docker compose ps -q) > /tmp/inspect.json
forecastin-harness preflight runtime --inspect-json /tmp/inspect.json

# 3. Generate + validate the packet for this drain shape.
forecastin-harness task generate --kind open-pr-drain \
  --target-repo J:/Forecastin-worktrees/_coordinator-main \
  --worktree   J:/Forecastin-worktrees/_coordinator-main \
  --option pr_owner_repo=Oscillate-JP/Forecastin \
  --out /tmp/packet.yaml
forecastin-harness controller validate --task-file /tmp/packet.yaml

# 4. Watchdog before any push.
forecastin-harness preflight watchdog

# 5. After every claimed merge, verify with structured evidence.
forecastin-harness preflight merge-evidence --pr <N> --repo Oscillate-JP/Forecastin

# 6. Only then update Linear.
```

Skip a step → the harness can no longer help.

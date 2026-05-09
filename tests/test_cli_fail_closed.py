"""Fail-closed CLI tests for v0.3.

These tests pin the v0.3 inversion of ``--dry-run`` to ``--apply``: every
state-changing subcommand defaults to plan-only, and the operator must
explicitly pass ``--apply`` for any mutation or external call to occur.

The contract is enforced at the CLI dispatch layer, so each test:

* runs ``cli.main([...])`` against a fixture config + fake target repo,
* monkeypatches the *dispatch-side* symbols (``apply_plan``, ``Popen``,
  ``run_pr_check``, ``subprocess.run``) so a leak through to the apply path
  is detectable as a function-call observation, and
* asserts both ``rc == 0`` (plan-only is always success) and that no
  state-mutating sentinel fired.

Where a test asserts the apply path *did* fire, we still stub the underlying
side effect so the test does not actually invoke git, gh, or Popen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forecastin_harness import cli


# ---------------- helpers ----------------


def _state_dir(target_repo: Path) -> Path:
    return target_repo / ".harness" / "state"


# ---------------- lanes create ----------------


def test_lanes_create_default_is_plan_only(
    fake_target_repo: Path,
    example_config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --apply, lanes create must NOT call apply_plan or any subprocess."""
    apply_called: list[bool] = []

    def fake_apply_plan(*_a: Any, **_kw: Any):
        apply_called.append(True)
        raise AssertionError("apply_plan must not run in plan-only mode")

    monkeypatch.setattr(cli, "apply_plan", fake_apply_plan)

    rc = cli.main(
        [
            "lanes",
            "create",
            "--config",
            str(example_config_file),
            "--name",
            "for-1-x",
            "--task",
            "FOR-1",
            "--scope",
            "y",
        ]
    )
    assert rc == 0
    assert apply_called == []
    # No lane should have been registered on disk. The registry file may exist
    # because _config_and_store() ensures the layout, but it must list zero
    # lanes regardless of the on-disk shape (current schema: {"lanes": [...]}).
    lanes_file = _state_dir(fake_target_repo) / "lanes.json"
    if lanes_file.exists():
        import json as _json

        payload = _json.loads(lanes_file.read_text(encoding="utf-8") or "[]")
        if isinstance(payload, list):
            assert payload == []
        else:
            assert payload.get("lanes", []) == []


def test_lanes_create_with_apply_invokes_apply_plan(
    fake_target_repo: Path,
    example_config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --apply, the dispatcher must call apply_plan exactly once."""
    seen: list[Any] = []

    def fake_apply_plan(plan: Any, store: Any, *, owner: Any = None, **_kw: Any):
        seen.append((plan, owner))

        class _Lane:
            name = plan.name
            head_sha = "h" * 40

        return _Lane

    monkeypatch.setattr(cli, "apply_plan", fake_apply_plan)

    rc = cli.main(
        [
            "lanes",
            "create",
            "--config",
            str(example_config_file),
            "--name",
            "for-1-x",
            "--task",
            "FOR-1",
            "--scope",
            "y",
            "--apply",
        ]
    )
    assert rc == 0
    assert len(seen) == 1, f"apply_plan should fire exactly once, got {len(seen)}"


# ---------------- gate start ----------------


def test_gate_start_default_is_plan_only(
    fake_target_repo: Path,
    example_config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --apply, gate start must NOT spawn a process or write a state file."""
    spawned: list[Any] = []

    def fake_supervise(*_a: Any, **_kw: Any):
        spawned.append(True)
        raise AssertionError("supervise must not run in plan-only mode")

    def fake_initialise(*_a: Any, **_kw: Any):
        spawned.append("init")
        raise AssertionError("gate_initialise must not run in plan-only mode")

    monkeypatch.setattr(cli, "supervise", fake_supervise)
    monkeypatch.setattr(cli, "gate_initialise", fake_initialise)

    rc = cli.main(
        [
            "gate",
            "start",
            "--config",
            str(example_config_file),
            "--name",
            "demo",
            "--command",
            "echo hi",
        ]
    )
    assert rc == 0
    assert spawned == []
    # No gate state file should exist on disk.
    state_file = _state_dir(fake_target_repo) / "gates" / "demo.state.json"
    assert not state_file.exists()


# ---------------- pr check ----------------


def test_pr_check_default_is_plan_only(
    fake_target_repo: Path,
    example_config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --apply, pr check must NOT call gh or run_pr_check."""
    ran: list[Any] = []

    def fake_run_pr_check(*_a: Any, **_kw: Any):
        ran.append(True)
        raise AssertionError("run_pr_check must not run in plan-only mode")

    def fake_gh_available() -> bool:
        ran.append("gh-probe")
        raise AssertionError("gh_available must not be probed in plan-only mode")

    monkeypatch.setattr(cli, "run_pr_check", fake_run_pr_check)
    monkeypatch.setattr(cli, "gh_available", fake_gh_available)

    rc = cli.main(
        [
            "pr",
            "check",
            "--config",
            str(example_config_file),
            "--pr",
            "2788",
            "--head-sha",
            "8f91dcf3",
        ]
    )
    assert rc == 0
    assert ran == []
    # No PR result should have been persisted.
    pr_file = _state_dir(fake_target_repo) / "pr" / "2788.json"
    assert not pr_file.exists()


# ---------------- pr merge ----------------


def _seed_merge_ready(target_repo: Path, *, pr: int, sha: str) -> None:
    """Place a merge_ready.json under the harness state dir for the fixture target."""
    import json

    pr_dir = _state_dir(target_repo) / "pr"
    pr_dir.mkdir(parents=True, exist_ok=True)
    (pr_dir / f"{pr}.merge_ready.json").write_text(
        json.dumps(
            {
                "pr_number": pr,
                "head_sha": sha,
                "verdict": "merge_ready",
                "ts": "2026-05-09T00:00:00Z",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_pr_merge_default_is_plan_only(
    fake_target_repo: Path,
    example_config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --apply, pr merge must NOT call gh; it just prints the planned command."""
    _seed_merge_ready(fake_target_repo, pr=2788, sha="8f91dcf3")

    # Hard-fail if anything attempts a subprocess.run-shaped invocation. We
    # patch at the subprocess module so the in-cli ``import subprocess`` (used
    # only on the execute path) would still resolve to the patched callable.
    import subprocess as _sp

    def fake_run(*_a: Any, **_kw: Any):
        raise AssertionError("subprocess.run must not run in plan-only mode")

    monkeypatch.setattr(_sp, "run", fake_run)

    def fake_gh_available() -> bool:
        raise AssertionError("gh_available must not be probed in plan-only mode")

    monkeypatch.setattr(cli, "gh_available", fake_gh_available)

    rc = cli.main(
        [
            "pr",
            "merge",
            "--config",
            str(example_config_file),
            "--pr",
            "2788",
            "--head-sha",
            "8f91dcf3",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "merge plan:" in out
    assert "command:" in out
    assert "plan-only" in out


def test_pr_merge_execute_alone_refuses(
    fake_target_repo: Path,
    example_config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --apply --execute but no --operator-confirmed, exit 2 with a clear refusal."""
    _seed_merge_ready(fake_target_repo, pr=2788, sha="8f91dcf3")

    import subprocess as _sp

    def fake_run(*_a: Any, **_kw: Any):
        raise AssertionError("subprocess.run must not run when refused")

    monkeypatch.setattr(_sp, "run", fake_run)
    # gh availability is irrelevant on this path; stub it to a permissive True
    # so the test pins the missing-confirmation refusal, not gh-not-found.
    monkeypatch.setattr(cli, "gh_available", lambda: True)

    rc = cli.main(
        [
            "pr",
            "merge",
            "--config",
            str(example_config_file),
            "--pr",
            "2788",
            "--head-sha",
            "8f91dcf3",
            "--apply",
            "--execute",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "operator-confirmed" in err



"""Transactional ``apply_plan`` semantics + orphan recovery.

These tests pair with ``test_worktree_planning.py`` (which covers the happy
path and dry-run plan determinism). The focus here is what happens when a
git operation in the middle of ``apply_plan`` blows up: the lane must be
discoverable in state with status ``blocked`` so the operator can see and
clean up the orphan worktree on disk.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from forecastin_harness.config import HarnessConfig
from forecastin_harness.state import Lane, StateStore, utcnow_iso
from forecastin_harness.worktrees import (
    apply_plan,
    plan_lane,
    recover_orphans,
)


class _RecordingRunner:
    """Records each subprocess call and lets the test inject failures."""

    def __init__(
        self,
        *,
        fail_on_substring: str | None = None,
        fail_exc: BaseException | None = None,
        on_call=None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._fail_on_substring = fail_on_substring
        self._fail_exc = fail_exc
        self._on_call = on_call

    def __call__(self, cmd: list[str], **_kwargs: Any):
        self.calls.append(cmd)
        if self._on_call is not None:
            self._on_call(cmd)
        if self._fail_on_substring is not None and any(
            self._fail_on_substring in part for part in cmd
        ):
            assert self._fail_exc is not None
            raise self._fail_exc

        class _Result:
            stdout = ""
            stderr = ""
            returncode = 0

        if "rev-parse" in cmd:
            if cmd[-1] == "HEAD":
                _Result.stdout = "h" * 40 + "\n"
            else:  # base branch
                _Result.stdout = "b" * 40 + "\n"
        return _Result


def _make_plan(example_config: HarnessConfig, name: str = "for-235"):
    return plan_lane(
        example_config,
        name=name,
        task_id="FOR-235",
        scope="settings repair",
    )


def test_apply_plan_records_planned_lane_before_git(
    example_config: HarnessConfig, tmp_path: Path
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = _make_plan(example_config)

    observed_states: list[Lane | None] = []

    def _peek(_cmd: list[str]) -> None:
        # On the very first git invocation, the lane MUST already be in state
        # — otherwise a crash before this point would orphan the worktree.
        observed_states.append(store.find_lane(plan.name))

    runner = _RecordingRunner(on_call=_peek)
    apply_plan(plan, store, runner=runner)

    assert observed_states, "runner was never called"
    first_seen = observed_states[0]
    assert first_seen is not None, "lane was not recorded before git ran"
    assert first_seen.status == "planned"
    assert first_seen.worktree == str(plan.worktree_path)


def test_apply_plan_marks_blocked_on_git_worktree_add_failure(
    example_config: HarnessConfig, tmp_path: Path
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = _make_plan(example_config, name="for-blocked-add")

    err = subprocess.CalledProcessError(
        returncode=128,
        cmd=["git", "worktree", "add"],
        stderr="fatal: 'lane/for-235-for-blocked-add' is already checked out",
    )
    runner = _RecordingRunner(fail_on_substring="worktree", fail_exc=err)

    with pytest.raises(subprocess.CalledProcessError):
        apply_plan(plan, store, runner=runner)

    lane = store.find_lane("for-blocked-add")
    assert lane is not None
    assert lane.status == "blocked"
    assert lane.worktree == str(plan.worktree_path)
    assert lane.notes is not None
    assert "already checked out" in lane.notes


def test_apply_plan_marks_blocked_on_rev_parse_failure(
    example_config: HarnessConfig, tmp_path: Path
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = _make_plan(example_config, name="for-blocked-rp")

    err = subprocess.CalledProcessError(
        returncode=128,
        cmd=["git", "rev-parse", "HEAD"],
        stderr="fatal: not a git repository",
    )
    runner = _RecordingRunner(fail_on_substring="rev-parse", fail_exc=err)

    with pytest.raises(subprocess.CalledProcessError):
        apply_plan(plan, store, runner=runner)

    lane = store.find_lane("for-blocked-rp")
    assert lane is not None
    assert lane.status == "blocked"
    assert lane.notes is not None
    assert "not a git repository" in lane.notes
    # fetch + worktree-add should have been attempted before rev-parse blew up
    assert any("fetch" in c for c in runner.calls)
    assert any("worktree" in c for c in runner.calls)


def test_apply_plan_success_path_records_shas(
    example_config: HarnessConfig, tmp_path: Path
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = _make_plan(example_config, name="for-happy")

    runner = _RecordingRunner()
    lane = apply_plan(plan, store, runner=runner)

    assert lane.status == "planned"
    assert lane.base_sha == "b" * 40
    assert lane.head_sha == "h" * 40
    assert lane.notes is None
    persisted = store.find_lane("for-happy")
    assert persisted is not None
    assert persisted.base_sha == "b" * 40
    assert persisted.head_sha == "h" * 40


def test_recover_orphans_lists_blocked_lanes_with_worktree_path(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()

    def _lane(name: str, status: str, worktree: str) -> Lane:
        return Lane(
            name=name,
            task_id=f"FOR-{name}",
            scope="x",
            branch=f"lane/{name}",
            base_sha="",
            head_sha="",
            worktree=worktree,
            status=status,  # type: ignore[arg-type]
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
        )

    store.add_lane(_lane("planned-one", "planned", "/tmp/p1"))
    store.add_lane(_lane("blocked-one", "blocked", "/tmp/b1"))
    store.add_lane(_lane("blocked-two", "blocked", "/tmp/b2"))
    store.add_lane(_lane("retired-one", "retired", "/tmp/r1"))
    # Defensive: a blocked lane with no worktree path is not an orphan
    # candidate (nothing on disk to recover).
    store.add_lane(_lane("blocked-no-wt", "blocked", ""))

    orphans = recover_orphans(store)
    names = sorted(lane.name for lane in orphans)
    assert names == ["blocked-one", "blocked-two"]
    assert all(lane.status == "blocked" for lane in orphans)
    assert all(lane.worktree for lane in orphans)


def test_apply_plan_emits_lane_blocked_audit_event(
    example_config: HarnessConfig, tmp_path: Path
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = _make_plan(example_config, name="for-audit")

    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["git", "fetch"],
        stderr="fatal: unable to access remote",
    )
    runner = _RecordingRunner(fail_on_substring="fetch", fail_exc=err)
    with pytest.raises(subprocess.CalledProcessError):
        apply_plan(plan, store, runner=runner)

    events = list(store.iter_events())
    blocked_events = [e for e in events if e.get("kind") == "lane.blocked"]
    assert len(blocked_events) == 1
    evt = blocked_events[0]
    assert evt["lane"] == "for-audit"
    assert "reason" in evt
    assert "unable to access remote" in evt["reason"]
    # Failure must NOT have emitted lane.created.
    assert not any(e.get("kind") == "lane.created" for e in events)

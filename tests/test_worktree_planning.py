"""Tests for the lane / worktree planner.

These tests deliberately avoid touching git; they verify that the *plan*
emitted in dry-run mode is correct and deterministic. ``apply_plan`` is
exercised with a fake subprocess runner so no real worktrees are created.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forecastin_harness.config import HarnessConfig
from forecastin_harness.state import StateError, StateStore
from forecastin_harness.worktrees import (
    apply_plan,
    plan_lane,
    plan_retire,
    retire_lane,
    slugify,
)


def test_slugify_lowercases_and_dedupes_separators() -> None:
    assert slugify("FOR-235 Settings 500!") == "for-235-settings-500"


def test_slugify_rejects_empty_after_normalisation() -> None:
    with pytest.raises(ValueError):
        slugify("!!!")


def test_plan_lane_is_deterministic(example_config: HarnessConfig) -> None:
    plan_a = plan_lane(example_config, name="for-235", task_id="FOR-235", scope="settings")
    plan_b = plan_lane(example_config, name="for-235", task_id="FOR-235", scope="settings")
    assert plan_a.branch == plan_b.branch
    assert plan_a.commands == plan_b.commands


def test_plan_lane_branch_naming(example_config: HarnessConfig) -> None:
    plan = plan_lane(
        example_config,
        name="settings-500",
        task_id="FOR-235",
        scope="repair groups endpoint",
    )
    assert plan.branch == "lane/for-235-settings-500"
    assert plan.base_branch == f"origin/{example_config.target.main_branch}"
    assert "git" in plan.commands[0]
    assert "worktree" in plan.commands[1]
    assert "add" in plan.commands[1]


def test_plan_lane_rejects_empty_inputs(example_config: HarnessConfig) -> None:
    with pytest.raises(ValueError):
        plan_lane(example_config, name="", task_id="FOR-1", scope="x")
    with pytest.raises(ValueError):
        plan_lane(example_config, name="lane", task_id="", scope="x")
    with pytest.raises(ValueError):
        plan_lane(example_config, name="lane", task_id="FOR-1", scope="")


class _FakeRunner:
    """Subprocess.run stand-in that returns canned rev-parse output."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: Any):
        self.calls.append(cmd)

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


def test_apply_plan_records_lane_and_emits_event(
    example_config: HarnessConfig, tmp_path: Path
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_lane(
        example_config,
        name="for-235",
        task_id="FOR-235",
        scope="settings repair",
    )
    runner = _FakeRunner()
    lane = apply_plan(plan, store, runner=runner)

    assert lane.head_sha == "h" * 40
    assert lane.base_sha == "b" * 40
    assert store.find_lane("for-235") is not None

    # The runner saw fetch + worktree-add + 2 rev-parse calls.
    assert len(runner.calls) == 4
    assert any("fetch" in c for c in runner.calls)
    assert any("worktree" in c and "add" in c for c in runner.calls)

    events = list(store.iter_events())
    assert any(e["kind"] == "lane.created" for e in events)


def test_apply_plan_rejects_duplicate(
    example_config: HarnessConfig, tmp_path: Path
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_lane(example_config, name="for-x", task_id="FOR-X", scope="x")
    runner = _FakeRunner()
    apply_plan(plan, store, runner=runner)
    with pytest.raises(StateError, match="already exists"):
        apply_plan(plan, store, runner=runner)


def test_plan_retire_marks_status_without_writing(
    example_config: HarnessConfig, tmp_path: Path
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_lane(example_config, name="for-y", task_id="FOR-Y", scope="y")
    apply_plan(plan, store, runner=_FakeRunner())

    proposed = plan_retire(store, "for-y")
    assert proposed.status == "retired"
    # Real registry untouched until we explicitly call retire_lane.
    assert store.find_lane("for-y").status == "planned"

    final = retire_lane(store, "for-y")
    assert final.status == "retired"
    assert store.find_lane("for-y").status == "retired"


def test_plan_retire_rejects_unknown(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    with pytest.raises(StateError, match="unknown lane"):
        plan_retire(store, "no-such-lane")

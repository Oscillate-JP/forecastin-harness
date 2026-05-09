"""Tests for the long-running CI gate state model.

Verifies dry-run rendering, the planned state machine, and that every
documented status is accepted by ``transition``. No real processes are
spawned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forecastin_harness.gates import (
    GATE_STATUSES,
    GateState,
    initialise,
    plan_gate,
    read_state,
    transition,
)
from forecastin_harness.state import StateStore


def test_plan_gate_render_mentions_state_paths(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_gate(store, name="backend-pytest", command="bash scripts/ci.sh")
    rendered = plan.render()
    assert "backend-pytest" in rendered
    assert "bash scripts/ci.sh" in rendered
    assert str(plan.state_path) in rendered
    assert str(plan.log_path) in rendered
    # Render documents the planned transition order.
    assert "planned -> running" in rendered


def test_plan_gate_rejects_separators_in_name(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    with pytest.raises(ValueError, match="path separators"):
        plan_gate(store, name="backend/pytest", command="x")


def test_plan_gate_rejects_blank_command(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    with pytest.raises(ValueError, match="non-empty"):
        plan_gate(store, name="g", command="   ")


def test_initialise_is_idempotent(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_gate(store, name="g", command="echo")
    state_a = initialise(plan, store)
    state_b = initialise(plan, store)
    assert state_a.status == state_b.status == "planned"
    # The second initialise must not overwrite or duplicate.
    assert (read_state(plan)).status == "planned"


@pytest.mark.parametrize("target", [s for s in GATE_STATUSES if s != "planned"])
def test_transition_accepts_every_documented_status(target: str, tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_gate(store, name="g", command="echo")
    initialise(plan, store)
    new = transition(plan, store, new_status=target, exit_code=0 if target == "passed" else 1)
    assert new.status == target
    assert new.history[-1]["from"] == "planned"
    assert new.history[-1]["to"] == target


def test_transition_records_started_and_ended(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_gate(store, name="g", command="echo")
    initialise(plan, store)

    running = transition(plan, store, new_status="running", pid=4321)
    assert running.started_at is not None
    assert running.pid == 4321
    assert running.ended_at is None

    passed = transition(plan, store, new_status="passed", exit_code=0)
    assert passed.ended_at is not None
    assert passed.exit_code == 0


def test_transition_rejects_unknown_status(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_gate(store, name="g", command="echo")
    initialise(plan, store)
    with pytest.raises(ValueError):
        transition(plan, store, new_status="exploded")  # type: ignore[arg-type]


def test_transition_emits_audit_event(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_gate(store, name="g", command="echo")
    initialise(plan, store)
    transition(plan, store, new_status="running", pid=1)
    transition(plan, store, new_status="passed", exit_code=0)

    events = list(store.iter_events())
    kinds = [e["kind"] for e in events]
    assert "gate.planned" in kinds
    assert kinds.count("gate.transition") == 2


def test_state_dataclass_round_trip() -> None:
    s = GateState(name="x", command="y", status="failed", exit_code=2)
    assert GateState.from_dict(s.to_dict()).status == "failed"

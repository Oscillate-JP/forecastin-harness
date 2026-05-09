"""Atomicity tests for ``gates.write_state``.

v0.3 routes gate state writes through ``StateStore.atomic_write_json``
so a crash mid-write cannot leave a half-written file. These tests pin
that contract: the call is delegated, and a corrupt pre-existing file
is fully replaced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forecastin_harness import gates
from forecastin_harness.gates import GateState, plan_gate, write_state
from forecastin_harness.state import StateStore


def test_gate_write_state_uses_atomic_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_gate(store, name="g", command="echo")
    state = GateState(name="g", command="echo", status="planned")

    calls: list[tuple[Path, dict]] = []

    def spy(path: Path, payload: dict) -> None:
        calls.append((Path(path), payload))
        # Still write so the rest of the system stays consistent for any
        # follow-up reads in this test.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(StateStore, "atomic_write_json", staticmethod(spy))
    # gates.py imports StateStore at module load — the attribute lookup
    # ``StateStore.atomic_write_json`` happens at call time, so the
    # monkeypatch is honoured.

    write_state(plan, state)

    assert len(calls) == 1
    called_path, called_payload = calls[0]
    assert called_path == plan.state_path
    assert called_payload["name"] == "g"
    assert called_payload["status"] == "planned"


def test_gate_write_state_replaces_corrupt_file(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_gate(store, name="g", command="echo")

    # Pre-write garbage so the path exists but is invalid JSON.
    plan.state_path.parent.mkdir(parents=True, exist_ok=True)
    plan.state_path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        json.loads(plan.state_path.read_text(encoding="utf-8"))

    state = GateState(name="g", command="echo", status="passed", exit_code=0)
    write_state(plan, state)

    # The atomic replace should leave the file with valid JSON contents
    # matching what we just wrote.
    loaded = json.loads(plan.state_path.read_text(encoding="utf-8"))
    assert loaded["status"] == "passed"
    assert loaded["exit_code"] == 0
    assert loaded["name"] == "g"

    # No `.tmp` siblings left behind.
    leftovers = list(plan.state_path.parent.glob("*.tmp"))
    assert leftovers == []


def test_gate_write_state_creates_parent_directories(tmp_path: Path) -> None:
    """``atomic_write_json`` is documented to mkdir parents as needed."""
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    plan = plan_gate(store, name="g", command="echo")

    # Remove gates dir to prove write_state will recreate it.
    import shutil

    shutil.rmtree(store.gates_dir)
    assert not store.gates_dir.exists()

    state = GateState(name="g", command="echo", status="planned")
    write_state(plan, state)

    assert plan.state_path.is_file()
    loaded = json.loads(plan.state_path.read_text(encoding="utf-8"))
    assert loaded["name"] == "g"


def test_gates_module_imports_state_store() -> None:
    """Sanity: ensure gates.py still re-exports the path through StateStore."""
    # Defensive against a future refactor that drops the import.
    assert getattr(gates, "StateStore") is StateStore

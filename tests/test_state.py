"""Tests for the state store, lane registry, and JSONL audit log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forecastin_harness.state import Lane, LANE_STATUSES, StateError, StateStore


def _make_lane(name: str = "for-1") -> Lane:
    return Lane(
        name=name,
        task_id="FOR-1",
        scope="example scope",
        branch=f"lane/for-1-{name}",
        base_sha="b" * 40,
        head_sha="h" * 40,
        worktree=f"/tmp/{name}",
        status="planned",
    )


def test_ensure_layout_creates_files(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    assert store.lanes_path.is_file()
    assert store.events_path.is_file()
    assert store.gates_dir.is_dir()
    assert store.pr_dir.is_dir()
    assert store.tasks_dir.is_dir()
    # Re-running is idempotent.
    store.ensure_layout()


def test_lane_round_trip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    lane = _make_lane()
    store.add_lane(lane)
    loaded = store.find_lane("for-1")
    assert loaded is not None
    assert loaded.task_id == "FOR-1"
    assert loaded.status == "planned"


def test_add_lane_rejects_duplicate(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    store.add_lane(_make_lane())
    with pytest.raises(StateError, match="already exists"):
        store.add_lane(_make_lane())


def test_upsert_lane_replaces_existing(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    store.add_lane(_make_lane())
    updated = _make_lane()
    updated.status = "active"
    store.upsert_lane(updated)
    after = store.find_lane("for-1")
    assert after is not None
    assert after.status == "active"
    # No duplicate created.
    assert len(store.load_lanes()) == 1


def test_lane_statuses_constant_matches_dataclass_default() -> None:
    # Sanity: every documented status round-trips through the dataclass.
    for status in LANE_STATUSES:
        lane = _make_lane()
        lane.status = status  # mypy: assignable
        round_trip = Lane.from_dict(lane.to_dict())
        assert round_trip.status == status


def test_append_event_writes_jsonl(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    store.append_event({"kind": "test", "value": 1})
    store.append_event({"kind": "test", "value": 2})
    events = list(store.iter_events())
    assert [e["value"] for e in events] == [1, 2]
    assert all("ts" in e for e in events)
    # File is JSONL: each line independently parseable.
    raw_lines = [
        line for line in store.events_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(raw_lines) == 2
    for line in raw_lines:
        json.loads(line)


def test_lanes_save_uses_atomic_write(tmp_path: Path) -> None:
    """Ensure save_lanes does not leave a half-written file on success.

    We don't simulate a crash; we just verify the temp file is gone after
    a normal save.
    """
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    store.add_lane(_make_lane())
    tmp = store.lanes_path.with_suffix(store.lanes_path.suffix + ".tmp")
    assert not tmp.exists()


def test_lane_from_dict_drops_unknown_keys() -> None:
    raw = _make_lane().to_dict()
    raw["spurious_future_field"] = 42
    lane = Lane.from_dict(raw)
    assert lane.name == "for-1"

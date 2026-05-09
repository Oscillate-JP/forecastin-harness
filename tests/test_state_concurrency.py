"""Concurrency and atomicity tests for ``StateStore``.

These cover the v0.3 invariants: ``atomic_write_json`` never leaves a
``.tmp`` sibling, and the cross-platform file lock serialises
read-modify-write operations on the lane registry. Threads (not
multiprocessing) are sufficient because the lock is held against a
filesystem sentinel and the OS APIs (``fcntl.flock``,
``msvcrt.locking``) are honoured per-descriptor, which threads obtain
independently inside :meth:`StateStore._lock`.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from forecastin_harness.state import Lane, StateError, StateStore


def _make_lane(name: str) -> Lane:
    return Lane(
        name=name,
        task_id=f"FOR-{name}",
        scope=f"scope for {name}",
        branch=f"lane/{name}",
        base_sha="b" * 40,
        head_sha="h" * 40,
        worktree=f"/tmp/{name}",
        status="planned",
    )


def test_atomic_write_json_replaces_existing(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    StateStore.atomic_write_json(target, {"v": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}

    StateStore.atomic_write_json(target, {"v": 2, "extra": [1, 2, 3]})
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == {"v": 2, "extra": [1, 2, 3]}


def test_atomic_write_json_does_not_leave_tmp_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "data.json"
    StateStore.atomic_write_json(target, {"hello": "world"})
    # No `.tmp` siblings should remain anywhere under tmp_path after a
    # successful write — os.replace removes the source.
    leftovers = list(tmp_path.rglob("*.tmp"))
    assert leftovers == [], f"unexpected tmp files: {leftovers}"


def test_ensure_layout_creates_lock_file(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    assert store.lock_path.is_file()
    # Idempotent: a second ensure_layout must not blow up or recreate it.
    store.ensure_layout()
    assert store.lock_path.is_file()


def test_lock_serialises_concurrent_add_lane(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()

    n = 4
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=5)
            store.add_lane(_make_lane(f"lane-{idx}"))
        except BaseException as exc:  # pragma: no cover - propagated below
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"unexpected errors: {errors}"
    names = sorted(lane.name for lane in store.load_lanes())
    assert names == sorted(f"lane-{i}" for i in range(n))


def test_lock_rejects_duplicate_under_concurrency(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()

    n = 8
    barrier = threading.Barrier(n)
    successes: list[int] = []
    failures: list[BaseException] = []
    bookkeeping = threading.Lock()

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=5)
            store.add_lane(_make_lane("collide"))
            with bookkeeping:
                successes.append(idx)
        except StateError as exc:
            with bookkeeping:
                failures.append(exc)
        except BaseException as exc:  # pragma: no cover
            with bookkeeping:
                failures.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(successes) == 1, f"expected exactly one winner, got {successes}"
    assert len(failures) == n - 1, f"expected {n - 1} losers, got {len(failures)}"
    for exc in failures:
        assert isinstance(exc, StateError)
        assert "already exists" in str(exc)
    assert len(store.load_lanes()) == 1


def test_lock_released_on_exception(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()

    with pytest.raises(RuntimeError, match="boom"):
        with store._lock():
            raise RuntimeError("boom")

    # A subsequent acquisition must succeed quickly. We run it on a worker
    # thread with a short timeout so a leaked lock would manifest as a
    # hung thread rather than a deadlock of the whole test run.
    acquired = threading.Event()

    def acquire() -> None:
        with store._lock():
            acquired.set()

    t = threading.Thread(target=acquire)
    t.start()
    t.join(timeout=5)
    assert acquired.is_set(), "lock was not released after exception"
    assert not t.is_alive()


def test_lock_serialises_append_event(tmp_path: Path) -> None:
    """Appends from multiple threads must not corrupt the JSONL log."""
    store = StateStore(tmp_path / "state")
    store.ensure_layout()

    n = 6
    per_thread = 5
    barrier = threading.Barrier(n)

    def worker(idx: int) -> None:
        barrier.wait(timeout=5)
        for j in range(per_thread):
            store.append_event({"kind": "test", "thread": idx, "seq": j})
            # tiny yield to interleave more aggressively
            time.sleep(0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    events = list(store.iter_events())
    assert len(events) == n * per_thread
    # Every line must be valid JSON (no torn writes).
    raw_lines = [
        line
        for line in store.events_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(raw_lines) == n * per_thread
    for line in raw_lines:
        json.loads(line)

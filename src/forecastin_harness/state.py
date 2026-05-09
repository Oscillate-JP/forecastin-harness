"""Harness state directory layout, lane registry, and JSONL audit log.

State lives at ``<target_repo>/<config.state.dir>`` (default ``.harness/state``).
Layout::

    .harness/state/
        .lock                   sentinel file used for cross-platform exclusion
        lanes.json              registry of lane records
        events.jsonl            append-only audit log of harness events
        gates/                  one subtree per gate run (state.json + log)
        pr/                     PR check + merge-ready records
        tasks/                  task packets and agent results

All state is plain JSON / JSONL so it's auditable, diffable, and recoverable
by hand.

Concurrency: writes that read-modify-write the lane registry, and appends
to the JSONL audit log, are serialised across threads and processes via a
filesystem advisory lock on ``<root>/.lock`` (``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows). The lock is stdlib-only and best-effort —
sufficient to prevent the duplicate-lane race observed in v0.2.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

LaneStatus = Literal["planned", "active", "blocked", "ready", "retired"]
LANE_STATUSES: tuple[LaneStatus, ...] = (
    "planned",
    "active",
    "blocked",
    "ready",
    "retired",
)


def utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with seconds precision and explicit Z suffix."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Lane:
    name: str
    task_id: str
    scope: str
    branch: str
    base_sha: str
    head_sha: str
    worktree: str
    status: LaneStatus
    owner: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Lane":
        # Tolerate forward-compatible extras: drop unknown keys rather than crash.
        known = {f for f in Lane.__dataclass_fields__}
        cleaned = {k: v for k, v in data.items() if k in known}
        return Lane(**cleaned)


class StateError(RuntimeError):
    """Raised when state operations are inconsistent (e.g. duplicate lane)."""


class StateStore:
    """Filesystem-backed registry. Cheap to construct; idempotent layout creation.

    Mutating operations (``add_lane``, ``upsert_lane``, ``save_lanes``,
    ``append_event``) acquire a cross-platform advisory lock on
    ``<root>/.lock`` so concurrent workers cannot lose writes through
    interleaved read-modify-write cycles.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ---------- layout ----------
    @property
    def lanes_path(self) -> Path:
        return self.root / "lanes.json"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def gates_dir(self) -> Path:
        return self.root / "gates"

    @property
    def pr_dir(self) -> Path:
        return self.root / "pr"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def lock_path(self) -> Path:
        return self.root / ".lock"

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in (self.gates_dir, self.pr_dir, self.tasks_dir):
            sub.mkdir(parents=True, exist_ok=True)
        # Sentinel lock file. Touch is enough — content is never read.
        if not self.lock_path.exists():
            self.lock_path.touch()
        if not self.lanes_path.exists():
            StateStore.atomic_write_json(self.lanes_path, {"lanes": []})
        if not self.events_path.exists():
            self.events_path.touch()

    # ---------- locking ----------
    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Acquire an exclusive advisory lock on ``<root>/.lock``.

        Cross-platform via stdlib only. The lock is released even if the
        guarded block raises. Reentrant acquisition from the same thread is
        not supported — callers must not nest ``with self._lock():`` blocks.
        """
        # Lock file must exist before we can open it for locking.
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.lock_path.exists():
            self.lock_path.touch()

        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT)
        try:
            if getattr(os, "name") == "nt":
                import msvcrt  # type: ignore[import-not-found]
                import time as _time

                # ``msvcrt.locking(fd, LK_LOCK, ...)`` only retries ~10 times
                # at one-second intervals before raising. Under heavy contention
                # (many threads/processes), that ceiling is too tight, so we
                # implement a longer non-blocking retry loop ourselves.
                deadline = _time.monotonic() + 60.0
                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if _time.monotonic() >= deadline:
                            raise
                        _time.sleep(0.01)
                try:
                    yield
                finally:
                    # Seek back to 0 before unlocking the same byte range.
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        # Best-effort unlock; descriptor close also releases.
                        pass
            else:
                import fcntl  # type: ignore[import-not-found]

                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
        finally:
            os.close(fd)

    # ---------- lanes ----------
    def load_lanes(self) -> list[Lane]:
        if not self.lanes_path.exists():
            return []
        data = self._read_json(self.lanes_path)
        if not isinstance(data, dict) or "lanes" not in data:
            raise StateError(f"corrupt lanes file: {self.lanes_path}")
        return [Lane.from_dict(item) for item in data["lanes"]]

    def save_lanes(self, lanes: Iterable[Lane]) -> None:
        # Materialise once so we can safely iterate inside the lock.
        materialised = list(lanes)
        with self._lock():
            StateStore.atomic_write_json(
                self.lanes_path,
                {"lanes": [lane.to_dict() for lane in materialised]},
            )

    def find_lane(self, name: str) -> Lane | None:
        for lane in self.load_lanes():
            if lane.name == name:
                return lane
        return None

    def upsert_lane(self, lane: Lane) -> None:
        with self._lock():
            lanes = self.load_lanes()
            replaced = False
            for i, existing in enumerate(lanes):
                if existing.name == lane.name:
                    lanes[i] = lane
                    replaced = True
                    break
            if not replaced:
                lanes.append(lane)
            StateStore.atomic_write_json(
                self.lanes_path,
                {"lanes": [item.to_dict() for item in lanes]},
            )

    def add_lane(self, lane: Lane) -> None:
        with self._lock():
            # Read-modify-write entirely inside the lock so duplicate-name
            # checks cannot race against a concurrent insert.
            lanes = self.load_lanes()
            for existing in lanes:
                if existing.name == lane.name:
                    raise StateError(f"lane already exists: {lane.name}")
            lanes.append(lane)
            StateStore.atomic_write_json(
                self.lanes_path,
                {"lanes": [item.to_dict() for item in lanes]},
            )

    # ---------- events ----------
    def append_event(self, event: dict[str, Any]) -> None:
        """Append a JSON object to the JSONL audit log.

        Always stamps a UTC ``ts`` if the caller did not. Refuses values that
        are not JSON-serialisable instead of writing a corrupt line.
        """
        record = {"ts": utcnow_iso(), **event}
        line = json.dumps(record, sort_keys=True, ensure_ascii=False)
        # ensure_layout is cheap if directories already exist
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def iter_events(self) -> Iterator[dict[str, Any]]:
        if not self.events_path.exists():
            return iter(())
        return _iter_jsonl(self.events_path)

    # ---------- helpers ----------
    @staticmethod
    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def atomic_write_json(path: Path, payload: Any) -> None:
        """Write ``payload`` as JSON to ``path`` atomically.

        Uses a sibling ``.tmp`` file plus :func:`os.replace`, so a crash
        mid-write never leaves the destination half-written. Parent
        directories are created if missing. Public so other modules
        (notably ``gates.write_state``) can adopt the same atomicity.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            yield json.loads(line)

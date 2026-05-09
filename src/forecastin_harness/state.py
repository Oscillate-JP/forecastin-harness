"""Harness state directory layout, lane registry, and JSONL audit log.

State lives at ``<target_repo>/<config.state.dir>`` (default ``.harness/state``).
Layout::

    .harness/state/
        lanes.json              registry of lane records
        events.jsonl            append-only audit log of harness events
        gates/                  one subtree per gate run (state.json + log)
        pr/                     PR check + merge-ready records
        tasks/                  task packets and agent results

All state is plain JSON / JSONL so it's auditable, diffable, and recoverable
by hand.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
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

    The store does not lock. Concurrent operators are expected to use distinct
    lane names; the audit log makes a collision visible in postmortems.
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

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in (self.gates_dir, self.pr_dir, self.tasks_dir):
            sub.mkdir(parents=True, exist_ok=True)
        if not self.lanes_path.exists():
            self._write_json(self.lanes_path, {"lanes": []})
        if not self.events_path.exists():
            self.events_path.touch()

    # ---------- lanes ----------
    def load_lanes(self) -> list[Lane]:
        if not self.lanes_path.exists():
            return []
        data = self._read_json(self.lanes_path)
        if not isinstance(data, dict) or "lanes" not in data:
            raise StateError(f"corrupt lanes file: {self.lanes_path}")
        return [Lane.from_dict(item) for item in data["lanes"]]

    def save_lanes(self, lanes: Iterable[Lane]) -> None:
        self._write_json(
            self.lanes_path,
            {"lanes": [lane.to_dict() for lane in lanes]},
        )

    def find_lane(self, name: str) -> Lane | None:
        for lane in self.load_lanes():
            if lane.name == name:
                return lane
        return None

    def upsert_lane(self, lane: Lane) -> None:
        lanes = self.load_lanes()
        replaced = False
        for i, existing in enumerate(lanes):
            if existing.name == lane.name:
                lanes[i] = lane
                replaced = True
                break
        if not replaced:
            lanes.append(lane)
        self.save_lanes(lanes)

    def add_lane(self, lane: Lane) -> None:
        if self.find_lane(lane.name) is not None:
            raise StateError(f"lane already exists: {lane.name}")
        lanes = self.load_lanes()
        lanes.append(lane)
        self.save_lanes(lanes)

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
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish write: temp file + replace, so a crash mid-write doesn't
        # corrupt an existing registry.
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

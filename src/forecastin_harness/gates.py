"""Long-running CI gate state model and dry-run planner.

A *gate* is a long-running shell command (typically a backend test suite)
that must run outside the agent's fragile shell timeout. Each gate has:

* a name (``backend-pytest``, ``frontend-vitest``, ``full-ci``…),
* a planned command,
* a state file at ``<state>/gates/<name>.state.json``,
* a log file at ``<state>/gates/<name>.log``,
* an exit-code marker at ``<state>/gates/<name>.exit``.

Gate states (a deliberate superset of "did the tests pass?"):

* ``planned``   — gate has been planned but not started.
* ``running``   — process is alive (or believed to be).
* ``passed``    — terminated with exit 0.
* ``failed``    — terminated with non-zero exit.
* ``killed``    — operator stopped it.
* ``timeout``   — wrapper killed it for exceeding the configured deadline.
* ``unknown``   — cannot determine; e.g. process gone but no exit marker.

This module implements the **state model** and the **dry-run planner**.
Background spawn / supervision is deliberately deferred to a follow-up pass
(see ``docs/architecture.md`` § Roadmap). Even without spawning, the state
model is useful: a separate spawner script can write the state files and
the harness CLI can read them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .state import StateStore, utcnow_iso  # StateStore.atomic_write_json adopted below

GateStatus = Literal[
    "planned",
    "running",
    "passed",
    "failed",
    "killed",
    "timeout",
    "unknown",
]

GATE_STATUSES: tuple[GateStatus, ...] = (
    "planned",
    "running",
    "passed",
    "failed",
    "killed",
    "timeout",
    "unknown",
)


@dataclass
class GateState:
    name: str
    command: str
    status: GateStatus = "planned"
    pid: int | None = None
    exit_code: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    log_path: str | None = None
    notes: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "GateState":
        known = {f for f in GateState.__dataclass_fields__}
        cleaned = {k: v for k, v in data.items() if k in known}
        return GateState(**cleaned)


@dataclass(frozen=True)
class GatePlan:
    name: str
    command: str
    state_path: Path
    log_path: Path
    exit_path: Path

    def render(self) -> str:
        """Operator-facing rendering used by ``gate start --dry-run``."""
        lines = [
            f"gate: {self.name}",
            f"command: {self.command}",
            f"state file: {self.state_path}",
            f"log file:   {self.log_path}",
            f"exit file:  {self.exit_path}",
            "",
            "Planned execution (background; not run in dry-run):",
            f"  bash -c {self.command!r} > {self.log_path} 2>&1; echo $? > {self.exit_path}",
            "",
            "After invocation, status transitions:",
            "  planned -> running -> passed | failed | killed | timeout",
            "  any other observation -> unknown",
        ]
        return "\n".join(lines)


def plan_gate(store: StateStore, *, name: str, command: str) -> GatePlan:
    """Build the on-disk plan for a gate without launching anything."""
    if not name:
        raise ValueError("gate name is required")
    if "/" in name or "\\" in name:
        # Names become filenames; keep them flat to avoid surprises.
        raise ValueError("gate name must not contain path separators")
    if not command.strip():
        raise ValueError("gate command must be non-empty")
    base = store.gates_dir / name
    return GatePlan(
        name=name,
        command=command,
        state_path=base.with_suffix(".state.json"),
        log_path=base.with_suffix(".log"),
        exit_path=base.with_suffix(".exit"),
    )


def write_state(plan: GatePlan, state: GateState) -> None:
    """Persist a gate state atomically, creating parent directories if needed.

    Adopts :meth:`StateStore.atomic_write_json` so that a crash mid-write
    cannot leave the gate state file truncated or corrupt — the previous
    contents (or absence) remain visible until the rename succeeds.
    """
    StateStore.atomic_write_json(plan.state_path, state.to_dict())


def read_state(plan: GatePlan) -> GateState | None:
    if not plan.state_path.exists():
        return None
    return GateState.from_dict(json.loads(plan.state_path.read_text(encoding="utf-8")))


def initialise(plan: GatePlan, store: StateStore) -> GateState:
    """Idempotent: create the planned state if it does not yet exist.

    If a state file already exists (e.g. from a previous run), it is left
    alone. The CLI can re-arm it via an explicit transition.
    """
    existing = read_state(plan)
    if existing is not None:
        return existing
    state = GateState(
        name=plan.name,
        command=plan.command,
        status="planned",
        log_path=str(plan.log_path),
    )
    write_state(plan, state)
    store.append_event({"kind": "gate.planned", "gate": plan.name, "command": plan.command})
    return state


def transition(
    plan: GatePlan,
    store: StateStore,
    *,
    new_status: GateStatus,
    exit_code: int | None = None,
    pid: int | None = None,
    notes: str | None = None,
) -> GateState:
    """Move a gate to a new status, recording the transition in history.

    This is what a future background-spawner / supervisor process will call
    when it observes process events. Tests use it directly to verify that
    the state machine accepts the documented states.
    """
    if new_status not in GATE_STATUSES:
        raise ValueError(f"invalid gate status: {new_status!r}")

    current = read_state(plan)
    if current is None:
        raise FileNotFoundError(f"gate not initialised: {plan.state_path}")

    transition_record = {
        "ts": utcnow_iso(),
        "from": current.status,
        "to": new_status,
        "exit_code": exit_code,
        "pid": pid,
        "notes": notes,
    }
    current.history.append(transition_record)
    current.status = new_status
    if pid is not None:
        current.pid = pid
    if exit_code is not None:
        current.exit_code = exit_code
    if notes is not None:
        current.notes = notes
    if new_status == "running" and current.started_at is None:
        current.started_at = utcnow_iso()
    if new_status in {"passed", "failed", "killed", "timeout"}:
        current.ended_at = utcnow_iso()

    write_state(plan, current)
    store.append_event(
        {
            "kind": "gate.transition",
            "gate": plan.name,
            "from": transition_record["from"],
            "to": new_status,
            "exit_code": exit_code,
        }
    )
    return current


def tail_text(plan: GatePlan, *, lines: int = 50) -> str:
    """Return the last ``lines`` lines of the gate log, or an empty string."""
    if not plan.log_path.exists():
        return ""
    # Naive but adequate for typical log sizes; CI logs of GB scale would need
    # a seek-based approach.
    text = plan.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])

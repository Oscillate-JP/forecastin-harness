"""Background gate supervisor.

The supervisor turns a planned gate into a running process and updates the
gate state machine until the process either exits cleanly, exits non-zero,
or is killed for breaching its deadline. Logs stream to a deterministic
file under the state directory.

This module deliberately avoids any threading or async machinery: the
operator-facing semantics are linear (start → wait → terminal), so a
single-process supervisor is enough. Tests exercise it with tiny
``python -c`` shells so the suite stays hermetic and fast.

Trust boundary:
    The user-supplied command is shell-evaluated exactly once when
    :class:`CommandSpec` is in ``posix`` or ``powershell`` mode. We never
    pass ``shell=True`` to subprocess; the shell binary is named explicitly
    and the snippet is a single argv element. Operators should treat the
    snippet as code they themselves wrote.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .gates import GatePlan, GateState, plan_gate, read_state, transition, write_state
from .rendering import CommandSpec
from .state import StateStore, utcnow_iso

DEFAULT_DEADLINE_SECONDS = 60 * 60  # 1 hour: matches the canonical Forecastin gate
KILL_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class SupervisorResult:
    state: GateState
    timed_out: bool


def supervise(
    store: StateStore,
    *,
    name: str,
    command: CommandSpec,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    poll_interval: float = 0.05,
    popen_factory=subprocess.Popen,
) -> SupervisorResult:
    """Run a gate to completion (or to its deadline) and persist state.

    Parameters
    ----------
    store:
        State store; used for the gate state file path and the audit log.
    name:
        Gate name. Becomes the basename for state, log, and exit-code files.
    command:
        :class:`CommandSpec` describing what to execute. The supervisor
        passes ``command.to_subprocess_argv()`` to ``popen_factory`` with
        ``shell=False``.
    deadline_seconds:
        Wall-clock seconds before the supervisor sends a soft kill, then a
        hard kill. Default mirrors the Forecastin backend pytest budget.
    poll_interval:
        How often to check process status. Tests use a small value to keep
        the suite fast; production callers may use larger values.
    popen_factory:
        Injection point for tests. Default is :class:`subprocess.Popen`.

    Returns
    -------
    SupervisorResult
        Final gate state and a flag that distinguishes timeout from a
        natural exit.
    """
    plan = plan_gate(store, name=name, command=command.display())
    plan.state_path.parent.mkdir(parents=True, exist_ok=True)

    state = _initialise_running(plan, store, command)
    store.append_event(
        {
            "kind": "gate.start",
            "gate": name,
            "command": command.display(),
            "deadline_seconds": deadline_seconds,
        }
    )

    log_handle = plan.log_path.open("wb")
    try:
        proc = popen_factory(
            command.to_subprocess_argv(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            shell=False,
            cwd=str(_target_cwd(store)),
        )
    except FileNotFoundError as exc:
        log_handle.close()
        # Treat a missing executable as an immediate failure with a clear note.
        state.notes = f"executable not found: {exc}"
        terminal = transition(plan, store, new_status="failed", exit_code=127, notes=state.notes)
        return SupervisorResult(state=terminal, timed_out=False)

    state.pid = proc.pid
    state.log_path = str(plan.log_path)
    write_state(plan, state)
    store.append_event({"kind": "gate.running", "gate": name, "pid": proc.pid})

    timed_out = False
    started = time.monotonic()
    try:
        while True:
            ret = proc.poll()
            if ret is not None:
                break
            if time.monotonic() - started >= deadline_seconds:
                timed_out = True
                _terminate(proc)
                break
            time.sleep(poll_interval)
        ret = proc.wait(timeout=KILL_GRACE_SECONDS) if proc.poll() is None else proc.returncode
    finally:
        log_handle.close()

    plan.exit_path.write_text(f"{ret}\n", encoding="utf-8")

    if timed_out:
        terminal = transition(
            plan,
            store,
            new_status="timeout",
            exit_code=ret,
            notes=f"deadline={deadline_seconds}s exceeded",
        )
        store.append_event({"kind": "gate.timeout", "gate": name, "deadline_seconds": deadline_seconds})
    elif ret == 0:
        terminal = transition(plan, store, new_status="passed", exit_code=ret)
        store.append_event({"kind": "gate.exit", "gate": name, "exit_code": 0})
    else:
        terminal = transition(plan, store, new_status="failed", exit_code=ret)
        store.append_event({"kind": "gate.exit", "gate": name, "exit_code": ret})

    return SupervisorResult(state=terminal, timed_out=timed_out)


def stop(store: StateStore, *, name: str) -> GateState:
    """Best-effort stop of a running gate by recorded PID.

    Idempotent: if the gate is not running, the call is a no-op transition
    that records the observation. The caller decides whether to treat that
    as success.
    """
    plan = plan_gate(store, name=name, command="placeholder")
    state = read_state(plan)
    if state is None:
        raise FileNotFoundError(f"gate not found: {plan.state_path}")

    if state.status not in {"running", "planned"}:
        # Nothing to stop; record the observation for the audit log.
        store.append_event(
            {
                "kind": "gate.stop.noop",
                "gate": name,
                "observed_status": state.status,
            }
        )
        return state

    if state.pid is None:
        return transition(
            plan,
            store,
            new_status="unknown",
            notes="stop requested but no pid recorded",
        )

    _terminate_pid(state.pid)
    store.append_event({"kind": "gate.kill", "gate": name, "pid": state.pid})
    return transition(plan, store, new_status="killed", notes="operator-stopped")


def summarise(store: StateStore, *, name: str) -> dict[str, object]:
    """One-shot operator-facing summary of a gate.

    Returns a plain dict so the CLI can pretty-print it as JSON without
    juggling dataclasses. Reads only state files; never starts processes.
    """
    plan = plan_gate(store, name=name, command="placeholder")
    state = read_state(plan)
    if state is None:
        return {"name": name, "status": "missing", "state_path": str(plan.state_path)}
    log_size = plan.log_path.stat().st_size if plan.log_path.exists() else 0
    return {
        "name": state.name,
        "status": state.status,
        "command": state.command,
        "pid": state.pid,
        "exit_code": state.exit_code,
        "started_at": state.started_at,
        "ended_at": state.ended_at,
        "log_path": state.log_path,
        "log_bytes": log_size,
        "transitions": len(state.history),
        "notes": state.notes,
    }


# ---------------- internal helpers ----------------


def _initialise_running(plan: GatePlan, store: StateStore, command: CommandSpec) -> GateState:
    """Idempotently create the state file and immediately mark it running.

    We use the existing transition machinery to keep the audit history
    consistent; the prior state is set to ``planned`` by hand if no state
    yet exists.
    """
    existing = read_state(plan)
    if existing is None:
        existing = GateState(
            name=plan.name,
            command=command.display(),
            status="planned",
            log_path=str(plan.log_path),
        )
        write_state(plan, existing)
        store.append_event({"kind": "gate.planned", "gate": plan.name, "command": command.display()})
    elif existing.status in {"passed", "failed", "killed", "timeout"}:
        # Re-arm: rewrite as planned so transitions stay coherent.
        existing.status = "planned"
        existing.exit_code = None
        existing.started_at = None
        existing.ended_at = None
        existing.history.append(
            {
                "ts": utcnow_iso(),
                "from": "(rearm)",
                "to": "planned",
                "exit_code": None,
                "pid": None,
                "notes": "supervisor re-armed gate from terminal state",
            }
        )
        write_state(plan, existing)
    return transition(plan, store, new_status="running", notes="supervisor started")


def _target_cwd(store: StateStore) -> Path:
    """The cwd a gate inherits.

    The state directory is guaranteed to exist; we step up to the first
    ancestor that contains a ``.git`` directory (the target repo root) so
    relative gate commands like ``bash scripts/ci.sh`` resolve correctly.
    Falls back to the state directory if no ancestor matches.
    """
    for parent in (store.root, *store.root.parents):
        if (parent / ".git").exists():
            return parent
    return store.root


def _terminate(proc: subprocess.Popen) -> None:
    """Send SIGTERM, wait briefly, then SIGKILL if still alive."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:  # pragma: no cover - platform-dependent rare error
        pass
    try:
        proc.wait(timeout=KILL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except Exception:  # pragma: no cover
        pass


def _terminate_pid(pid: int) -> None:
    """Send a polite signal to a pid we recorded earlier.

    Windows lacks SIGTERM semantics, so we use ``taskkill``. POSIX uses
    ``os.kill``. We do not retry; the caller logs the outcome.
    """
    name = getattr(os, "name")
    if name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

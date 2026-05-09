"""End-to-end tests for the gate supervisor.

These tests spawn real subprocesses but use tiny ``python -c`` shells so
the suite stays fast and hermetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from forecastin_harness.gate_supervisor import (
    DEFAULT_DEADLINE_SECONDS,
    stop,
    summarise,
    supervise,
)
from forecastin_harness.gates import plan_gate
from forecastin_harness.rendering import from_argv
from forecastin_harness.state import StateStore


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    return store


def test_supervise_passing_command(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = from_argv([sys.executable, "-c", "import sys; sys.exit(0)"])
    result = supervise(store, name="smoke", command=spec, deadline_seconds=10.0)
    assert result.state.status == "passed"
    assert result.state.exit_code == 0
    assert result.timed_out is False
    assert result.state.started_at is not None
    assert result.state.ended_at is not None

    plan = plan_gate(store, name="smoke", command="placeholder")
    assert plan.exit_path.exists()
    assert plan.exit_path.read_text(encoding="utf-8").strip() == "0"


def test_supervise_failing_command(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = from_argv([sys.executable, "-c", "import sys; sys.exit(3)"])
    result = supervise(store, name="fail", command=spec, deadline_seconds=10.0)
    assert result.state.status == "failed"
    assert result.state.exit_code == 3


def test_supervise_timeout_records_timeout_not_failed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Long sleep, tiny deadline → must hit the timeout path.
    spec = from_argv([sys.executable, "-c", "import time; time.sleep(30)"])
    result = supervise(
        store,
        name="slowpoke",
        command=spec,
        deadline_seconds=0.5,
        poll_interval=0.05,
    )
    assert result.state.status == "timeout"
    assert result.timed_out is True


def test_supervise_writes_log(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = from_argv([sys.executable, "-c", "print('marker-stdout')"])
    supervise(store, name="logging", command=spec, deadline_seconds=10.0)
    plan = plan_gate(store, name="logging", command="placeholder")
    text = plan.log_path.read_text(encoding="utf-8")
    assert "marker-stdout" in text


def test_supervise_records_audit_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = from_argv([sys.executable, "-c", "import sys; sys.exit(0)"])
    supervise(store, name="audit", command=spec, deadline_seconds=10.0)

    kinds = [e["kind"] for e in store.iter_events()]
    # At minimum: start, running transition, exit, and the gate.transition events.
    assert "gate.start" in kinds
    assert "gate.running" in kinds
    assert "gate.exit" in kinds
    # The gate state machine still emits its own transitions.
    assert "gate.transition" in kinds


def test_summarise_missing_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    summary = summarise(store, name="never-ran")
    assert summary["status"] == "missing"


def test_summarise_after_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = from_argv([sys.executable, "-c", "print('ok')"])
    supervise(store, name="finished", command=spec, deadline_seconds=10.0)
    summary = summarise(store, name="finished")
    assert summary["status"] == "passed"
    assert summary["exit_code"] == 0
    assert int(summary["log_bytes"]) > 0
    assert summary["transitions"] >= 1


def test_stop_on_terminal_state_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = from_argv([sys.executable, "-c", "import sys; sys.exit(0)"])
    supervise(store, name="done", command=spec, deadline_seconds=10.0)

    state = stop(store, name="done")
    # Already passed; stop is a no-op observation.
    assert state.status == "passed"
    kinds = [e["kind"] for e in store.iter_events()]
    assert "gate.stop.noop" in kinds


def test_stop_unknown_gate_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(FileNotFoundError):
        stop(store, name="ghost")


def test_supervise_handles_missing_executable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spec = from_argv(["this-binary-does-not-exist-xyzzy"])
    result = supervise(store, name="missing-exe", command=spec, deadline_seconds=5.0)
    assert result.state.status == "failed"
    assert result.state.exit_code == 127


def test_default_deadline_is_one_hour() -> None:
    assert DEFAULT_DEADLINE_SECONDS == 3600


def test_supervise_has_no_dry_run_parameter() -> None:
    """Sanity: dry-run lives in the CLI, not the supervisor.

    Pinning this contract stops a future refactor from sneaking a dry-run
    flag into ``supervise()`` and silently skipping audit events.
    """
    import inspect

    sig = inspect.signature(supervise)
    assert "dry_run" not in sig.parameters

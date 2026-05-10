"""Push watchdog — detect hung pre-push pytest / git invocations.

Background
==========

The 2026-05-09 drain spawned three concurrent pytest processes that
each consumed ~1 GB and never produced output. The push that triggered
them was hung for 30+ minutes; the harness had no way to surface that
the operator was paying for stalled work. This module classifies a
process snapshot and produces a verdict the CLI can print.

Design
------

* Pure synthesis: :func:`evaluate_processes` accepts an iterable of
  :class:`ProcessRow` (re-imported from :mod:`controller`) plus a
  threshold and returns a :class:`WatchdogReport`. Tests inject a
  fixed list and never spawn shells.
* The watchdog is a *report*, not an actor. It names PIDs and
  suggests ``taskkill`` / ``kill`` commands but never runs them. The
  operator decides; the harness logs.
* The watchdog distinguishes two failure modes:
  1. ``orphan_pytest`` — a pytest process running for ≥
     ``age_threshold_seconds``. Memory is not consulted because
     ``ProcessRow`` does not carry it; the threshold is age-only.
  2. ``no_output_hang`` — a pre-push hook is alive but no new bytes
     have been written to its stdout for ≥ ``output_silence_seconds``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .controller import ProcessRow

#: Default age threshold above which a pytest process is suspect.
DEFAULT_AGE_THRESHOLD_SECONDS: int = 300

#: Default output-silence threshold for a no-output-hang. 90 seconds is
#: long enough that legitimate slow tests aren't false-positives but
#: short enough that operators don't pay for 30 min of silence.
DEFAULT_OUTPUT_SILENCE_SECONDS: int = 90


@dataclass(frozen=True)
class HungProcess:
    """One process the watchdog has flagged."""

    pid: int
    name: str
    cmdline: str
    runtime_seconds: int | None
    memory_mb: int | None
    reason: str  # "orphan_pytest" | "no_output_hang"
    suggested_kill_command: str  # operator-runnable; not executed


@dataclass(frozen=True)
class WatchdogReport:
    """Result of :func:`evaluate_processes`."""

    flagged: tuple[HungProcess, ...]
    age_threshold_seconds: int
    output_silence_seconds: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.flagged


@dataclass(frozen=True)
class OutputAge:
    """Description of a background command's output age.

    The CLI provides this when it has access to the subprocess's
    stdout file: how many bytes are in it and how long ago the file
    was last modified. The watchdog combines this with the process
    list to produce ``no_output_hang`` findings.
    """

    pid: int
    last_byte_age_seconds: int
    bytes_seen: int


def evaluate_processes(
    processes: Iterable[ProcessRow],
    *,
    age_threshold_seconds: int = DEFAULT_AGE_THRESHOLD_SECONDS,
    output_ages: Sequence[OutputAge] = (),
    output_silence_seconds: int = DEFAULT_OUTPUT_SILENCE_SECONDS,
) -> WatchdogReport:
    """Classify a process snapshot, returning a :class:`WatchdogReport`.

    A process is flagged ``orphan_pytest`` when its name or cmdline
    matches pytest patterns AND its runtime is at-or-above
    ``age_threshold_seconds``. ``ProcessRow`` does not carry memory
    in this version, so the watchdog does not enforce a memory
    threshold; the field was removed to keep the API and the
    docstring honest.

    A process is flagged ``no_output_hang`` when:

    * its PID is in ``output_ages``,
    * its associated ``last_byte_age_seconds`` ≥ ``output_silence_seconds``.
    """
    flagged: list[HungProcess] = []
    output_age_by_pid = {oa.pid: oa for oa in output_ages}

    for row in processes:
        # 1. Orphan pytest detection.
        if _is_pytest(row) and (row.runtime_seconds or 0) >= age_threshold_seconds:
            flagged.append(
                HungProcess(
                    pid=row.pid,
                    name=row.name,
                    cmdline=row.cmdline,
                    runtime_seconds=row.runtime_seconds,
                    memory_mb=None,
                    reason="orphan_pytest",
                    suggested_kill_command=_kill_command(row.pid),
                )
            )
            continue
        # 2. No-output hang detection.
        oa = output_age_by_pid.get(row.pid)
        if oa is not None and oa.last_byte_age_seconds >= output_silence_seconds:
            flagged.append(
                HungProcess(
                    pid=row.pid,
                    name=row.name,
                    cmdline=row.cmdline,
                    runtime_seconds=row.runtime_seconds,
                    memory_mb=None,
                    reason="no_output_hang",
                    suggested_kill_command=_kill_command(row.pid),
                )
            )

    return WatchdogReport(
        flagged=tuple(flagged),
        age_threshold_seconds=age_threshold_seconds,
        output_silence_seconds=output_silence_seconds,
    )


def _is_pytest(row: ProcessRow) -> bool:
    haystack = f"{row.name} {row.cmdline}".lower()
    return "pytest" in haystack or "py.test" in haystack


def _kill_command(pid: int) -> str:
    """Return a documented operator-runnable kill command.

    On Windows the operator uses ``taskkill /F /PID <pid>``; on POSIX
    ``kill -TERM <pid>`` (escalating to ``-KILL`` if needed). The
    watchdog never picks one for the operator — it lists both.
    """
    return f"taskkill /F /PID {pid}    # POSIX: kill -TERM {pid} || kill -KILL {pid}"


def render_watchdog_report(report: WatchdogReport) -> str:
    """Human-readable rendering used by the CLI."""
    if report.ok:
        return (
            f"watchdog ok: no hung pytest / no-output processes "
            f"(age_threshold={report.age_threshold_seconds}s, "
            f"silence_threshold={report.output_silence_seconds}s)"
        )
    lines = [f"watchdog FAIL: {len(report.flagged)} flagged process(es)"]
    for hp in report.flagged:
        runtime = "?" if hp.runtime_seconds is None else f"{hp.runtime_seconds}s"
        lines.append(
            f"  pid={hp.pid:>7} reason={hp.reason} runtime={runtime} "
            f"name={hp.name} cmd={hp.cmdline}"
        )
        lines.append(f"    suggested: {hp.suggested_kill_command}")
    lines.append(
        "  Operator action only — the harness never auto-kills "
        "without explicit authorisation."
    )
    return "\n".join(lines)

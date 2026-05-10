"""Tests for the push_watchdog module.

The watchdog is a pure function over a process snapshot; no live
process table is touched.
"""

from __future__ import annotations

from forecastin_harness.controller import ProcessRow
from forecastin_harness.push_watchdog import (
    DEFAULT_AGE_THRESHOLD_SECONDS,
    OutputAge,
    evaluate_processes,
    render_watchdog_report,
)


def test_no_processes_is_ok() -> None:
    report = evaluate_processes([])
    assert report.ok is True
    assert report.flagged == ()
    assert "ok" in render_watchdog_report(report).lower()


def test_short_pytest_does_not_flag() -> None:
    rows = [
        ProcessRow(pid=1234, name="python.exe", cmdline="python -m pytest", runtime_seconds=30),
    ]
    report = evaluate_processes(rows)
    assert report.ok is True


def test_long_pytest_flags_as_orphan() -> None:
    rows = [
        ProcessRow(
            pid=4242,
            name="python.exe",
            cmdline="python -m pytest tests/api/v1",
            runtime_seconds=DEFAULT_AGE_THRESHOLD_SECONDS + 10,
        ),
    ]
    report = evaluate_processes(rows)
    assert report.ok is False
    assert len(report.flagged) == 1
    h = report.flagged[0]
    assert h.pid == 4242
    assert h.reason == "orphan_pytest"
    assert "taskkill" in h.suggested_kill_command


def test_no_output_hang_flagged() -> None:
    rows = [
        ProcessRow(pid=99, name="git.exe", cmdline="git push", runtime_seconds=20),
    ]
    output_ages = [OutputAge(pid=99, last_byte_age_seconds=300, bytes_seen=10)]
    report = evaluate_processes(rows, output_ages=output_ages, output_silence_seconds=90)
    assert report.ok is False
    h = report.flagged[0]
    assert h.reason == "no_output_hang"


def test_render_lists_pid_and_kill_command() -> None:
    rows = [
        ProcessRow(
            pid=4242,
            name="python.exe",
            cmdline="python -m pytest",
            runtime_seconds=DEFAULT_AGE_THRESHOLD_SECONDS + 10,
        ),
    ]
    report = evaluate_processes(rows)
    text = render_watchdog_report(report)
    assert "watchdog FAIL" in text
    assert "pid=   4242" in text or "pid=4242" in text
    assert "taskkill" in text
    assert "Operator action only" in text


def test_pytest_below_threshold_not_flagged_even_with_output_age() -> None:
    """Output-age flagging applies to non-pytest processes too; here a
    fast pytest with no output-age entry must not be flagged."""
    rows = [
        ProcessRow(pid=100, name="python.exe", cmdline="python -m pytest", runtime_seconds=10),
    ]
    report = evaluate_processes(rows)
    assert report.ok is True

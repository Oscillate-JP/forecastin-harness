"""Controller-doctor environment tests.

These exercise the pure :func:`diagnose` synthesis function with a fixed
list of :class:`ProcessRow` objects (no live process table) and a stub
git probe (no live ``git`` invocation). The CLI integration test
monkeypatches ``cli.enumerate_processes`` and ``cli.probe_git`` for the
same reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forecastin_harness import cli
from forecastin_harness.controller import (
    LONG_RUNTIME_THRESHOLD_SECONDS,
    ProcessRow,
    diagnose,
    render_doctor_report,
)


# ---------------- helpers ----------------


def _stub_git(branch: str | None, dirty: bool | None):
    def _probe(_cwd: Path) -> tuple[str | None, bool | None]:
        return branch, dirty

    return _probe


# ---------------- diagnose ----------------


def test_doctor_classifies_processes_into_buckets(tmp_path: Path) -> None:
    rows = [
        ProcessRow(pid=11, name="python.exe", cmdline="pytest tests/", runtime_seconds=30),
        ProcessRow(pid=12, name="python.exe", cmdline="manage.py shell", runtime_seconds=5),
        ProcessRow(pid=13, name="bash.exe", cmdline="-l", runtime_seconds=200),
        ProcessRow(pid=14, name="powershell.exe", cmdline="profile", runtime_seconds=400),
        ProcessRow(pid=15, name="explorer.exe", cmdline="", runtime_seconds=10000),
    ]
    report = diagnose(
        cwd=tmp_path,
        target_path=tmp_path,
        processes=rows,
        git_probe=_stub_git("main", False),
    )
    pytest_pids = {r.pid for r in report.pytest_processes}
    python_pids = {r.pid for r in report.python_processes}
    shell_pids = {r.pid for r in report.shell_processes}

    assert pytest_pids == {11}
    # PID 12 is python and NOT pytest, so it lives in the python bucket only.
    assert python_pids == {12}
    assert shell_pids == {13, 14}
    # Pytest must not double-count into python.
    assert pytest_pids.isdisjoint(python_pids)


def test_doctor_warns_on_multiple_long_pytest_processes(tmp_path: Path) -> None:
    rows = [
        ProcessRow(
            pid=21,
            name="python.exe",
            cmdline="pytest tests/",
            runtime_seconds=LONG_RUNTIME_THRESHOLD_SECONDS + 60,
        ),
        ProcessRow(
            pid=22,
            name="python.exe",
            cmdline="pytest -x backend/tests",
            runtime_seconds=LONG_RUNTIME_THRESHOLD_SECONDS + 200,
        ),
        ProcessRow(
            pid=23,
            name="python.exe",
            cmdline="pytest --collect-only",
            runtime_seconds=10,  # short — does not count
        ),
    ]
    report = diagnose(
        cwd=tmp_path,
        target_path=tmp_path,
        processes=rows,
        git_probe=_stub_git("main", False),
    )
    joined = " ".join(report.warnings)
    assert "pytest processes have run for" in joined
    assert "21" in joined and "22" in joined
    # The short pytest must NOT be flagged as orphan.
    assert "23" not in joined


def test_doctor_does_not_warn_on_single_long_pytest(tmp_path: Path) -> None:
    rows = [
        ProcessRow(
            pid=31,
            name="python.exe",
            cmdline="pytest tests/",
            runtime_seconds=LONG_RUNTIME_THRESHOLD_SECONDS + 60,
        ),
    ]
    report = diagnose(
        cwd=tmp_path,
        target_path=tmp_path,
        processes=rows,
        git_probe=_stub_git("main", False),
    )
    joined = " ".join(report.warnings)
    assert "pytest processes have run for" not in joined


def test_doctor_warns_on_cwd_target_mismatch(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    report = diagnose(
        cwd=tmp_path,
        target_path=other,
        processes=[],
        git_probe=_stub_git("main", False),
    )
    assert not report.cwd_matches_target
    assert any("does not match configured target" in w for w in report.warnings)


def test_doctor_no_target_path_no_match_warning(tmp_path: Path) -> None:
    """Without a target_path, doctor must NOT emit a mismatch warning."""
    report = diagnose(
        cwd=tmp_path,
        target_path=None,
        processes=[],
        git_probe=_stub_git("main", False),
    )
    assert not report.cwd_matches_target
    assert all("does not match configured target" not in w for w in report.warnings)


def test_doctor_passes_branch_and_dirty_through(tmp_path: Path) -> None:
    report = diagnose(
        cwd=tmp_path,
        target_path=tmp_path,
        processes=[],
        git_probe=_stub_git("feat/x", True),
    )
    assert report.current_branch == "feat/x"
    assert report.dirty is True


def test_doctor_no_git_probe_returns_unknowns(tmp_path: Path) -> None:
    report = diagnose(
        cwd=tmp_path, target_path=tmp_path, processes=[], git_probe=None
    )
    assert report.current_branch is None
    assert report.dirty is None


def test_doctor_render_smoke(tmp_path: Path) -> None:
    rows = [
        ProcessRow(pid=41, name="python.exe", cmdline="pytest", runtime_seconds=600),
    ]
    report = diagnose(
        cwd=tmp_path,
        target_path=tmp_path,
        processes=rows,
        git_probe=_stub_git("main", False),
    )
    text = render_doctor_report(report)
    assert "cwd:" in text
    assert "current_branch:     main" in text
    assert "pytest processes (1)" in text


# ---------------- CLI integration ----------------


def test_cli_controller_doctor_no_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """doctor without --config must succeed and skip the target match check."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "enumerate_processes",
        lambda: [
            ProcessRow(pid=99, name="python.exe", cmdline="pytest", runtime_seconds=10),
        ],
    )
    monkeypatch.setattr(cli, "probe_git", lambda _cwd: ("main", False))

    rc = cli.main(["controller", "doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "target_path:        (no config)" in out
    assert "pytest processes (1)" in out
    assert "current_branch:     main" in out


def test_cli_controller_doctor_with_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_target_repo: Path,
    example_config_file: Path,
) -> None:
    """doctor with --config compares cwd against target.path."""
    other_dir = fake_target_repo.parent / "elsewhere"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    monkeypatch.setattr(cli, "enumerate_processes", lambda: [])
    monkeypatch.setattr(cli, "probe_git", lambda _cwd: ("feat/x", True))

    rc = cli.main(
        ["controller", "doctor", "--config", str(example_config_file)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "cwd_matches_target: False" in out
    assert "does not match configured target" in out


def test_cli_controller_doctor_bad_config(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bogus = tmp_path / "bogus.yaml"
    bogus.write_text("not: { a: valid", encoding="utf-8")
    rc = cli.main(["controller", "doctor", "--config", str(bogus)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "config error" in err

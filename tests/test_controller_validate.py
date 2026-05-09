"""Controller-validate safety tests.

These pin the FOR-220 lessons:

* a packet that mixes feature paths and gate-fix paths under
  ``files.allowed`` is rejected;
* a packet missing the ``official_gate_command`` is rejected;
* a packet whose ``merge_authority`` is anything other than ``human-only``
  is rejected;
* a packet that hides ``--no-verify`` (or any other bypass token) anywhere
  in its strings is rejected;
* a clean feature-lane packet PASSes;
* a clean gate-fix-lane packet PASSes.

Each test builds a packet dict in-place so the failure mode is locally
visible — no shared mutable fixture, no clever subclassing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from forecastin_harness import cli
from forecastin_harness.controller import (
    TaskPacketLoadError,
    load_packet_dict,
    validate_packet,
    validate_packet_file,
)


# ---------------- helpers ----------------


def _valid_feature_packet() -> dict[str, Any]:
    """A packet that, in isolation, MUST pass every controller check."""
    return {
        "task_id": "FOR-9101",
        "branch": "lane/for-9101-feature",
        "base_sha": "deadbeefcafef00d",
        "target_repo": "Oscillate-JP/Forecastin",
        "worktree": "/tmp/.harness/worktrees/lane-for-9101",
        "lane_type": "feature",
        "max_runtime_minutes": 60,
        "official_gate_command": "bash scripts/ci.sh",
        "diagnostic_commands_allowed": ["git status", "git diff"],
        "merge_authority": "human-only",
        "no_bypass_permissions": True,
        "mission": "Repair the /settings/groups 500 by adding a NULL guard.",
        "scope": "backend/app/api/v1/endpoints/settings.py and its test.",
        "files": {
            "allowed": [
                "backend/app/api/v1/endpoints/settings.py",
                "backend/tests/api/v1/endpoints/test_settings.py",
            ],
            "forbidden": [
                "scripts/ci.sh",
                "frontend/**",
            ],
        },
        "acceptance": ["targeted pytest passes"],
        "tests": ["pytest backend/tests/api/v1/endpoints/test_settings.py -x"],
        "stop_conditions": ["acceptance check passes"],
        "evidence": ["paste failing then passing pytest output"],
        "final_report": [
            "Summary",
            "Changed files (paths + rationale)",
            "Tests run (commands + tail)",
            "Remaining risks / follow-ups",
        ],
    }


def _valid_gate_fix_packet() -> dict[str, Any]:
    pkt = _valid_feature_packet()
    pkt["task_id"] = "FOR-9102"
    pkt["lane_type"] = "gate-fix"
    pkt["mission"] = "Re-pin scripts/ci.sh to the new pytest invocation."
    pkt["scope"] = "scripts/ci.sh and its smoke test only."
    pkt["files"] = {
        "allowed": ["scripts/ci.sh"],
        "forbidden": ["backend/app/**", "frontend/src/**"],
    }
    return pkt


def _write_packet(tmp_path: Path, packet: dict[str, Any]) -> Path:
    p = tmp_path / "packet.yaml"
    p.write_text(yaml.safe_dump(packet, sort_keys=False), encoding="utf-8")
    return p


# ---------------- happy paths ----------------


def test_valid_feature_lane_passes(tmp_path: Path) -> None:
    p = _write_packet(tmp_path, _valid_feature_packet())
    report = validate_packet_file(p)
    assert report.ok, [f"{f.code}: {f.message}" for f in report.findings]


def test_valid_gate_fix_lane_passes(tmp_path: Path) -> None:
    p = _write_packet(tmp_path, _valid_gate_fix_packet())
    report = validate_packet_file(p)
    assert report.ok, [f"{f.code}: {f.message}" for f in report.findings]


# ---------------- single-rule rejections ----------------


def test_mixed_feature_and_gate_fix_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    pkt["files"]["allowed"] = [
        "backend/app/api/v1/endpoints/settings.py",  # feature
        "scripts/ci.sh",  # gate-fix
    ]
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    codes = {f.code for f in report.errors}
    assert "mixed_feature_and_gate_fix" in codes


def test_missing_official_gate_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    del pkt["official_gate_command"]
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "missing_official_gate" in {f.code for f in report.errors}


def test_autonomous_merge_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    pkt["merge_authority"] = "auto"
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "autonomous_merge_not_allowed" in {f.code for f in report.errors}


def test_bypass_permissions_flag_false_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    pkt["no_bypass_permissions"] = False
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "bypass_permissions_not_disallowed" in {f.code for f in report.errors}


def test_bypass_token_anywhere_rejected(tmp_path: Path) -> None:
    """Even with the flag set, --no-verify in any string is a hard reject."""
    pkt = _valid_feature_packet()
    pkt["scope"] = pkt["scope"] + " (use --no-verify if hooks complain)"
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "bypass_token_detected" in {f.code for f in report.errors}


def test_missing_max_runtime_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    del pkt["max_runtime_minutes"]
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "missing_max_runtime" in {f.code for f in report.errors}


def test_missing_forbidden_files_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    pkt["files"]["forbidden"] = []
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "missing_forbidden_files" in {f.code for f in report.errors}


def test_missing_objective_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    pkt["mission"] = "   "
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "missing_objective" in {f.code for f in report.errors}


def test_missing_target_repo_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    pkt["target_repo"] = ""
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "missing_target_repo" in {f.code for f in report.errors}


def test_missing_worktree_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    pkt["worktree"] = ""
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "missing_worktree" in {f.code for f in report.errors}


def test_missing_final_report_contract_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    del pkt["final_report"]
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "missing_final_report_contract" in {f.code for f in report.errors}


def test_final_report_missing_required_topics_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    pkt["final_report"] = ["Summary"]  # missing the three required topics
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    codes = {f.code for f in report.errors}
    assert "final_report_missing_changed_files" in codes
    assert "final_report_missing_tests" in codes
    assert "final_report_missing_remaining_risks" in codes


def test_invalid_lane_type_rejected(tmp_path: Path) -> None:
    pkt = _valid_feature_packet()
    pkt["lane_type"] = "ad-hoc"
    p = _write_packet(tmp_path, pkt)
    report = validate_packet_file(p)
    assert not report.ok
    assert "invalid_lane_type" in {f.code for f in report.errors}


# ---------------- io errors ----------------


def test_validate_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(TaskPacketLoadError, match="not found"):
        validate_packet_file(tmp_path / "no-such-packet.yaml")


def test_validate_empty_file_raises(tmp_path: Path) -> None:
    bad = tmp_path / "empty.yaml"
    bad.write_text("", encoding="utf-8")
    with pytest.raises(TaskPacketLoadError, match="empty"):
        load_packet_dict(bad)


def test_validate_non_mapping_raises(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(TaskPacketLoadError, match="must be a mapping"):
        load_packet_dict(bad)


# ---------------- in-memory entrypoint ----------------


def test_validate_packet_in_memory_clean() -> None:
    """validate_packet works without round-tripping through disk."""
    report = validate_packet(_valid_feature_packet())
    assert report.ok, [f.code for f in report.findings]


# ---------------- CLI integration ----------------


def test_cli_controller_validate_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _write_packet(tmp_path, _valid_feature_packet())
    rc = cli.main(["controller", "validate", "--task-file", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS" in out


def test_cli_controller_validate_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pkt = _valid_feature_packet()
    pkt["merge_authority"] = "auto"
    pkt["no_bypass_permissions"] = False
    p = _write_packet(tmp_path, pkt)
    rc = cli.main(["controller", "validate", "--task-file", str(p)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "autonomous_merge_not_allowed" in out
    assert "bypass_permissions_not_disallowed" in out


def test_cli_controller_validate_io_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(
        ["controller", "validate", "--task-file", str(tmp_path / "missing.yaml")]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_cli_controller_validate_against_packaged_template() -> None:
    """The shipped task_packet.yaml must be controller-clean.

    Otherwise an operator who copies the template gets an immediate
    validation failure for no reason of their own.
    """
    from importlib import resources

    from forecastin_harness.prompts import TEMPLATES_PACKAGE

    yaml_path = Path(
        str(resources.files(TEMPLATES_PACKAGE).joinpath("task_packet.yaml"))
    )
    report = validate_packet_file(yaml_path)
    assert report.ok, [f.code for f in report.findings]

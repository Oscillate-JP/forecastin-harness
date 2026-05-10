"""Tests for the task_packet_templates module.

Generators must produce packets that pass the existing controller
validators (objective, target, worktree, official_gate,
files.forbidden, final_report contract, lane_type, merge_authority,
no_bypass_permissions).
"""

from __future__ import annotations

import yaml

from forecastin_harness.controller import validate_packet
from forecastin_harness.task_packet_templates import (
    GENERATORS,
    STANDARD_FINAL_REPORT,
    STANDARD_STOP_CONDITIONS,
    generate_docs_only_spec_batch,
    generate_linear_closeout,
    generate_open_pr_drain,
    generate_runtime_bugfix,
)


def test_open_pr_drain_packet_passes_validator() -> None:
    packet = generate_open_pr_drain(
        target_repo="J:/Forecastin-worktrees/_coordinator-main",
        worktree="J:/Forecastin-worktrees/_coordinator-main",
        pr_owner_repo="Oscillate-JP/Forecastin",
    )
    report = validate_packet(packet)
    assert report.ok, [f.code for f in report.errors]
    assert packet["lane_type"] == "review"
    assert packet["merge_authority"] == "human-only"
    assert packet["no_bypass_permissions"] is True


def test_docs_only_spec_batch_packet_passes_validator() -> None:
    packet = generate_docs_only_spec_batch(
        target_repo="J:/Forecastin-worktrees/_coordinator-main",
        worktree="J:/Forecastin-worktrees/_coordinator-main",
        parent_linear_id="FOR-244",
        child_linear_ids=["FOR-281", "FOR-282"],
        docs_path_glob="docs/ui2/UI2_PHASE_5_SPECS.md",
    )
    report = validate_packet(packet)
    assert report.ok, [f.code for f in report.errors]
    forbidden = packet["files"]["forbidden"]
    assert any("Forcastin" in f for f in forbidden)


def test_runtime_bugfix_packet_passes_validator() -> None:
    packet = generate_runtime_bugfix(
        target_repo="J:/Forecastin-worktrees/_coordinator-main",
        worktree="J:/Forecastin-worktrees/_coordinator-main",
        linear_id="FOR-220",
        failing_endpoint="/api/v1/intelligence/briefing/clustered",
        expected_runtime_evidence="curl returns 200 with non-empty stories[]",
    )
    report = validate_packet(packet)
    assert report.ok, [f.code for f in report.errors]
    assert packet["lane_type"] == "feature"
    assert "runtime evidence" in " ".join(packet["final_report"]).lower()


def test_linear_closeout_packet_passes_validator() -> None:
    packet = generate_linear_closeout(
        target_repo="J:/Forecastin-worktrees/_coordinator-main",
        worktree="J:/Forecastin-worktrees/_coordinator-main",
        parent_linear_id="FOR-213",
        expect_children_done=23,
    )
    report = validate_packet(packet)
    assert report.ok, [f.code for f in report.errors]


def test_all_packets_carry_drain_loop_semantics() -> None:
    """Stop conditions must be present so the agent never treats a
    lane report as a stopping point unless the rule says so."""
    expected_stops = set(STANDARD_STOP_CONDITIONS)
    for kind, fn in GENERATORS.items():
        if kind == "open-pr-drain":
            packet = fn(
                target_repo="t", worktree="w", pr_owner_repo="o/r",
            )
        elif kind == "docs-only-spec-batch":
            packet = fn(
                target_repo="t", worktree="w",
                parent_linear_id="FOR-1",
                child_linear_ids=["FOR-2"],
                docs_path_glob="docs/foo.md",
            )
        elif kind == "runtime-bugfix":
            packet = fn(
                target_repo="t", worktree="w",
                linear_id="FOR-1",
                failing_endpoint="/foo",
                expected_runtime_evidence="x",
            )
        elif kind == "linear-closeout":
            packet = fn(
                target_repo="t", worktree="w",
                parent_linear_id="FOR-1",
                expect_children_done=1,
            )
        else:
            raise AssertionError(f"untested generator kind: {kind}")
        stops = set(packet.get("stop_conditions") or [])
        assert expected_stops.issubset(stops), (
            f"kind {kind!r} missing stop conditions: {expected_stops - stops}"
        )
        report = list(packet.get("final_report") or [])
        for required in STANDARD_FINAL_REPORT:
            assert required in report, (
                f"kind {kind!r} missing final_report section {required!r}"
            )


def test_generated_yaml_round_trips() -> None:
    """YAML serialisation must preserve every field."""
    packet = generate_open_pr_drain(
        target_repo="J:/Forecastin-worktrees/_coordinator-main",
        worktree="J:/Forecastin-worktrees/_coordinator-main",
        pr_owner_repo="Oscillate-JP/Forecastin",
    )
    text = yaml.safe_dump(packet, sort_keys=False)
    loaded = yaml.safe_load(text)
    assert loaded == packet

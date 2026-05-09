"""Tests for the guarded PR merge planner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forecastin_harness.pr_gate import (
    PRMergeError,
    plan_pr_merge,
    render_merge_command,
)
from forecastin_harness.state import StateStore


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    return store


def _write_merge_ready(store: StateStore, *, pr: int, sha: str, verdict: str = "merge_ready") -> Path:
    path = store.pr_dir / f"{pr}.merge_ready.json"
    payload = {
        "pr_number": pr,
        "head_sha": sha,
        "verdict": verdict,
        "ts": "2026-05-09T00:00:00Z",
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_plan_pr_merge_refuses_when_record_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(PRMergeError, match="no merge-ready record"):
        plan_pr_merge(store, pr_number=999, head_sha="abc")


def test_plan_pr_merge_refuses_on_sha_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_merge_ready(store, pr=1, sha="aaaaaaaa")
    with pytest.raises(PRMergeError, match="head SHA mismatch"):
        plan_pr_merge(store, pr_number=1, head_sha="bbbbbbbb")


def test_plan_pr_merge_refuses_on_bad_verdict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_merge_ready(store, pr=1, sha="aaaaaaaa", verdict="needs_changes")
    with pytest.raises(PRMergeError, match="verdict is"):
        plan_pr_merge(store, pr_number=1, head_sha="aaaaaaaa")


def test_plan_pr_merge_renders_expected_command(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_merge_ready(store, pr=2788, sha="8f91dcf3")
    plan = plan_pr_merge(store, pr_number=2788, head_sha="8f91dcf3")
    assert plan.pr_number == 2788
    assert plan.head_sha == "8f91dcf3"
    assert plan.verdict == "merge_ready"

    rendered = render_merge_command(plan)
    # Exact shape required by the spec when no repo is supplied.
    assert rendered == "gh pr merge --squash --delete-branch --match-head-commit 8f91dcf3 2788"


def test_plan_pr_merge_pins_repo_when_supplied(tmp_path: Path) -> None:
    """Regression: gh must never fall back to whatever cwd's origin points at.

    Without ``--repo`` an operator running the merge from inside a different
    checkout (for example the Forecastin clone) would target *that* repo.
    Pinning ``--repo`` keeps the merge bound to the configured target.
    """
    store = _store(tmp_path)
    _write_merge_ready(store, pr=2788, sha="8f91dcf3")
    plan = plan_pr_merge(
        store,
        pr_number=2788,
        head_sha="8f91dcf3",
        repo="Oscillate-JP/forecastin-harness",
    )
    rendered = render_merge_command(plan)
    assert (
        rendered
        == "gh pr merge --repo Oscillate-JP/forecastin-harness --squash --delete-branch "
        "--match-head-commit 8f91dcf3 2788"
    )
    # The argv list keeps --repo immediately after the subcommand for safety.
    assert plan.argv[:5] == ["gh", "pr", "merge", "--repo", "Oscillate-JP/forecastin-harness"]


def test_plan_pr_merge_accepts_approved_verdict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_merge_ready(store, pr=42, sha="cafebabe", verdict="approved")
    plan = plan_pr_merge(store, pr_number=42, head_sha="cafebabe")
    assert plan.verdict == "approved"

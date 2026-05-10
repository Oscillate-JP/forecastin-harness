"""Tests for the merge_evidence module.

Inputs are synthetic ``gh pr view --json`` payloads. No live gh
invocation, no live git checkout.
"""

from __future__ import annotations

import json
from pathlib import Path

from forecastin_harness.merge_evidence import (
    render_merge_evidence,
    verify_merge_payload,
    verify_reachable_from_main,
)


def _payload(*, state: str, merged_at: str | None, sha: str | None, number: int = 2796) -> str:
    return json.dumps({
        "number": number,
        "state": state,
        "mergedAt": merged_at,
        "mergeCommit": ({"oid": sha} if sha else None),
        "url": f"https://github.com/Oscillate-JP/Forecastin/pull/{number}",
    })


def test_merged_payload_passes() -> None:
    payload = _payload(state="MERGED", merged_at="2026-05-10T12:00:00Z", sha="abc123def456")
    e = verify_merge_payload(payload, expected_pr=2796)
    assert e.ok is True
    assert e.state == "MERGED"
    assert e.merge_commit_sha == "abc123def456"
    assert e.merged_at == "2026-05-10T12:00:00Z"


def test_merged_at_null_is_rejected() -> None:
    """The exact failure mode from 2026-05-09."""
    payload = _payload(state="MERGED", merged_at=None, sha="abc123")
    e = verify_merge_payload(payload, expected_pr=2796)
    assert e.ok is False
    assert "mergedAt" in e.reason


def test_merge_commit_null_is_rejected() -> None:
    payload = _payload(state="MERGED", merged_at="2026-05-10T12:00:00Z", sha=None)
    e = verify_merge_payload(payload, expected_pr=2796)
    assert e.ok is False
    assert "mergeCommit" in e.reason


def test_open_state_is_rejected() -> None:
    payload = _payload(state="OPEN", merged_at=None, sha=None)
    e = verify_merge_payload(payload, expected_pr=2796)
    assert e.ok is False
    assert "OPEN" in e.reason


def test_pr_number_mismatch_rejected() -> None:
    payload = _payload(state="MERGED", merged_at="2026-05-10T12:00:00Z", sha="abc", number=1234)
    e = verify_merge_payload(payload, expected_pr=2796)
    assert e.ok is False
    assert "PR number mismatch" in e.reason


def test_unparseable_payload_rejected() -> None:
    e = verify_merge_payload("not-json", expected_pr=1)
    assert e.ok is False
    assert "JSON" in e.reason


def test_banner_text_is_not_evidence() -> None:
    """The 'Merged — proceed' banner is not JSON; the parser refuses it."""
    e = verify_merge_payload("Merged — proceed", expected_pr=1)
    assert e.ok is False


def test_render_includes_verdict() -> None:
    payload = _payload(state="MERGED", merged_at="2026-05-10T12:00:00Z", sha="abc123def456")
    e = verify_merge_payload(payload, expected_pr=2796)
    text = render_merge_evidence(e)
    assert "verdict" in text
    assert "ok" in text


def test_render_with_reachability_failure_marks_fail() -> None:
    payload = _payload(state="MERGED", merged_at="2026-05-10T12:00:00Z", sha="abc")
    e = verify_merge_payload(payload, expected_pr=2796)
    text = render_merge_evidence(e, reachable_from_main=False)
    assert "FAIL" in text
    assert "reachable_origin_main = false" in text


def test_verify_reachable_uses_injected_runner(tmp_path: Path) -> None:
    """verify_reachable_from_main must call the injected git runner."""
    seen: list[list[str]] = []

    def fake_runner(argv):
        seen.append(list(argv))
        return 0  # ancestor

    ok = verify_reachable_from_main(
        tmp_path, "abc123def456", main_ref="origin/main", git_runner=fake_runner
    )
    assert ok is True
    assert seen == [["git", "-C", str(tmp_path), "merge-base", "--is-ancestor", "abc123def456", "origin/main"]]


def test_verify_reachable_returns_false_on_nonzero(tmp_path: Path) -> None:
    def fake_runner(argv):
        return 1
    ok = verify_reachable_from_main(tmp_path, "abc", main_ref="origin/main", git_runner=fake_runner)
    assert ok is False

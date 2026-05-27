"""Fail-closed PR gate tests.

These tests exercise the hardened ``run_pr_check`` evaluator added in
v0.3. They are entirely hermetic: a fake ``runner`` returns canned JSON
shaped like ``gh pr view`` output, and a fake ``which`` makes
``gh-available`` pass without consulting the real PATH. No live ``gh``
invocations, no network, no auth.

The gate's previous behaviour (audited in v0.2) silently treated
PENDING / IN_PROGRESS / SKIPPED / NEUTRAL checks as PASS, never pinned
``--repo``, and ignored ``isDraft`` / ``baseRefName`` /
``mergeStateStatus``. Each test below names the audit finding it locks
in place.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forecastin_harness.pr_gate import run_pr_check
from forecastin_harness.state import StateStore


# --- helpers ---------------------------------------------------------------


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    return store


def _good_payload(**overrides: Any) -> dict[str, Any]:
    """A baseline payload that, untouched, would produce merge_ready=True."""
    payload: dict[str, Any] = {
        "number": 1,
        "state": "OPEN",
        "headRefOid": "deadbeef",
        "reviewDecision": "APPROVED",
        "isDraft": False,
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [
            {"name": "ci", "conclusion": "SUCCESS", "status": "COMPLETED"},
        ],
        "comments": [],
        "reviews": [],
    }
    payload.update(overrides)
    return payload


class _FakeProc:
    """Mimics the subset of ``subprocess.CompletedProcess`` we use."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _runner_returning(payload: dict[str, Any], *, returncode: int = 0):
    """Build a runner closure that captures the argv it was called with."""
    captured: dict[str, Any] = {}

    def runner(argv, **kwargs):  # pragma: no cover - passthrough
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return _FakeProc(stdout=json.dumps(payload), returncode=returncode)

    runner.captured = captured  # type: ignore[attr-defined]
    return runner


def _which_gh(_: str) -> str:
    return "/usr/bin/gh"


# --- argv pinning ----------------------------------------------------------


def test_run_pr_check_passes_repo_flag_to_gh(tmp_path: Path) -> None:
    """When ``repo`` is supplied, the argv must pin ``-R <repo>``.

    Locks the audit finding that the gate could otherwise resolve the PR
    against whatever git origin the operator's cwd happened to point at.
    """
    runner = _runner_returning(_good_payload())
    run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        main_branch="main",
        runner=runner,
        which=_which_gh,
    )
    argv = runner.captured["argv"]  # type: ignore[attr-defined]
    assert argv[:5] == ["gh", "-R", "Owner/Demo", "pr", "view"]


def test_run_pr_check_omits_repo_when_unset_for_back_compat(tmp_path: Path) -> None:
    """Legacy callers (and existing tests) must keep their argv shape."""
    runner = _runner_returning(_good_payload())
    run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        runner=runner,
        which=_which_gh,
    )
    argv = runner.captured["argv"]  # type: ignore[attr-defined]
    assert argv[:3] == ["gh", "pr", "view"]
    # No -R or --repo anywhere when repo is not pinned.
    assert "-R" not in argv
    assert "--repo" not in argv


# --- Windows UTF-8 decode regression ---------------------------------------


def test_run_pr_check_decodes_gh_output_as_utf8(tmp_path: Path) -> None:
    """gh output must be decoded as UTF-8, not the platform default codec.

    Regression: on Windows ``text=True`` alone decodes with cp1252, which
    raises UnicodeDecodeError on the emoji / em-dash bytes that routinely
    appear in PR bodies and CodeRabbit comments (e.g. "🎉", "→"). The reader
    thread then dies, ``proc.stdout`` becomes None, and the gate fails with an
    opaque ``TypeError`` at ``json.loads`` instead of evaluating the PR. The
    runner must be invoked with ``encoding="utf-8"`` and ``errors="replace"``.
    """
    runner = _runner_returning(_good_payload(body="ship it 🎉 — done"))
    run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        runner=runner,
        which=_which_gh,
    )
    kwargs = runner.captured["kwargs"]  # type: ignore[attr-defined]
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("errors") == "replace"


# --- required-checks fail-closed -------------------------------------------


def test_run_pr_check_rejects_pending_required_check(tmp_path: Path) -> None:
    """IN_PROGRESS used to silently pass; it must now fail with 'pending'."""
    payload = _good_payload(
        statusCheckRollup=[
            {"name": "ci", "conclusion": None, "status": "IN_PROGRESS"},
        ],
    )
    runner = _runner_returning(payload)
    result = run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        main_branch="main",
        runner=runner,
        which=_which_gh,
    )
    assert result.merge_ready is False
    failing = [o for o in result.outcomes if o.name == "required-checks"]
    assert failing and failing[0].result == "fail"
    assert "pending" in failing[0].detail.lower()
    assert "ci" in failing[0].detail


def test_run_pr_check_rejects_failed_required_check(tmp_path: Path) -> None:
    """Existing failure-bucket behaviour must be preserved."""
    payload = _good_payload(
        statusCheckRollup=[
            {"name": "ci", "conclusion": "FAILURE", "status": "COMPLETED"},
        ],
    )
    runner = _runner_returning(payload)
    result = run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        main_branch="main",
        runner=runner,
        which=_which_gh,
    )
    assert result.merge_ready is False
    failing = [o for o in result.outcomes if o.name == "required-checks"]
    assert failing and failing[0].result == "fail"
    assert "FAILURE" in failing[0].detail


def test_run_pr_check_rejects_unknown_bucket(tmp_path: Path) -> None:
    """Unrecognised buckets fail closed with detail 'unknown'."""
    payload = _good_payload(
        statusCheckRollup=[
            {"name": "ci", "conclusion": "WARP_DRIVE", "status": "COMPLETED"},
        ],
    )
    runner = _runner_returning(payload)
    result = run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        main_branch="main",
        runner=runner,
        which=_which_gh,
    )
    assert result.merge_ready is False
    failing = [o for o in result.outcomes if o.name == "required-checks"]
    assert failing and failing[0].result == "fail"
    assert "unknown" in failing[0].detail.lower()


# --- new outcomes ----------------------------------------------------------


def test_run_pr_check_rejects_draft_pr(tmp_path: Path) -> None:
    """isDraft=True must block merge readiness."""
    runner = _runner_returning(_good_payload(isDraft=True))
    result = run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        main_branch="main",
        runner=runner,
        which=_which_gh,
    )
    assert result.merge_ready is False
    draft = [o for o in result.outcomes if o.name == "pr-not-draft"]
    assert draft and draft[0].result == "fail"
    assert "DRAFT" in draft[0].detail.upper()


def test_run_pr_check_rejects_wrong_base_branch(tmp_path: Path) -> None:
    """A PR opened against a non-main branch must not be flagged ready."""
    runner = _runner_returning(_good_payload(baseRefName="develop"))
    result = run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        main_branch="main",
        runner=runner,
        which=_which_gh,
    )
    assert result.merge_ready is False
    base = [o for o in result.outcomes if o.name == "pr-base-branch"]
    assert base and base[0].result == "fail"
    assert "develop" in base[0].detail
    assert "main" in base[0].detail


def test_run_pr_check_skips_base_check_when_main_branch_unset(tmp_path: Path) -> None:
    """Without main_branch the base check is skipped (not failed)."""
    runner = _runner_returning(_good_payload(baseRefName="develop"))
    result = run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        # main_branch deliberately omitted.
        runner=runner,
        which=_which_gh,
    )
    base = [o for o in result.outcomes if o.name == "pr-base-branch"]
    assert base and base[0].result == "skipped"
    # The skipped outcome must not block merge_ready in isolation; the
    # rest of the payload is good, so the gate should pass.
    assert result.merge_ready is True


def test_run_pr_check_rejects_dirty_merge_state(tmp_path: Path) -> None:
    """DIRTY mergeStateStatus blocks merge-ready."""
    runner = _runner_returning(_good_payload(mergeStateStatus="DIRTY"))
    result = run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        main_branch="main",
        runner=runner,
        which=_which_gh,
    )
    assert result.merge_ready is False
    mergeable = [o for o in result.outcomes if o.name == "pr-mergeable"]
    assert mergeable and mergeable[0].result == "fail"
    assert "DIRTY" in mergeable[0].detail


def test_run_pr_check_accepts_clean_merge_state(tmp_path: Path) -> None:
    """The all-green baseline payload must produce merge_ready=True."""
    runner = _runner_returning(_good_payload())
    result = run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        main_branch="main",
        runner=runner,
        which=_which_gh,
    )
    assert result.merge_ready is True
    mergeable = [o for o in result.outcomes if o.name == "pr-mergeable"]
    assert mergeable and mergeable[0].result == "pass"
    assert "CLEAN" in mergeable[0].detail


def test_run_pr_check_accepts_unstable_merge_state_with_note(tmp_path: Path) -> None:
    """UNSTABLE passes pr-mergeable but the detail records the actual state."""
    runner = _runner_returning(_good_payload(mergeStateStatus="UNSTABLE"))
    result = run_pr_check(
        _store(tmp_path),
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        main_branch="main",
        runner=runner,
        which=_which_gh,
    )
    mergeable = [o for o in result.outcomes if o.name == "pr-mergeable"]
    assert mergeable and mergeable[0].result == "pass"
    assert "UNSTABLE" in mergeable[0].detail
    # All required checks were SUCCESS so the gate should still be ready.
    assert result.merge_ready is True


# --- merge-ready persistence ----------------------------------------------


def test_run_pr_check_emits_merge_ready_with_extra_fields(tmp_path: Path) -> None:
    """The merge_ready record must carry forward the new audit fields."""
    store = _store(tmp_path)
    runner = _runner_returning(_good_payload())
    result = run_pr_check(
        store,
        pr_number=1,
        head_sha="deadbeef",
        repo="Owner/Demo",
        main_branch="main",
        runner=runner,
        which=_which_gh,
    )
    assert result.merge_ready is True
    record = json.loads(
        (store.pr_dir / "1.merge_ready.json").read_text(encoding="utf-8")
    )
    # The downstream merge planner cross-checks every one of these fields.
    assert record["mergeStateStatus"] == "CLEAN"
    assert record["isDraft"] is False
    assert record["baseRefName"] == "main"
    assert record["repo"] == "Owner/Demo"
    # Sanity: the planner-required fields are still present.
    assert record["verdict"] == "merge_ready"
    assert record["head_sha"] == "deadbeef"

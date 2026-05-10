"""Merge evidence verifier — never accept text as proof of merge.

Background
==========

The 2026-05-09 drain treated GitHub UI text ("Merged — proceed") as
evidence of merge. ``gh pr view`` later returned ``state=OPEN`` and
``mergedAt=null``. The harness now requires structured evidence
before any post-merge action (Linear close-out, branch delete, lane
retire) is allowed.

Design
------

* :func:`verify_merge_payload` is a pure parser. Tests inject the
  exact JSON ``gh pr view`` produces; the function returns a
  :class:`MergeEvidence` verdict.
* :func:`verify_reachable_from_main` is a thin wrapper around
  ``git merge-base --is-ancestor``. It is documented but not run from
  this module's tests because the test fixtures don't contain a real
  git repo.
* The CLI subcommand ``preflight merge-evidence`` calls both.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class MergeEvidence:
    """Structured merge verdict.

    ``ok`` is True only when:

    * the parsed payload reports ``state == "MERGED"``,
    * ``mergedAt`` is a non-empty string,
    * ``mergeCommit.oid`` is a non-empty SHA.
    """

    pr: int
    state: str
    merged_at: str | None
    merge_commit_sha: str | None
    url: str | None
    ok: bool
    reason: str


def verify_merge_payload(payload: str | bytes | Mapping[str, Any], *, expected_pr: int | None = None) -> MergeEvidence:
    """Parse a ``gh pr view --json …`` payload into :class:`MergeEvidence`.

    The parser refuses to derive a verdict from anything other than
    the canonical JSON fields. Free-text fields, banner messages, and
    UI affordances are ignored — by construction, the only inputs are
    ``state``, ``mergedAt``, ``mergeCommit.oid``, and ``url``.
    """
    if isinstance(payload, (str, bytes)):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            return MergeEvidence(
                pr=expected_pr or 0,
                state="UNKNOWN",
                merged_at=None,
                merge_commit_sha=None,
                url=None,
                ok=False,
                reason=f"could not parse JSON: {exc}",
            )
    else:
        data = payload
    if not isinstance(data, Mapping):
        return MergeEvidence(
            pr=expected_pr or 0,
            state="UNKNOWN",
            merged_at=None,
            merge_commit_sha=None,
            url=None,
            ok=False,
            reason=f"payload root is not a JSON object: {type(data).__name__}",
        )
    pr_number = data.get("number")
    if not isinstance(pr_number, int):
        pr_number = expected_pr or 0
    state = str(data.get("state") or "UNKNOWN").upper()
    merged_at_raw = data.get("mergedAt")
    merged_at = str(merged_at_raw) if isinstance(merged_at_raw, str) and merged_at_raw else None
    merge_commit = data.get("mergeCommit")
    merge_commit_sha: str | None = None
    if isinstance(merge_commit, Mapping):
        oid = merge_commit.get("oid")
        if isinstance(oid, str) and oid:
            merge_commit_sha = oid
    url_raw = data.get("url")
    url = str(url_raw) if isinstance(url_raw, str) and url_raw else None

    if state != "MERGED":
        return MergeEvidence(
            pr=pr_number,
            state=state,
            merged_at=merged_at,
            merge_commit_sha=merge_commit_sha,
            url=url,
            ok=False,
            reason=f"state is {state!r}, expected 'MERGED'",
        )
    if not merged_at:
        return MergeEvidence(
            pr=pr_number,
            state=state,
            merged_at=merged_at,
            merge_commit_sha=merge_commit_sha,
            url=url,
            ok=False,
            reason="mergedAt is missing or empty",
        )
    if not merge_commit_sha:
        return MergeEvidence(
            pr=pr_number,
            state=state,
            merged_at=merged_at,
            merge_commit_sha=merge_commit_sha,
            url=url,
            ok=False,
            reason="mergeCommit.oid is missing or empty",
        )

    if expected_pr is not None and pr_number and pr_number != expected_pr:
        return MergeEvidence(
            pr=pr_number,
            state=state,
            merged_at=merged_at,
            merge_commit_sha=merge_commit_sha,
            url=url,
            ok=False,
            reason=f"PR number mismatch: payload says {pr_number}, expected {expected_pr}",
        )

    return MergeEvidence(
        pr=pr_number,
        state=state,
        merged_at=merged_at,
        merge_commit_sha=merge_commit_sha,
        url=url,
        ok=True,
        reason="state=MERGED with mergedAt and mergeCommit.oid present",
    )


def verify_reachable_from_main(
    repo_root: Path,
    commit_sha: str,
    *,
    main_ref: str = "origin/main",
    git_runner: "GitRunner | None" = None,
) -> bool:
    """Return True when ``commit_sha`` is reachable from ``main_ref``.

    Uses ``git merge-base --is-ancestor <commit_sha> <main_ref>``. The
    ``git_runner`` injection is for tests; the default uses
    ``subprocess.run`` with ``shell=False``.
    """
    runner = git_runner or _default_git_runner
    rc = runner(["git", "-C", str(repo_root), "merge-base", "--is-ancestor", commit_sha, main_ref])
    return rc == 0


def _default_git_runner(argv: list[str]) -> int:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return int(result.returncode)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return 1


# Type alias for the runner so consumers can be precise.
from typing import Callable, Sequence  # noqa: E402

GitRunner = Callable[[Sequence[str]], int]


def render_merge_evidence(evidence: MergeEvidence, *, reachable_from_main: bool | None = None) -> str:
    """Human-readable rendering used by the CLI."""
    lines: list[str] = [
        f"pr               = #{evidence.pr}",
        f"state            = {evidence.state}",
        f"mergedAt         = {evidence.merged_at!r}",
        f"mergeCommit.oid  = {evidence.merge_commit_sha!r}",
        f"url              = {evidence.url!r}",
        f"reason           = {evidence.reason}",
    ]
    if reachable_from_main is not None:
        lines.append(f"reachable_origin_main = {str(reachable_from_main).lower()}")
    overall = "ok" if evidence.ok and (reachable_from_main is None or reachable_from_main) else "FAIL"
    lines.append(f"verdict          = {overall}")
    return "\n".join(lines)

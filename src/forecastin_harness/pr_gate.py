"""PR review gate.

The gate validates a PR against a set of preconditions and emits a
``merge_ready.json`` record only when all conditions are satisfied. The
harness *never* merges; the artefact is consumed by the operator.

Preconditions, by default:

* ``head_sha`` from the operator matches the PR's actual head SHA.
* PR state is OPEN.
* PR is not a DRAFT.
* PR base branch matches the configured ``main_branch`` (when supplied).
* PR review decision is APPROVED.
* Every check in ``statusCheckRollup`` is in a *passing* terminal state.
  Pending / queued / unrecognised buckets fail closed — see
  :func:`_evaluate_required_checks` for the full table.
* ``mergeStateStatus`` is one of ``CLEAN`` / ``HAS_HOOKS`` / ``UNSTABLE``.
* No unresolved CodeRabbit critical / security / correctness comment.

GitHub CLI (``gh``) availability is detected at runtime. If ``gh`` is missing,
the gate reports the missing dependency and exits without contacting GitHub.
This keeps local validation hermetic.

All ``gh`` invocations pin ``--repo`` (via :func:`repo_pinned_argv`) when the
caller supplies the configured target. This prevents the gate from silently
targeting whatever git origin the *current* working directory happens to
point at — a real risk when operators run the harness from a different
checkout (for example, the Forecastin product repo).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal

from .state import StateStore, utcnow_iso

CheckResult = Literal["pass", "fail", "missing-tool", "skipped"]


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    result: CheckResult
    detail: str = ""


@dataclass
class PRGateResult:
    pr_number: int
    expected_head_sha: str
    observed_head_sha: str | None
    state: str | None
    review_decision: str | None
    is_draft: bool | None = None
    base_ref_name: str | None = None
    merge_state_status: str | None = None
    repo: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    outcomes: list[CheckOutcome] = field(default_factory=list)
    merge_ready: bool = False
    notes: list[str] = field(default_factory=list)
    ts: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> dict[str, Any]:
        # Convert nested dataclasses to plain dicts manually because asdict
        # mishandles tuples nested in dataclass lists when used elsewhere; here
        # the structure is shallow and asdict is fine.
        return {
            "pr_number": self.pr_number,
            "expected_head_sha": self.expected_head_sha,
            "observed_head_sha": self.observed_head_sha,
            "state": self.state,
            "review_decision": self.review_decision,
            "is_draft": self.is_draft,
            "base_ref_name": self.base_ref_name,
            "merge_state_status": self.merge_state_status,
            "repo": self.repo,
            "checks": self.checks,
            "outcomes": [asdict(o) for o in self.outcomes],
            "merge_ready": self.merge_ready,
            "notes": self.notes,
            "ts": self.ts,
        }


def gh_available(which=shutil.which) -> bool:
    """Return True iff a ``gh`` binary is on PATH."""
    return which("gh") is not None


def repo_pinned_argv(repo: str | None, base: list[str]) -> list[str]:
    """Insert ``-R <repo>`` immediately after the ``gh`` binary in ``base``.

    The harness must never let ``gh`` discover its target repo from the
    *current* working directory's git origin. Operators routinely invoke the
    harness from inside another checkout (the Forecastin product repo, a
    sibling worktree, a tooling clone), and an unpinned ``gh pr view 123``
    would happily resolve PR 123 in *that* origin instead of the configured
    ``Oscillate-JP/forecastin-harness`` target. The result is silently wrong
    answers — the gate would attest to a PR that does not even exist in the
    intended repo.

    Centralising the insertion here means every present and future ``gh``
    call in the gate inherits the same trust property. ``-R`` is preferred
    over ``--repo`` only because it is the short form ``gh`` documents as
    repository-pinning; both are accepted by the CLI.

    When ``repo`` is ``None`` the original argv is returned unchanged so
    legacy callers and dry-run renderings still work.
    """
    if not repo:
        return list(base)
    if not base or base[0] != "gh":
        # Defensive: refuse to mangle a non-gh argv. A bare repo pin is
        # meaningless without the binary, and silently inserting ``-R``
        # mid-command would corrupt the call.
        return list(base)
    return [base[0], "-R", repo, *base[1:]]


# ---- CodeRabbit detection -------------------------------------------------

_CODERABBIT_LOGINS = {"coderabbitai", "coderabbitai[bot]"}

# Severe-language patterns the harness treats as blocking. These are
# intentionally narrow: vague "consider…" comments are not blockers; the
# words below are the ones CodeRabbit uses for its severe verdicts.
_SEVERE_PATTERNS = (
    re.compile(r"\b(critical|severity:\s*critical)\b", re.IGNORECASE),
    re.compile(r"\b(security|sec\s*risk|vulnerab\w*)\b", re.IGNORECASE),
    re.compile(r"\b(correctness|incorrect|race\s*condition|data\s*loss)\b", re.IGNORECASE),
)

# Phrases that mark a comment as resolved by the operator.
_RESOLUTION_PATTERNS = (
    re.compile(r"\bresolved\b", re.IGNORECASE),
    re.compile(r"\bfixed\b", re.IGNORECASE),
    re.compile(r"\baddressed\b", re.IGNORECASE),
    re.compile(r"\bnot\s*applicable\b", re.IGNORECASE),
    re.compile(r"\bwon'?t\s*fix\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class CodeRabbitFinding:
    comment_id: str
    body_excerpt: str
    severe_terms: tuple[str, ...]
    resolved: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "body_excerpt": self.body_excerpt,
            "severe_terms": list(self.severe_terms),
            "resolved": self.resolved,
        }


def parse_coderabbit_findings(payload: dict[str, Any]) -> list[CodeRabbitFinding]:
    """Extract CodeRabbit severe findings from a ``gh pr view`` payload.

    Accepts the JSON shape that ``gh pr view --json comments,reviews`` emits:
    ``comments`` is a list of {author:{login}, body, isMinimized, ...} and
    ``reviews`` is similar with ``state`` (APPROVED, COMMENTED, …) and ``body``.

    A finding is "severe" when its body matches any of
    :data:`_SEVERE_PATTERNS`. It is "resolved" when its body matches any of
    :data:`_RESOLUTION_PATTERNS` *or* the comment is marked
    ``isMinimized=True``. Resolved findings do not block merge.
    """
    findings: list[CodeRabbitFinding] = []
    for source in ("comments", "reviews"):
        for entry in payload.get(source) or []:
            login = ((entry.get("author") or {}).get("login") or "").lower()
            if login not in _CODERABBIT_LOGINS:
                continue
            body = entry.get("body") or ""
            severe = tuple(p.pattern for p in _SEVERE_PATTERNS if p.search(body))
            if not severe:
                continue
            resolved = bool(entry.get("isMinimized")) or any(
                p.search(body) for p in _RESOLUTION_PATTERNS
            )
            findings.append(
                CodeRabbitFinding(
                    comment_id=str(entry.get("id") or entry.get("databaseId") or ""),
                    body_excerpt=body[:200],
                    severe_terms=severe,
                    resolved=resolved,
                )
            )
    return findings


def coderabbit_outcome(payload: dict[str, Any]) -> CheckOutcome:
    """Convert a parsed payload into a :class:`CheckOutcome`."""
    findings = parse_coderabbit_findings(payload)
    unresolved = [f for f in findings if not f.resolved]
    if unresolved:
        return CheckOutcome(
            "coderabbit-clean",
            "fail",
            f"{len(unresolved)} unresolved severe CodeRabbit finding(s)",
        )
    return CheckOutcome("coderabbit-clean", "pass")


# ---- merge plan -----------------------------------------------------------


class PRMergeError(RuntimeError):
    """Raised when the merge plan cannot be constructed."""


@dataclass(frozen=True)
class PRMergePlan:
    pr_number: int
    head_sha: str
    verdict: str
    merge_ready_ts: str
    argv: list[str]


def render_merge_command(plan: PRMergePlan) -> str:
    """Operator-facing single-line rendering of the merge command."""
    return " ".join(plan.argv)


def plan_pr_merge(
    store: StateStore,
    *,
    pr_number: int,
    head_sha: str,
    repo: str | None = None,
) -> PRMergePlan:
    """Build a merge plan from a previously-recorded ``merge_ready.json``.

    Refuses if the file is missing, the recorded SHA disagrees with the
    operator-supplied SHA, or the verdict is anything other than
    ``approved``/``merge_ready``. The harness never merges; this builds the
    artefact the CLI prints (and optionally runs) on the operator's
    explicit confirmation.

    ``repo`` is the GitHub ``<owner>/<name>`` identifier from the adapter
    config. When provided, the rendered ``gh`` argv pins ``--repo`` so the
    command can never default to whatever git origin the *current working
    directory* happens to point at. Omitting it is allowed for tests that
    only inspect the rendered command, but the CLI always passes it.
    """
    path = store.pr_dir / f"{pr_number}.merge_ready.json"
    if not path.exists():
        raise PRMergeError(f"no merge-ready record at {path}; run 'pr check' first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    recorded_sha = str(payload.get("head_sha") or "")
    if recorded_sha != head_sha:
        raise PRMergeError(
            f"head SHA mismatch: recorded={recorded_sha!r}, supplied={head_sha!r}"
        )
    verdict = str(payload.get("verdict") or "merge_ready")
    if verdict not in {"merge_ready", "approved"}:
        raise PRMergeError(f"verdict is {verdict!r}; expected 'merge_ready' or 'approved'")

    argv = ["gh", "pr", "merge"]
    if repo:
        argv.extend(["--repo", repo])
    argv.extend(
        [
            "--squash",
            "--delete-branch",
            "--match-head-commit",
            head_sha,
            str(pr_number),
        ]
    )
    return PRMergePlan(
        pr_number=pr_number,
        head_sha=head_sha,
        verdict=verdict,
        merge_ready_ts=str(payload.get("ts") or ""),
        argv=argv,
    )


def plan_pr_check(*, pr_number: int, head_sha: str) -> str:
    """Human-readable rendering of what a real PR check would do."""
    sha = head_sha or "(unset)"
    return "\n".join(
        [
            f"PR check plan: PR #{pr_number}",
            f"expected head SHA: {sha}",
            "",
            "Steps that would run (none execute in dry-run):",
            f"  1. gh -R <repo> pr view {pr_number} --json "
            "number,state,headRefOid,reviewDecision,statusCheckRollup,"
            "mergeStateStatus,isDraft,baseRefName,comments,reviews",
            "  2. compare returned headRefOid to the operator-provided head SHA",
            "  3. require state == OPEN",
            "  4. require isDraft is False (pr-not-draft)",
            "  5. require baseRefName == config.target.main_branch (pr-base-branch;"
            " skipped if main_branch unset)",
            "  6. require reviewDecision == APPROVED",
            "  7. require every statusCheckRollup entry to be in a passing"
            " terminal bucket; pending/queued/unrecognised buckets fail closed",
            "  8. require mergeStateStatus in {CLEAN, HAS_HOOKS, UNSTABLE}"
            " (pr-mergeable; UNSTABLE passes with a note)",
            "  9. require no unresolved CodeRabbit critical/security/correctness comment",
            "",
            "Result is written to <state>/pr/<pr_number>.json. If every step",
            "passes, also writes <state>/pr/<pr_number>.merge_ready.json.",
            "The harness still does NOT merge; merge is operator-only.",
        ]
    )


def run_pr_check(
    store: StateStore,
    *,
    pr_number: int,
    head_sha: str,
    repo: str | None = None,
    main_branch: str | None = None,
    runner=subprocess.run,
    which=shutil.which,
) -> PRGateResult:
    """Run a real PR check using ``gh``. Falls back gracefully if ``gh`` is missing.

    ``repo`` pins ``gh`` to the configured target (see
    :func:`repo_pinned_argv` for the trust rationale). When omitted, the
    legacy unpinned argv is used so existing tests still drive the function.
    Production callers (the CLI) always pass ``config.target.repo``.

    ``main_branch`` enables the ``pr-base-branch`` outcome. When ``None``,
    that outcome is recorded as ``skipped`` and is excluded from the
    merge-ready conjunction (matches v0.2 behaviour).
    """
    result = PRGateResult(
        pr_number=pr_number,
        expected_head_sha=head_sha,
        observed_head_sha=None,
        state=None,
        review_decision=None,
        repo=repo,
    )

    if not gh_available(which):
        result.outcomes.append(
            CheckOutcome(
                "gh-available",
                "missing-tool",
                "GitHub CLI 'gh' not found on PATH; PR gate cannot contact GitHub.",
            )
        )
        result.notes.append("install gh and retry; harness does not require gh for local validation.")
        _persist(result, store)
        return result

    base_argv = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--json",
        # Order is documentation; gh ignores ordering. ``isDraft`` and
        # ``baseRefName`` are required for the new pr-not-draft and
        # pr-base-branch outcomes; ``mergeStateStatus`` was already
        # requested but never evaluated prior to this hardening pass.
        "number,state,headRefOid,reviewDecision,statusCheckRollup,"
        "mergeStateStatus,isDraft,baseRefName,comments,reviews",
    ]
    argv = repo_pinned_argv(repo, base_argv)
    try:
        proc = runner(
            argv,
            check=False,
            capture_output=True,
            text=True,
            # Force UTF-8 decoding of gh's output. ``text=True`` alone uses the
            # platform default codec (cp1252 on Windows), which raises
            # UnicodeDecodeError on the emoji/em-dash bytes that routinely
            # appear in PR bodies and CodeRabbit comments (e.g. "🎉", "→").
            # When that happens the reader thread dies and ``proc.stdout`` is
            # ``None``, surfacing as an opaque TypeError at ``json.loads``.
            # ``errors="replace"`` keeps a stray undecodable byte from aborting
            # the whole gate — the JSON fields we read are ASCII.
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        result.outcomes.append(CheckOutcome("gh-invoke", "fail", f"failed to invoke gh: {exc}"))
        _persist(result, store)
        return result

    if proc.returncode != 0:
        result.outcomes.append(
            CheckOutcome(
                "gh-pr-view",
                "fail",
                f"gh exited {proc.returncode}: {proc.stderr.strip()[:400]}",
            )
        )
        _persist(result, store)
        return result

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        result.outcomes.append(CheckOutcome("gh-parse", "fail", f"could not parse gh output: {exc}"))
        _persist(result, store)
        return result

    result.observed_head_sha = payload.get("headRefOid")
    result.state = payload.get("state")
    result.review_decision = payload.get("reviewDecision")
    result.is_draft = payload.get("isDraft")
    result.base_ref_name = payload.get("baseRefName")
    result.merge_state_status = payload.get("mergeStateStatus")
    result.checks = list(payload.get("statusCheckRollup") or [])

    # Step-by-step gate evaluation. The order is meaningful for human
    # readers reviewing the persisted JSON: cheap identity checks first,
    # then approval, then CI surface, then mergeability, then bot review.
    if result.observed_head_sha != head_sha:
        result.outcomes.append(
            CheckOutcome(
                "head-sha-match",
                "fail",
                f"expected {head_sha}, observed {result.observed_head_sha}",
            )
        )
    else:
        result.outcomes.append(CheckOutcome("head-sha-match", "pass"))

    if result.state != "OPEN":
        result.outcomes.append(
            CheckOutcome("pr-open", "fail", f"PR state is {result.state}, expected OPEN")
        )
    else:
        result.outcomes.append(CheckOutcome("pr-open", "pass"))

    # ``isDraft`` is an explicit field from gh; treat anything other than
    # the literal False as a failure. ``None`` (missing) is suspicious
    # because the projection requested the field; fail closed.
    if result.is_draft is False:
        result.outcomes.append(CheckOutcome("pr-not-draft", "pass"))
    elif result.is_draft is True:
        result.outcomes.append(
            CheckOutcome("pr-not-draft", "fail", "PR is in DRAFT state")
        )
    else:
        result.outcomes.append(
            CheckOutcome(
                "pr-not-draft",
                "fail",
                f"isDraft missing or non-boolean: {result.is_draft!r}",
            )
        )

    # Base-branch check is opt-in: the CLI passes the configured main
    # branch, but legacy callers (and the current dry-run renderer) may
    # omit it. Skipped outcomes do not block merge_ready.
    if main_branch is None:
        result.outcomes.append(
            CheckOutcome(
                "pr-base-branch",
                "skipped",
                "main_branch not supplied; base-branch check skipped",
            )
        )
    elif result.base_ref_name == main_branch:
        result.outcomes.append(CheckOutcome("pr-base-branch", "pass"))
    else:
        result.outcomes.append(
            CheckOutcome(
                "pr-base-branch",
                "fail",
                f"baseRefName={result.base_ref_name!r}, expected {main_branch!r}",
            )
        )

    if result.review_decision != "APPROVED":
        result.outcomes.append(
            CheckOutcome(
                "review-approved",
                "fail",
                f"reviewDecision is {result.review_decision!r}, expected 'APPROVED'",
            )
        )
    else:
        result.outcomes.append(CheckOutcome("review-approved", "pass"))

    checks_outcome, checks_note = _evaluate_required_checks(result.checks)
    result.outcomes.append(checks_outcome)
    if checks_note:
        result.notes.append(checks_note)

    result.outcomes.append(_evaluate_merge_state(result.merge_state_status))

    result.outcomes.append(coderabbit_outcome(payload))

    # ``skipped`` outcomes neither block nor confirm merge readiness; only
    # ``pass`` outcomes count as positive evidence. This matches the
    # contract: pr-base-branch can be skipped without blocking, but no
    # other outcome may be missing.
    result.merge_ready = all(o.result in {"pass", "skipped"} for o in result.outcomes) and any(
        o.result == "pass" for o in result.outcomes
    )
    _persist(result, store)
    if result.merge_ready:
        _emit_merge_ready(result, store)
    return result


# Buckets that count as a *passing* terminal state. SUCCESS is the only
# GitHub conclusion that we treat as green — NEUTRAL and SKIPPED are
# ambiguous because a non-required check can legitimately be skipped, but
# the rollup does not tell us whether a check is required, so we cannot
# safely tolerate them. Fail-closed.
_CHECK_PASS_BUCKETS = frozenset({"SUCCESS"})

# Explicitly bad terminal states. These always fail. ``ERROR`` is the
# legacy StatusContext failure state (CheckRuns use ``FAILURE``).
_CHECK_FAIL_BUCKETS = frozenset(
    {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
)

# Non-terminal states. The previous implementation treated these as PASS,
# which is exactly the bug the audit caught: a PR with checks still
# IN_PROGRESS could pass the gate. They now fail with detail "pending".
# ``EXPECTED`` is the legacy StatusContext pending state.
_CHECK_PENDING_BUCKETS = frozenset(
    {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}
)

# Ambiguous in this projection — required-vs-optional is not knowable
# from the rollup alone, so we fail closed and let the operator confirm.
_CHECK_AMBIGUOUS_BUCKETS = frozenset({"NEUTRAL", "SKIPPED"})


def _bucket_for(check: dict[str, Any]) -> str:
    """Return the upper-cased bucket for a check, preferring conclusion.

    GitHub populates ``conclusion`` once a check reaches a terminal state
    and leaves it ``None`` while the check is still running; ``status``
    holds the lifecycle phase (``IN_PROGRESS`` etc.). Conclusion wins when
    present so a fast-failing check is reported as ``FAILURE`` rather than
    ``COMPLETED``.

    The rollup mixes two node shapes. CheckRun nodes carry
    ``conclusion``/``status``; legacy StatusContext nodes (commit statuses
    posted by CodeRabbit and many external CIs) carry neither — only
    ``state`` (SUCCESS / FAILURE / ERROR / PENDING / EXPECTED). Without the
    ``state`` fallback every status-based check reads as ``<empty>`` and the
    gate fails closed on it even when the status is green.
    """
    raw = check.get("conclusion")
    if not raw:
        raw = check.get("status")
    if not raw:
        raw = check.get("state")
    return str(raw or "").upper()


def _evaluate_required_checks(
    checks: Iterable[dict[str, Any]],
) -> tuple[CheckOutcome, str | None]:
    """Fail-closed evaluator for ``statusCheckRollup``.

    Returns a ``(CheckOutcome, optional note)`` pair. The note, when
    present, is appended to :attr:`PRGateResult.notes` to flag operator
    follow-up — for example, an empty rollup is treated as a *pass* so a
    repo without required checks is not permanently blocked, but the
    operator should still know that no CI evidence exists.
    """
    seen = list(checks)
    if not seen:
        return (
            CheckOutcome(
                "required-checks",
                "pass",
                "no required checks reported",
            ),
            "statusCheckRollup is empty; no CI evidence available — verify by hand.",
        )

    for c in seen:
        bucket = _bucket_for(c)
        name = c.get("name") or c.get("context") or "?"
        if bucket in _CHECK_PASS_BUCKETS:
            continue
        if bucket in _CHECK_FAIL_BUCKETS:
            return (
                CheckOutcome(
                    "required-checks",
                    "fail",
                    f"check failing: {name}={bucket}",
                ),
                None,
            )
        if bucket in _CHECK_PENDING_BUCKETS:
            return (
                CheckOutcome(
                    "required-checks",
                    "fail",
                    f"check pending: {name}={bucket} (pending)",
                ),
                None,
            )
        if bucket in _CHECK_AMBIGUOUS_BUCKETS:
            return (
                CheckOutcome(
                    "required-checks",
                    "fail",
                    f"check ambiguous: {name}={bucket} (ambiguous-bucket)",
                ),
                None,
            )
        return (
            CheckOutcome(
                "required-checks",
                "fail",
                f"check unknown: {name}={bucket or '<empty>'} (unknown)",
            ),
            None,
        )

    return CheckOutcome("required-checks", "pass"), None


# Buckets that are mergeable from GitHub's perspective. UNSTABLE means
# non-required checks are red; we tolerate it because every required check
# was already independently evaluated above. The full taxonomy is at
# https://docs.github.com/en/graphql/reference/enums#mergestatestatus.
_MERGE_STATE_OK = frozenset({"CLEAN", "HAS_HOOKS"})
_MERGE_STATE_OK_WITH_NOTE = frozenset({"UNSTABLE"})


def _evaluate_merge_state(status: str | None) -> CheckOutcome:
    """Translate GitHub's ``mergeStateStatus`` into a gate outcome.

    Fail-closed: any value not on the explicit allow-list is a failure.
    The detail always includes the raw status so the persisted JSON makes
    the reason for blocking obvious to the operator.
    """
    raw = (status or "").upper()
    if raw in _MERGE_STATE_OK:
        return CheckOutcome("pr-mergeable", "pass", f"mergeStateStatus={raw}")
    if raw in _MERGE_STATE_OK_WITH_NOTE:
        return CheckOutcome(
            "pr-mergeable",
            "pass",
            f"mergeStateStatus={raw} (non-required checks failing; required checks evaluated separately)",
        )
    if not raw:
        return CheckOutcome(
            "pr-mergeable",
            "fail",
            "mergeStateStatus missing from gh response",
        )
    return CheckOutcome(
        "pr-mergeable",
        "fail",
        f"mergeStateStatus={raw} (not in allow-list {sorted(_MERGE_STATE_OK | _MERGE_STATE_OK_WITH_NOTE)})",
    )


def _persist(result: PRGateResult, store: StateStore) -> None:
    store.pr_dir.mkdir(parents=True, exist_ok=True)
    path = store.pr_dir / f"{result.pr_number}.json"
    path.write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    store.append_event(
        {
            "kind": "pr.check",
            "pr": result.pr_number,
            "merge_ready": result.merge_ready,
            "outcomes": [o.result for o in result.outcomes],
        }
    )


def _emit_merge_ready(result: PRGateResult, store: StateStore) -> None:
    """Persist the operator-facing merge-ready record.

    The downstream ``pr merge`` planner cross-checks the recorded SHA. We
    additionally record ``mergeStateStatus``, ``isDraft``, ``baseRefName``
    and ``repo`` so that planner can later confirm — even if hours have
    elapsed — that the PR's mergeability surface and base-branch contract
    haven't drifted since the gate ran. Cross-checking is the planner's
    job; this function just preserves the evidence.
    """
    suggestion_parts = [f"gh pr merge"]
    if result.repo:
        suggestion_parts.append(f"--repo {result.repo}")
    suggestion_parts.append(
        f"--squash --delete-branch --match-head-commit {result.observed_head_sha} {result.pr_number}"
    )
    payload: dict[str, Any] = {
        "pr_number": result.pr_number,
        "head_sha": result.observed_head_sha,
        "verdict": "merge_ready",
        "ts": result.ts,
        "mergeStateStatus": result.merge_state_status,
        "isDraft": result.is_draft,
        "baseRefName": result.base_ref_name,
        "repo": result.repo,
        "merge_command_suggestion": " ".join(suggestion_parts),
        "note": "execution requires explicit operator action; harness does not merge.",
    }
    path = store.pr_dir / f"{result.pr_number}.merge_ready.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    store.append_event({"kind": "pr.merge_ready", "pr": result.pr_number})

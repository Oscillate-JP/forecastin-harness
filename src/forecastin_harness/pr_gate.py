"""PR review gate.

The gate validates a PR against a set of preconditions and emits a
``merge_ready.json`` record only when all conditions are satisfied. The
harness *never* merges; the artefact is consumed by the operator.

Preconditions, by default:

* ``head_sha`` from the operator matches the PR's actual head SHA.
* PR state is OPEN.
* PR review decision is APPROVED.
* No required check is FAILURE / CANCELLED / TIMED_OUT.
* No unresolved CodeRabbit critical / security / correctness comment.

GitHub CLI (``gh``) availability is detected at runtime. If ``gh`` is missing,
the gate reports the missing dependency and exits without contacting GitHub.
This keeps local validation hermetic.
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
            "checks": self.checks,
            "outcomes": [asdict(o) for o in self.outcomes],
            "merge_ready": self.merge_ready,
            "notes": self.notes,
            "ts": self.ts,
        }


def gh_available(which=shutil.which) -> bool:
    """Return True iff a ``gh`` binary is on PATH."""
    return which("gh") is not None


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
            f"  1. gh pr view {pr_number} --json number,state,headRefOid,reviewDecision,statusCheckRollup,comments,reviews",
            "  2. compare returned headRefOid to the operator-provided head SHA",
            "  3. require state == OPEN",
            "  4. require reviewDecision == APPROVED",
            "  5. require no required check in {FAILURE, CANCELLED, TIMED_OUT}",
            "  6. require no unresolved CodeRabbit critical/security/correctness comment",
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
    runner=subprocess.run,
    which=shutil.which,
) -> PRGateResult:
    """Run a real PR check using ``gh``. Falls back gracefully if ``gh`` is missing."""
    result = PRGateResult(
        pr_number=pr_number,
        expected_head_sha=head_sha,
        observed_head_sha=None,
        state=None,
        review_decision=None,
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

    try:
        proc = runner(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "number,state,headRefOid,reviewDecision,statusCheckRollup,mergeStateStatus,comments,reviews",
            ],
            check=False,
            capture_output=True,
            text=True,
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
    result.checks = list(payload.get("statusCheckRollup") or [])

    # Step-by-step gate evaluation.
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

    bad_check = _first_failing_required_check(result.checks)
    if bad_check is not None:
        result.outcomes.append(
            CheckOutcome("required-checks", "fail", f"check failing: {bad_check}")
        )
    else:
        result.outcomes.append(CheckOutcome("required-checks", "pass"))

    result.outcomes.append(coderabbit_outcome(payload))

    result.merge_ready = all(o.result == "pass" for o in result.outcomes)
    _persist(result, store)
    if result.merge_ready:
        _emit_merge_ready(result, store)
    return result


def _first_failing_required_check(checks: Iterable[dict[str, Any]]) -> str | None:
    for c in checks:
        bucket = (c.get("conclusion") or c.get("status") or "").upper()
        if bucket in {"FAILURE", "CANCELLED", "TIMED_OUT"}:
            return f"{c.get('name', '?')}={bucket}"
    return None


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
    payload: dict[str, Any] = {
        "pr_number": result.pr_number,
        "head_sha": result.observed_head_sha,
        "verdict": "merge_ready",
        "ts": result.ts,
        "merge_command_suggestion": (
            f"gh pr merge {result.pr_number} --squash --delete-branch "
            f"--match-head-commit {result.observed_head_sha}"
        ),
        "note": "execution requires explicit operator action; harness does not merge.",
    }
    path = store.pr_dir / f"{result.pr_number}.merge_ready.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    store.append_event({"kind": "pr.merge_ready", "pr": result.pr_number})

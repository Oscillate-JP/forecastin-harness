"""Tests for the CodeRabbit comment / review parser.

All inputs are static JSON fixtures shaped like ``gh pr view --json
comments,reviews`` output. No network. No live ``gh``. No auth required.
"""

from __future__ import annotations

from forecastin_harness.pr_gate import (
    CodeRabbitFinding,
    coderabbit_outcome,
    parse_coderabbit_findings,
)


def _payload(*, comments=None, reviews=None) -> dict:
    return {"comments": comments or [], "reviews": reviews or []}


def _cr(body: str, *, login: str = "coderabbitai", minimised: bool = False, _id: int = 1) -> dict:
    return {
        "id": _id,
        "author": {"login": login},
        "body": body,
        "isMinimized": minimised,
    }


def test_parse_no_comments_returns_empty() -> None:
    assert parse_coderabbit_findings(_payload()) == []


def test_parse_ignores_non_coderabbit_authors() -> None:
    payload = _payload(comments=[_cr("Critical security issue", login="reviewer-bob")])
    assert parse_coderabbit_findings(payload) == []


def test_parse_detects_critical_severity() -> None:
    payload = _payload(comments=[_cr("Severity: CRITICAL — drops auth check")])
    found = parse_coderabbit_findings(payload)
    assert len(found) == 1
    assert isinstance(found[0], CodeRabbitFinding)
    assert found[0].resolved is False


def test_parse_detects_security_keyword() -> None:
    payload = _payload(comments=[_cr("Potential security issue: SQL injection on /search")])
    found = parse_coderabbit_findings(payload)
    assert len(found) == 1


def test_parse_detects_correctness_keyword() -> None:
    payload = _payload(reviews=[_cr("Correctness: race condition between writers")])
    found = parse_coderabbit_findings(payload)
    assert len(found) == 1


def test_parse_marks_minimised_as_resolved() -> None:
    payload = _payload(comments=[_cr("Critical issue", minimised=True)])
    found = parse_coderabbit_findings(payload)
    assert len(found) == 1
    assert found[0].resolved is True


def test_parse_marks_resolved_via_text() -> None:
    payload = _payload(comments=[_cr("Critical: ... — resolved in commit abc123")])
    found = parse_coderabbit_findings(payload)
    assert len(found) == 1
    assert found[0].resolved is True


def test_parse_skips_vague_consider_comments() -> None:
    payload = _payload(comments=[_cr("Consider naming this variable better.")])
    assert parse_coderabbit_findings(payload) == []


def test_outcome_pass_when_no_findings() -> None:
    outcome = coderabbit_outcome(_payload())
    assert outcome.name == "coderabbit-clean"
    assert outcome.result == "pass"


def test_outcome_pass_when_all_findings_resolved() -> None:
    payload = _payload(
        comments=[
            _cr("Critical issue — fixed", _id=1),
            _cr("Security risk — addressed", _id=2),
        ]
    )
    assert coderabbit_outcome(payload).result == "pass"


def test_outcome_fail_when_unresolved_severe_present() -> None:
    payload = _payload(
        comments=[
            _cr("Critical issue — fixed", _id=1),
            _cr("Critical issue — unmitigated", _id=2),
        ]
    )
    outcome = coderabbit_outcome(payload)
    assert outcome.result == "fail"
    assert "unresolved severe" in outcome.detail


def test_parse_recognises_bot_login_variant() -> None:
    payload = _payload(comments=[_cr("Critical issue", login="coderabbitai[bot]")])
    found = parse_coderabbit_findings(payload)
    assert len(found) == 1

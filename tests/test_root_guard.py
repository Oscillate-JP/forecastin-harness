"""Tests for the root_guard module.

Every check uses fake paths so the suite is hermetic — the runner
doesn't have to be in any particular cwd, and CI doesn't accidentally
see a real ``J:/Forcastin`` checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forecastin_harness.root_guard import (
    DEFAULT_FORBIDDEN_ROOTS,
    check_root,
)


def test_cwd_equals_expected_returns_ok(tmp_path: Path) -> None:
    expected = tmp_path / "coordinator-main"
    expected.mkdir()
    result = check_root(expected, expected=expected, forbidden=())
    assert result.ok
    assert result.verdict == "ok"
    assert "ok" in result.message.lower()


def test_cwd_under_forbidden_returns_wrong_root(tmp_path: Path) -> None:
    forbidden = tmp_path / "Forcastin"
    forbidden.mkdir()
    expected = tmp_path / "coordinator-main"
    expected.mkdir()
    result = check_root(forbidden, expected=expected, forbidden=[forbidden])
    assert not result.ok
    assert result.verdict == "WRONG_ROOT"
    assert "WRONG_ROOT" in result.message
    assert str(expected.resolve()) in result.message


def test_cwd_descendant_of_forbidden_is_wrong_root(tmp_path: Path) -> None:
    forbidden = tmp_path / "Forcastin"
    nested = forbidden / "backend" / ".venv"
    nested.mkdir(parents=True)
    expected = tmp_path / "coordinator-main"
    expected.mkdir()
    result = check_root(nested, expected=expected, forbidden=[forbidden])
    assert not result.ok
    assert result.verdict == "WRONG_ROOT"


def test_cwd_neither_expected_nor_forbidden_is_wrong_root(tmp_path: Path) -> None:
    expected = tmp_path / "coordinator-main"
    expected.mkdir()
    other = tmp_path / "some-other-checkout"
    other.mkdir()
    result = check_root(other, expected=expected, forbidden=())
    assert not result.ok
    assert result.verdict == "WRONG_ROOT"
    assert str(expected.resolve()) in result.message


def test_default_forbidden_roots_include_legacy_checkout() -> None:
    # Documented invariant: the default forbidden list always includes
    # the legacy ``J:/Forcastin`` checkout so operators are protected
    # even if they forget to configure it.
    paths = {str(p) for p in DEFAULT_FORBIDDEN_ROOTS}
    assert any("Forcastin" in p for p in paths), (
        f"DEFAULT_FORBIDDEN_ROOTS must include the legacy J:/Forcastin "
        f"checkout. Got: {sorted(paths)}"
    )


def test_resolution_collapses_relative_components(tmp_path: Path) -> None:
    expected = tmp_path / "coordinator-main"
    expected.mkdir()
    relative_path = expected / "subdir" / ".."  # resolves to expected
    result = check_root(relative_path, expected=expected, forbidden=())
    assert result.ok, result.message


@pytest.mark.parametrize(
    "verdict,expected_ok",
    [
        ("ok", True),
        ("WRONG_ROOT", False),
    ],
)
def test_result_ok_property(tmp_path: Path, verdict: str, expected_ok: bool) -> None:
    """RootGuardResult.ok must mirror verdict == 'ok'."""
    from forecastin_harness.root_guard import RootGuardResult

    r = RootGuardResult(
        verdict=verdict,
        cwd_resolved=tmp_path,
        expected_resolved=tmp_path,
        forbidden_resolved=(),
        message="test",
    )
    assert r.ok is expected_ok

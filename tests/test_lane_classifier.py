"""Tests for the lane_classifier module.

The classifier is a pure function over a list of file paths so the
suite is hermetic.
"""

from __future__ import annotations

import pytest

from forecastin_harness.lane_classifier import (
    classify_changes,
    render_classification,
)


def test_docs_only_paths_classify_as_docs_only() -> None:
    paths = [
        "docs/ui2/UI2_PHASE_5_SPECS.md",
        "docs/_meta/documentation-inventory.json",
        "docs/_meta/documentation-inventory.md",
    ]
    result = classify_changes(paths)
    assert result.kind == "docs-only"
    assert result.is_docs_only is True
    assert result.requires_full_backend_pytest is False
    # Recipe must include the docs gate, not pytest.
    joined = " ".join(result.gate_recipe)
    assert "build_inventory" in joined
    assert "pytest" not in joined


def test_test_only_paths_classify_as_test_only() -> None:
    paths = [
        "backend/tests/api/v1/endpoints/test_calibration_quality_response.py",
    ]
    result = classify_changes(paths)
    assert result.kind == "test-only"
    assert result.requires_full_backend_pytest is False


def test_backend_code_classifies_as_backend_code() -> None:
    paths = ["backend/app/scripts/foo.py"]
    result = classify_changes(paths)
    assert result.kind == "backend-code"
    assert result.requires_full_backend_pytest is True


def test_runtime_code_classifies_as_runtime_code() -> None:
    paths = ["backend/app/api/v1/endpoints/calibration.py"]
    result = classify_changes(paths)
    assert result.kind == "runtime-code"
    assert result.requires_full_backend_pytest is True
    joined = " ".join(result.gate_recipe)
    assert "curl" in joined  # operator captures runtime evidence


def test_migration_classifies_as_migration() -> None:
    paths = ["backend/alembic/versions/20260306_140000_convert_to_halfvec_512.py"]
    result = classify_changes(paths)
    assert result.kind == "migration"
    assert result.requires_full_backend_pytest is True


def test_mixed_changeset_picks_most_restrictive() -> None:
    # docs + test + runtime → runtime wins per RESTRICTION_ORDER
    paths = [
        "docs/ui2/foo.md",
        "backend/tests/api/v1/endpoints/test_x.py",
        "backend/app/api/v1/endpoints/calibration.py",
    ]
    result = classify_changes(paths)
    assert result.kind == "runtime-code"


def test_empty_changeset_returns_empty_kind() -> None:
    result = classify_changes([])
    assert result.kind == "empty"
    assert result.requires_full_backend_pytest is False  # nothing to do, can't require


def test_unknown_path_falls_back_to_backend_code() -> None:
    # Conservative default: unknown paths must NOT be classified as
    # docs-only, otherwise an unfamiliar path could silently skip the
    # backend pytest gate.
    result = classify_changes(["weird/unknown/file.py"])
    assert result.kind == "backend-code"


def test_top_level_md_is_docs_only() -> None:
    result = classify_changes(["TOP_LEVEL_README.md"])
    assert result.kind == "docs-only"


def test_per_file_classification_exposed() -> None:
    paths = [
        "docs/ui2/foo.md",
        "backend/tests/test_x.py",
    ]
    result = classify_changes(paths)
    by_path = dict(result.per_file)
    assert by_path["docs/ui2/foo.md"] == "docs-only"
    assert by_path["backend/tests/test_x.py"] == "test-only"


def test_render_includes_kind_and_recipe() -> None:
    result = classify_changes(["docs/foo.md"])
    text = render_classification(result)
    assert "lane_kind = docs-only" in text
    assert "requires_full_backend_pytest = false" in text


@pytest.mark.parametrize(
    "path,expected",
    [
        ("backend/app/api/foo.py", "runtime-code"),
        ("backend/app/services/foo.py", "runtime-code"),
        ("backend/app/websocket/handler.py", "runtime-code"),
        ("backend/app/models/foo.py", "backend-code"),
        ("frontend/src/components/Foo.tsx", "frontend-code"),
        ("frontend/src/__tests__/Foo.test.tsx", "test-only"),
        ("frontend/tests/e2e/foo.spec.ts", "test-only"),
        ("docs/anything.md", "docs-only"),
        ("infrastructure/docker/compose.yml", "docs-only"),
        ("scripts/ci.sh", "docs-only"),
        ("backend/alembic/versions/20260101_000000_x.py", "migration"),
    ],
)
def test_per_path_classification(path: str, expected: str) -> None:
    result = classify_changes([path])
    by_path = dict(result.per_file)
    assert by_path[path] == expected, (
        f"path {path!r} expected to classify as {expected!r}, got {by_path[path]!r}"
    )

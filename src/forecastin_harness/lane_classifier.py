"""Lane-aware gate policy — classify a changed-files list into a gate kind.

Background
==========

The 2026-05-09 drain blocked docs-only PRs behind the full backend
pytest suite for ~30 minutes per attempt. The pytest gate was the
only one CI knew how to run; a docs-only PR has no business waiting
on it. A lane-aware classifier lets the harness pick the right gate
for the change kind.

Design
------

* :func:`classify_changes` is the pure entry point. Tests inject a
  list of changed-file paths; the function returns a
  :class:`LaneClassification` naming the gate kind plus the rationale.
* Classification is *strict*: when a change touches files in more
  than one bucket, the most-restrictive bucket wins. A docs PR that
  also touches a backend route is a backend PR.
* The classification influences which gates run; it never alone
  authorises bypassing a gate. Operators still inspect the report
  before merging.

Gate kinds
----------

* ``docs-only`` — markdown / yaml / docs-inventory only. Gate:
  markdown lint + docs inventory regen check.
* ``test-only`` — only paths under ``backend/tests/`` or
  ``frontend/src/__tests__/`` etc. Gate: targeted pytest / vitest on
  the test files themselves.
* ``backend-code`` — any path under ``backend/app/``,
  ``backend/scripts/``, ``backend/migrations/`` (non-migration). Gate:
  targeted backend pytest + ruff + mypy.
* ``frontend-code`` — paths under ``frontend/src/`` other than
  ``frontend/src/__tests__/``. Gate: targeted vitest + eslint +
  type-check.
* ``runtime-code`` — services / routes that handle live data. Gate:
  same as backend-code plus a curl proof against a running stack.
  The classifier flags it; the harness does not run the curl.
* ``migration`` — Alembic migration. Gate: migration safety.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

#: Path prefix → gate kind mapping. Ordered most-specific first.
#: The classifier walks each changed file, finds the first prefix that
#: matches, and counts the file under that gate kind.
PATH_PREFIX_TO_KIND: tuple[tuple[str, str], ...] = (
    # Migrations are most specific — they shadow backend-code.
    ("backend/alembic/versions/", "migration"),
    ("backend/migrations/", "migration"),
    # Test paths shadow code paths in the same tree.
    ("backend/tests/", "test-only"),
    ("frontend/src/__tests__/", "test-only"),
    ("frontend/tests/", "test-only"),
    ("frontend/src/test/", "test-only"),
    ("tests/", "test-only"),
    # Runtime-code prefixes — endpoints, services, websocket.
    ("backend/app/api/", "runtime-code"),
    ("backend/app/services/", "runtime-code"),
    ("backend/app/websocket/", "runtime-code"),
    # Other backend code is backend-code.
    ("backend/app/", "backend-code"),
    ("backend/scripts/", "backend-code"),
    # Frontend code (non-test) is frontend-code.
    ("frontend/src/", "frontend-code"),
    ("frontend/public/", "frontend-code"),
    # Docs and config.
    ("docs/", "docs-only"),
    ("README", "docs-only"),
    (".github/", "docs-only"),
    ("infrastructure/", "docs-only"),
    ("scripts/", "docs-only"),
)

#: File extensions that are always docs-only regardless of where they
#: live. Caveat: this is an after-the-prefix check, so ``backend/app/foo.md``
#: still classifies as ``backend-code`` under the prefix rule.
DOCS_EXTENSIONS: frozenset[str] = frozenset({".md", ".rst", ".txt"})

#: Most-restrictive ordering. When a change-set spans multiple kinds,
#: the result is the highest-restriction kind seen.
RESTRICTION_ORDER: tuple[str, ...] = (
    "migration",
    "runtime-code",
    "backend-code",
    "frontend-code",
    "test-only",
    "docs-only",
)

#: Gate command recipe per kind. The harness does not run these
#: directly — it returns them in the report so the CLI / runbook
#: documents the canonical command per kind.
GATE_RECIPES: dict[str, tuple[str, ...]] = {
    "docs-only": (
        "python scripts/docs/build_inventory.py --check",
        "markdownlint docs/ '*.md'",
    ),
    "test-only": (
        "pytest <changed-test-files>",
    ),
    "backend-code": (
        "pytest <targeted-tests>",
        "ruff check <changed-files>",
        "mypy app",
    ),
    "frontend-code": (
        "vitest run <targeted-tests>",
        "eslint <changed-files>",
        "tsc --noEmit",
    ),
    "runtime-code": (
        "pytest <targeted-tests>",
        "ruff check <changed-files>",
        "mypy app",
        "curl <runtime-endpoint>  # operator captures evidence",
    ),
    "migration": (
        "python scripts/check_migration_safety.py",
        "alembic upgrade head && alembic downgrade base && alembic upgrade head",
    ),
}


@dataclass(frozen=True)
class LaneClassification:
    """Result of :func:`classify_changes`.

    ``kind`` is the gate kind the harness should run.
    ``per_file`` exposes the per-file classification so the CLI can
    show *why* a change-set is not docs-only when the operator
    expected it to be.
    """

    kind: str
    rationale: str
    per_file: tuple[tuple[str, str], ...]
    gate_recipe: tuple[str, ...]

    @property
    def is_docs_only(self) -> bool:
        return self.kind == "docs-only"

    @property
    def requires_full_backend_pytest(self) -> bool:
        """Mirror of the harness rule: docs-only never runs full pytest.

        ``empty`` is a no-op change-set (the operator's diff is empty);
        there is nothing to gate, so the property is False. Treating
        it as True would force the harness to run a backend pytest for
        a no-op, which is the symptom this whole module exists to
        prevent.
        """
        return self.kind not in ("docs-only", "test-only", "frontend-code", "empty")


def classify_changes(paths: Iterable[str]) -> LaneClassification:
    """Classify a change-set into a single gate kind.

    Empty input is rejected with ``kind="empty"`` so downstream code
    never silently passes a no-op as docs-only.
    """
    files = [p.strip() for p in paths if p.strip()]
    if not files:
        return LaneClassification(
            kind="empty",
            rationale="no changed files",
            per_file=(),
            gate_recipe=(),
        )

    per_file: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in files:
        kind = _classify_one(path)
        per_file.append((path, kind))
        seen.add(kind)

    # Pick the most-restrictive seen kind per RESTRICTION_ORDER.
    chosen: str = "docs-only"
    for k in RESTRICTION_ORDER:
        if k in seen:
            chosen = k
            break

    rationale = (
        f"selected most-restrictive kind {chosen!r} from kinds seen: "
        f"{sorted(seen)}"
    )
    return LaneClassification(
        kind=chosen,
        rationale=rationale,
        per_file=tuple(per_file),
        gate_recipe=GATE_RECIPES.get(chosen, ()),
    )


def _classify_one(path: str) -> str:
    """Classify a single path against :data:`PATH_PREFIX_TO_KIND`.

    Falls back to ``docs-only`` when the path matches no prefix and
    its extension is in :data:`DOCS_EXTENSIONS`. Otherwise returns
    ``backend-code`` as the conservative default — unknown paths must
    not cause the harness to skip backend gates.
    """
    p = path.replace("\\", "/")
    for prefix, kind in PATH_PREFIX_TO_KIND:
        if p.startswith(prefix):
            return kind
    # Suffix fallback for top-level docs files.
    for ext in DOCS_EXTENSIONS:
        if p.lower().endswith(ext):
            return "docs-only"
    return "backend-code"


def render_classification(report: LaneClassification) -> str:
    """Human-readable rendering used by the CLI."""
    lines: list[str] = [
        f"lane_kind = {report.kind}",
        f"requires_full_backend_pytest = {str(report.requires_full_backend_pytest).lower()}",
        f"rationale: {report.rationale}",
        "gate recipe:",
    ]
    if report.gate_recipe:
        for cmd in report.gate_recipe:
            lines.append(f"  $ {cmd}")
    else:
        lines.append("  (no gates for this kind)")
    if report.per_file:
        lines.append("per-file classification:")
        for path, kind in report.per_file:
            lines.append(f"  {kind:14}  {path}")
    return "\n".join(lines)

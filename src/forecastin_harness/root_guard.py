"""Root guard — refuse to run when launched from a forbidden repo root.

Background
==========

The 2026-05-09 drain repeatedly relaunched Claude in the legacy
``J:/Forcastin`` checkout instead of the canonical
``J:/Forecastin-worktrees/_coordinator-main`` worktree. Hooks, merge
guards, and Docker mounts in that legacy checkout silently corrupted
runtime evidence and merge state. The cost was hours of churn before
the operator caught the wrong-root drift.

This module provides a *pure* guard: given a candidate cwd and an
expected root, it returns a verdict the CLI can print and exit on.
The CLI subcommand ``preflight root`` is the public surface.

Design
------

* No filesystem mutation, no network. Returns a dataclass.
* ``forbidden_roots`` is a set of canonical paths the guard treats as
  hard rejects regardless of whether cwd happens to equal one.
* Path comparison is canonical: ``Path.resolve(strict=False)``. Symlinks
  collapse, case-only differences on Windows collapse, trailing
  separators collapse.
* The guard returns a dedicated ``WRONG_ROOT`` verdict so callers can
  match on the exact string the operator runbook documents.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

#: Default canonical worktree root for Forecastin coordination work.
#: Configurable via ``preflight root --expected``; this is the documented
#: out-of-the-box default.
DEFAULT_EXPECTED_ROOT = Path("J:/Forecastin-worktrees/_coordinator-main")

#: Default forbidden roots. The legacy ``J:/Forcastin`` checkout is the
#: primary trap from the 2026-05-09 incident. Added here so it is
#: rejected even if the operator forgets to configure it.
DEFAULT_FORBIDDEN_ROOTS: tuple[Path, ...] = (
    Path("J:/Forcastin"),
)


@dataclass(frozen=True)
class RootGuardResult:
    """Verdict produced by :func:`check_root`.

    ``verdict`` is one of:

    * ``"ok"`` — cwd resolves to ``expected`` and is not in
      ``forbidden``.
    * ``"WRONG_ROOT"`` — cwd is in ``forbidden`` *or* does not equal
      ``expected``. The CLI prints the documented message and exits 2.
    """

    verdict: str  # "ok" | "WRONG_ROOT"
    cwd_resolved: Path
    expected_resolved: Path
    forbidden_resolved: tuple[Path, ...]
    message: str

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"


def _resolve(p: Path) -> Path:
    """Resolve a path canonically, tolerating non-existent paths.

    ``Path.resolve(strict=False)`` collapses symlinks where possible,
    case-folds on Windows for the on-disk components that exist, and
    leaves the rest as-is. We use this consistently across all path
    comparisons so the guard never trips on cosmetic differences.
    """
    return Path(p).resolve(strict=False)


def check_root(
    cwd: Path,
    *,
    expected: Path = DEFAULT_EXPECTED_ROOT,
    forbidden: Iterable[Path] = DEFAULT_FORBIDDEN_ROOTS,
) -> RootGuardResult:
    """Return a :class:`RootGuardResult` for ``cwd``.

    The check is fail-closed: any path that is not exactly ``expected``
    after resolution returns ``WRONG_ROOT``. This is intentional —
    half-matching paths (e.g. the operator launched in a sibling
    worktree) are exactly the trap the guard exists to catch.
    """
    cwd_r = _resolve(cwd)
    expected_r = _resolve(expected)
    forbidden_r = tuple(_resolve(p) for p in forbidden)

    # Forbidden roots win. They are explicit "this checkout is broken /
    # legacy / dirty / forbidden" markers.
    for f in forbidden_r:
        if cwd_r == f or _is_under(cwd_r, f):
            return RootGuardResult(
                verdict="WRONG_ROOT",
                cwd_resolved=cwd_r,
                expected_resolved=expected_r,
                forbidden_resolved=forbidden_r,
                message=(
                    f"WRONG_ROOT: relaunch Claude from {expected_r}. "
                    f"Detected forbidden root: {f}"
                ),
            )

    if cwd_r != expected_r:
        return RootGuardResult(
            verdict="WRONG_ROOT",
            cwd_resolved=cwd_r,
            expected_resolved=expected_r,
            forbidden_resolved=forbidden_r,
            message=f"WRONG_ROOT: relaunch Claude from {expected_r}",
        )

    return RootGuardResult(
        verdict="ok",
        cwd_resolved=cwd_r,
        expected_resolved=expected_r,
        forbidden_resolved=forbidden_r,
        message=f"ok: cwd matches expected root {expected_r}",
    )


def _is_under(child: Path, parent: Path) -> bool:
    """Return ``True`` when ``child`` is at-or-under ``parent``.

    Uses ``Path.relative_to`` semantics; on failure the paths are
    siblings or share no common ancestor and the result is ``False``.
    Both inputs must already be resolved.
    """
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True

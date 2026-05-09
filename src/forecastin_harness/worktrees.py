"""Lane / worktree planner.

The planner is split into two phases:

1. ``plan_lane`` — pure function. Given a config, a lane name, a task id, and
   a scope, it returns a :class:`LanePlan` describing what would happen, and a
   list of git commands. No filesystem changes. This is what ``--dry-run``
   prints.
2. ``apply_plan`` — executes the plan via ``git worktree add`` and registers
   the lane in :class:`StateStore`. The CLI only invokes this when the
   operator drops ``--dry-run``.

Branch naming is deterministic so the same (task, scope) pair produces the
same branch on retry. Worktree paths are absolute and live under
``config.worktrees.root``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import HarnessConfig
from .state import Lane, StateError, StateStore, utcnow_iso

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(value: str) -> str:
    """Lower-case, hyphen-separated, ASCII-only slug.

    Used for both branch names and worktree directory names so they sort
    predictably. Empty input is rejected because it would produce ambiguous
    branch names.
    """
    s = _SLUG_RE.sub("-", value.lower()).strip("-")
    if not s:
        raise ValueError("slugify input produced empty result")
    return s


@dataclass(frozen=True)
class LanePlan:
    name: str
    task_id: str
    scope: str
    branch: str
    base_branch: str
    worktree_path: Path
    commands: tuple[tuple[str, ...], ...]

    def render_commands(self) -> str:
        """Human-readable rendering used by ``--dry-run`` output."""
        return "\n".join(" ".join(cmd) for cmd in self.commands)


def plan_lane(
    config: HarnessConfig,
    *,
    name: str,
    task_id: str,
    scope: str,
) -> LanePlan:
    """Compute what creating ``name`` would do, without touching disk."""
    if not name:
        raise ValueError("lane name is required")
    if not task_id:
        raise ValueError("task id is required")
    if not scope:
        raise ValueError("scope is required")

    name_slug = slugify(name)
    task_slug = slugify(task_id)
    branch = f"lane/{task_slug}-{name_slug}"
    worktree_path = (config.worktrees.root / name_slug).resolve()
    base_branch = f"origin/{config.target.main_branch}"

    target_repo = str(config.target.path)
    commands: tuple[tuple[str, ...], ...] = (
        ("git", "-C", target_repo, "fetch", "origin", "--prune"),
        (
            "git",
            "-C",
            target_repo,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            base_branch,
        ),
    )
    return LanePlan(
        name=name_slug,
        task_id=task_slug,
        scope=scope,
        branch=branch,
        base_branch=base_branch,
        worktree_path=worktree_path,
        commands=commands,
    )


def apply_plan(
    plan: LanePlan,
    store: StateStore,
    *,
    owner: str | None = None,
    runner=subprocess.run,
) -> Lane:
    """Materialise the worktree and register the lane.

    Raises :class:`StateError` if a lane with the same name already exists,
    *before* executing any git command.
    """
    if store.find_lane(plan.name) is not None:
        raise StateError(f"lane already exists: {plan.name}")

    base_sha = ""
    head_sha = ""
    for cmd in plan.commands:
        result = runner(list(cmd), check=True, capture_output=True, text=True)
        # We deliberately do not parse fetch output; SHAs are read after.
        _ = result

    rev_parse_base = runner(
        ["git", "-C", str(plan.worktree_path), "rev-parse", plan.base_branch],
        check=True,
        capture_output=True,
        text=True,
    )
    base_sha = rev_parse_base.stdout.strip()
    rev_parse_head = runner(
        ["git", "-C", str(plan.worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    head_sha = rev_parse_head.stdout.strip()

    lane = Lane(
        name=plan.name,
        task_id=plan.task_id,
        scope=plan.scope,
        branch=plan.branch,
        base_sha=base_sha,
        head_sha=head_sha,
        worktree=str(plan.worktree_path),
        status="planned",
        owner=owner,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    store.add_lane(lane)
    store.append_event(
        {
            "kind": "lane.created",
            "lane": lane.name,
            "branch": lane.branch,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "worktree": lane.worktree,
        }
    )
    return lane


def plan_retire(store: StateStore, name: str) -> Lane:
    """Plan retirement of a lane. Pure: returns the would-be-updated lane."""
    lane = store.find_lane(name)
    if lane is None:
        raise StateError(f"unknown lane: {name}")
    if lane.status == "retired":
        raise StateError(f"lane already retired: {name}")
    return Lane(
        name=lane.name,
        task_id=lane.task_id,
        scope=lane.scope,
        branch=lane.branch,
        base_sha=lane.base_sha,
        head_sha=lane.head_sha,
        worktree=lane.worktree,
        status="retired",
        owner=lane.owner,
        created_at=lane.created_at,
        updated_at=utcnow_iso(),
        notes=lane.notes,
    )


def retire_lane(store: StateStore, name: str) -> Lane:
    """Mark the lane retired in state. Does NOT remove the worktree on disk.

    Worktree removal is destructive and out-of-scope for the harness; the
    operator removes the worktree manually after confirming nothing useful
    remains. The harness only updates bookkeeping.
    """
    new = plan_retire(store, name)
    store.upsert_lane(new)
    store.append_event({"kind": "lane.retired", "lane": new.name})
    return new

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


class LaneApplyError(RuntimeError):
    """Raised when ``apply_plan`` fails after the lane was recorded.

    The lane is still in the registry (status ``blocked``); the operator
    can inspect it via :func:`recover_orphans` and clean up the orphan
    worktree on disk if any.
    """


def _truncate(text: str, limit: int = 400) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _failure_reason(exc: BaseException) -> str:
    """Build a stable, length-bounded failure reason for the lane notes."""
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if isinstance(stderr, str) and stderr.strip():
        return _truncate(f"{type(exc).__name__}: {stderr}")
    return _truncate(f"{type(exc).__name__}: {exc}")


def apply_plan(
    plan: LanePlan,
    store: StateStore,
    *,
    owner: str | None = None,
    runner=subprocess.run,
) -> Lane:
    """Materialise the worktree and register the lane transactionally.

    Lifecycle:

    1. Reject duplicates (``StateError``) before any side effect.
    2. Write a ``planned`` lane record to state *before* invoking git, so a
       crash mid-flight always leaves a discoverable record (no orphan
       worktrees the registry doesn't know about).
    3. Run the planned git commands + rev-parse for SHAs. On success, update
       the lane in place with the resolved ``base_sha`` / ``head_sha`` and
       emit ``lane.created``.
    4. On any failure during git execution, mark the lane ``blocked``,
       capture the reason in ``notes``, emit ``lane.blocked``, and re-raise.
    """
    if store.find_lane(plan.name) is not None:
        raise StateError(f"lane already exists: {plan.name}")

    now = utcnow_iso()
    lane = Lane(
        name=plan.name,
        task_id=plan.task_id,
        scope=plan.scope,
        branch=plan.branch,
        base_sha="",
        head_sha="",
        worktree=str(plan.worktree_path),
        status="planned",
        owner=owner,
        created_at=now,
        updated_at=now,
    )
    # Step 1 (state-first): record the intent before any disk mutation.
    store.add_lane(lane)

    try:
        for cmd in plan.commands:
            result = runner(list(cmd), check=True, capture_output=True, text=True)
            # Fetch + worktree-add output is not parsed; SHAs come from rev-parse.
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
    except BaseException as exc:
        reason = _failure_reason(exc)
        blocked = Lane(
            name=lane.name,
            task_id=lane.task_id,
            scope=lane.scope,
            branch=lane.branch,
            base_sha="",
            head_sha="",
            worktree=lane.worktree,
            status="blocked",
            owner=lane.owner,
            created_at=lane.created_at,
            updated_at=utcnow_iso(),
            notes=reason,
        )
        store.upsert_lane(blocked)
        store.append_event(
            {
                "kind": "lane.blocked",
                "lane": blocked.name,
                "branch": blocked.branch,
                "worktree": blocked.worktree,
                "reason": reason,
            }
        )
        raise

    updated = Lane(
        name=lane.name,
        task_id=lane.task_id,
        scope=lane.scope,
        branch=lane.branch,
        base_sha=base_sha,
        head_sha=head_sha,
        worktree=lane.worktree,
        status="planned",
        owner=lane.owner,
        created_at=lane.created_at,
        updated_at=utcnow_iso(),
    )
    store.upsert_lane(updated)
    store.append_event(
        {
            "kind": "lane.created",
            "lane": updated.name,
            "branch": updated.branch,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "worktree": updated.worktree,
        }
    )
    return updated


def recover_orphans(store: StateStore) -> list[Lane]:
    """Return lanes left in ``blocked`` state with a recorded worktree path.

    These are lanes whose registry record exists but whose git worktree
    creation (or post-create rev-parse) failed. The operator inspects the
    list, decides whether to remove the orphan worktree on disk, and then
    either retries the lane (after `retire_lane` clears the record) or
    leaves it blocked for forensic purposes.
    """
    return [
        lane
        for lane in store.load_lanes()
        if lane.status == "blocked" and lane.worktree
    ]


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

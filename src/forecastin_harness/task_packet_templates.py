"""Generated task packet templates for common drain lanes.

Background
==========

The 2026-05-09 drain blocked because ``controller validate`` requires
``--task-file`` and the operator was not handed a ready-made packet
for "open PR drain", "docs-only spec batch", "runtime bugfix", or
"Linear close-out". Each drain shape needs a slightly different set
of fields. This module ships generators so the operator (or the
harness CLI) can produce a packet without hand-crafting YAML.

Design
------

* Each generator returns a Python ``dict`` that satisfies every
  controller validator (objective, target, worktree, official_gate,
  files.forbidden, final_report contract, lane_type, merge_authority,
  no_bypass_permissions).
* The CLI subcommand ``task generate`` writes the dict as YAML.
* Templates explicitly document the drain-loop semantics so the
  operator does not treat lane reports as stopping conditions.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Drain-loop stop conditions every generated packet inherits via
#: ``stop_conditions``. Operators should not delete entries; they may
#: add specific extras (e.g. ``"DB schema migration encountered"``).
STANDARD_STOP_CONDITIONS: tuple[str, ...] = (
    "credentials missing",
    "destructive operation",
    "merge conflict",
    "product or architecture decision required",
    "no safe actionable issues remain",
    "wrong root detected",
    "invalid runtime provenance detected",
)

#: Standard final-report sections every generated packet inherits.
#: Controller validators check for ``changed files``, ``tests``, and
#: ``remaining risks``; we add ``next`` so chained drains know what to
#: pick up.
STANDARD_FINAL_REPORT: tuple[str, ...] = (
    "changed files",
    "tests",
    "remaining risks",
    "next action",
)


def generate_open_pr_drain(
    *,
    target_repo: str,
    worktree: str,
    pr_owner_repo: str,
    main_branch: str = "main",
    max_runtime_minutes: int = 60,
) -> dict[str, Any]:
    """Packet for "drain all open PRs" — list, classify, merge per policy."""
    return {
        "mission": (
            f"Drain all open PRs in {pr_owner_repo}. For each PR: classify "
            "via lane_classifier (docs-only, test-only, backend-code, "
            "frontend-code, runtime-code, migration); run the gate recipe; "
            "merge with SHA pin only when merge_evidence verifies; close "
            "Linear after reachable-from-main is proven."
        ),
        "target_repo": target_repo,
        "worktree": worktree,
        "lane_type": "review",
        "files": {
            "forbidden": [
                "J:/Forcastin",
                "J:/Forcastin/**",
            ],
            "allowed": [],
        },
        "merge_authority": "human-only",
        "no_bypass_permissions": True,
        "official_gate_command": (
            "forecastin-harness preflight merge-evidence --pr <PR> "
            f"--repo {pr_owner_repo}"
        ),
        "max_runtime_minutes": max_runtime_minutes,
        "main_branch": main_branch,
        "stop_conditions": list(STANDARD_STOP_CONDITIONS),
        "final_report": list(STANDARD_FINAL_REPORT),
        "drain_loop": {
            "lane_report_is_checkpoint": True,
            "after_report": "pick next actionable lane",
        },
    }


def generate_docs_only_spec_batch(
    *,
    target_repo: str,
    worktree: str,
    parent_linear_id: str,
    child_linear_ids: list[str],
    docs_path_glob: str,
    max_runtime_minutes: int = 90,
) -> dict[str, Any]:
    """Packet for "batch a set of spec-only Linear issues into one PR".

    The generated packet asserts ``files.allowed`` only mentions
    docs-related paths, so a controller validator that runs the
    ``feature_lane_only_touches_gate_paths`` style mix-check will
    pass.
    """
    return {
        "mission": (
            f"Author a single batched spec doc covering {parent_linear_id} "
            f"and its children {', '.join(child_linear_ids)}. The doc "
            f"lives under {docs_path_glob}. No code changes."
        ),
        "target_repo": target_repo,
        "worktree": worktree,
        "lane_type": "review",
        "files": {
            "forbidden": [
                "J:/Forcastin",
                "J:/Forcastin/**",
                "backend/app/**",
                "backend/migrations/**",
                "backend/alembic/**",
                "frontend/src/**",
            ],
            "allowed": [
                docs_path_glob,
                "docs/_meta/documentation-inventory.json",
                "docs/_meta/documentation-inventory.md",
            ],
        },
        "merge_authority": "human-only",
        "no_bypass_permissions": True,
        "official_gate_command": (
            "forecastin-harness preflight lane-policy --files <changed-files>  "
            "&& python scripts/docs/build_inventory.py --check"
        ),
        "max_runtime_minutes": max_runtime_minutes,
        "main_branch": "main",
        "linear": {
            "parent": parent_linear_id,
            "children": child_linear_ids,
        },
        "stop_conditions": list(STANDARD_STOP_CONDITIONS),
        "final_report": list(STANDARD_FINAL_REPORT),
        "drain_loop": {
            "lane_report_is_checkpoint": True,
            "after_report": "pick next actionable spec batch",
        },
    }


def generate_runtime_bugfix(
    *,
    target_repo: str,
    worktree: str,
    linear_id: str,
    failing_endpoint: str,
    expected_runtime_evidence: str,
    max_runtime_minutes: int = 90,
) -> dict[str, Any]:
    """Packet for "fix a runtime bug with curl/log evidence".

    Generates ``files.forbidden`` that includes the legacy checkout so
    runtime evidence cannot be collected from a J:/Forcastin-mounted
    container.
    """
    return {
        "mission": (
            f"Fix the runtime defect tracked by {linear_id} affecting "
            f"{failing_endpoint}. Provide runtime proof via the "
            "harness's runtime-provenance guard before declaring done."
        ),
        "target_repo": target_repo,
        "worktree": worktree,
        "lane_type": "feature",
        "files": {
            "forbidden": [
                "J:/Forcastin",
                "J:/Forcastin/**",
                "backend/alembic/versions/**",
            ],
            "allowed": [
                "backend/app/api/**",
                "backend/app/services/**",
                "backend/tests/**",
                "frontend/src/**",
            ],
        },
        "merge_authority": "human-only",
        "no_bypass_permissions": True,
        "official_gate_command": (
            "forecastin-harness preflight runtime --service backend  "
            "&& forecastin-harness preflight runtime --service frontend"
        ),
        "max_runtime_minutes": max_runtime_minutes,
        "main_branch": "main",
        "linear": {"id": linear_id},
        "expected_runtime_evidence": expected_runtime_evidence,
        "stop_conditions": list(STANDARD_STOP_CONDITIONS),
        "final_report": list(STANDARD_FINAL_REPORT) + [
            "runtime evidence (curl, logs, DB rows)",
        ],
        "drain_loop": {
            "lane_report_is_checkpoint": True,
            "after_report": "verify reachability + Linear close-out",
        },
    }


def generate_linear_closeout(
    *,
    target_repo: str,
    worktree: str,
    parent_linear_id: str,
    expect_children_done: int,
    pr_owner_repo: str = "Oscillate-JP/Forecastin",
    max_runtime_minutes: int = 30,
) -> dict[str, Any]:
    """Packet for "close out a Linear parent whose children are all Done".

    ``pr_owner_repo`` is required for the generated official_gate_command
    because ``preflight merge-evidence`` itself requires ``--repo``.
    Defaulted to the canonical Forecastin repo so single-arg callers still
    produce a runnable packet.
    """
    return {
        "mission": (
            f"Close out {parent_linear_id} after verifying all "
            f"{expect_children_done} children are Done with merged-PR "
            "evidence. No code changes."
        ),
        "target_repo": target_repo,
        "worktree": worktree,
        "lane_type": "review",
        "files": {
            "forbidden": [
                "J:/Forcastin",
                "J:/Forcastin/**",
            ],
            "allowed": [],
        },
        "merge_authority": "human-only",
        "no_bypass_permissions": True,
        "official_gate_command": (
            f"forecastin-harness preflight merge-evidence --pr <ATTACHED_PR> "
            f"--repo {pr_owner_repo}"
        ),
        "max_runtime_minutes": max_runtime_minutes,
        "main_branch": "main",
        "linear": {"parent": parent_linear_id},
        "stop_conditions": list(STANDARD_STOP_CONDITIONS),
        "final_report": list(STANDARD_FINAL_REPORT),
        "drain_loop": {
            "lane_report_is_checkpoint": True,
            "after_report": "pick next Linear cleanup",
        },
    }


GENERATORS: Mapping[str, Any] = {
    "open-pr-drain": generate_open_pr_drain,
    "docs-only-spec-batch": generate_docs_only_spec_batch,
    "runtime-bugfix": generate_runtime_bugfix,
    "linear-closeout": generate_linear_closeout,
}

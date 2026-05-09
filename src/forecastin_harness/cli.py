"""``forecastin-harness`` command-line entry point.

The CLI is intentionally thin: each subcommand wires arguments through to the
relevant module. Dry-run is the default for every state-changing command;
the operator must pass an explicit flag (or the absence of ``--dry-run``)
to apply changes. Every command emits human-readable text on stdout and a
non-zero exit code on validation failure so it is safe to compose in a
shell pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import ConfigError, HarnessConfig, load_config
from .gates import initialise as gate_initialise
from .gates import plan_gate, read_state, tail_text
from .pr_gate import gh_available, plan_pr_check, run_pr_check
from .prompts import (
    SUPPORTED_AGENTS,
    TaskPacketError,
    load_packet,
    render_prompt,
)
from .state import StateStore
from .worktrees import apply_plan, plan_lane, plan_retire, retire_lane


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forecastin-harness",
        description="Standalone AI-agent harness for Forecastin and friends.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"forecastin-harness {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Initialise harness state for a target repo.")
    p_init.add_argument("--target-repo", required=True, help="Path to target repo working copy.")
    p_init.add_argument("--config", required=True, help="Path to adapter YAML.")
    p_init.set_defaults(func=cmd_init)

    # lanes
    p_lanes = sub.add_parser("lanes", help="Lane (worktree) operations.")
    p_lanes_sub = p_lanes.add_subparsers(dest="lanes_cmd", required=True)

    p_l_list = p_lanes_sub.add_parser("list", help="List registered lanes.")
    p_l_list.add_argument("--config", required=True)
    p_l_list.set_defaults(func=cmd_lanes_list)

    p_l_create = p_lanes_sub.add_parser("create", help="Create a new lane (dry-run by default).")
    p_l_create.add_argument("--config", required=True)
    p_l_create.add_argument("--name", required=True)
    p_l_create.add_argument("--task", required=True, help="Task id, e.g. FOR-235.")
    p_l_create.add_argument("--scope", required=True)
    p_l_create.add_argument("--owner", default=None)
    _add_dry_run(p_l_create)
    p_l_create.set_defaults(func=cmd_lanes_create)

    p_l_status = p_lanes_sub.add_parser("status", help="Print a lane status report.")
    p_l_status.add_argument("--config", required=True)
    p_l_status.set_defaults(func=cmd_lanes_status)

    p_l_retire = p_lanes_sub.add_parser("retire", help="Retire a lane.")
    p_l_retire.add_argument("--config", required=True)
    p_l_retire.add_argument("--name", required=True)
    _add_dry_run(p_l_retire)
    p_l_retire.set_defaults(func=cmd_lanes_retire)

    # task render
    p_task = sub.add_parser("task", help="Task packet operations.")
    p_task_sub = p_task.add_subparsers(dest="task_cmd", required=True)
    p_task_render = p_task_sub.add_parser("render", help="Render an agent prompt from a task packet.")
    p_task_render.add_argument("--task-file", required=True)
    p_task_render.add_argument("--agent", required=True, choices=SUPPORTED_AGENTS)
    p_task_render.add_argument(
        "--templates-dir",
        default=str(_default_templates_dir()),
        help="Directory holding <agent>_prompt.md templates.",
    )
    p_task_render.set_defaults(func=cmd_task_render)

    # gate
    p_gate = sub.add_parser("gate", help="Long-running CI gate operations.")
    p_gate_sub = p_gate.add_subparsers(dest="gate_cmd", required=True)

    p_g_start = p_gate_sub.add_parser("start", help="Plan or start a gate run.")
    p_g_start.add_argument("--config", required=True)
    p_g_start.add_argument("--name", required=True)
    p_g_start.add_argument("--command", required=True)
    _add_dry_run(p_g_start)
    p_g_start.set_defaults(func=cmd_gate_start)

    p_g_status = p_gate_sub.add_parser("status", help="Print gate status.")
    p_g_status.add_argument("--config", required=True)
    p_g_status.add_argument("--name", required=True)
    p_g_status.set_defaults(func=cmd_gate_status)

    p_g_tail = p_gate_sub.add_parser("tail", help="Tail the gate log.")
    p_g_tail.add_argument("--config", required=True)
    p_g_tail.add_argument("--name", required=True)
    p_g_tail.add_argument("--lines", type=int, default=50)
    p_g_tail.set_defaults(func=cmd_gate_tail)

    # pr
    p_pr = sub.add_parser("pr", help="Pull-request gate operations.")
    p_pr_sub = p_pr.add_subparsers(dest="pr_cmd", required=True)

    p_pr_check = p_pr_sub.add_parser("check", help="Run or plan a PR check.")
    p_pr_check.add_argument("--config", required=True)
    p_pr_check.add_argument("--pr", type=int, required=True)
    p_pr_check.add_argument("--head-sha", required=True)
    _add_dry_run(p_pr_check)
    p_pr_check.set_defaults(func=cmd_pr_check)

    return parser


def _add_dry_run(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only; do not modify state or invoke external tools.",
    )


def _default_templates_dir() -> Path:
    # Templates ship next to the package source in the repo layout, NOT inside
    # the installed package. We therefore resolve relative to the repo root
    # discovered upward from this file. If not found, fall back to CWD/templates.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "templates"
        if candidate.is_dir():
            return candidate
    return Path.cwd() / "templates"


# ---------------- command implementations ----------------


def cmd_init(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    target = Path(args.target_repo).resolve()
    if not target.is_dir():
        print(f"target repo path does not exist: {target}", file=sys.stderr)
        return 2
    if not (target / ".git").exists():
        print(f"target repo path is not a git checkout: {target}", file=sys.stderr)
        return 2
    state_root = target / config.state.dir
    store = StateStore(state_root)
    store.ensure_layout()
    store.append_event(
        {
            "kind": "harness.init",
            "target_repo": str(target),
            "config_repo": config.target.repo,
            "state_dir": str(state_root),
        }
    )
    print(f"initialised harness state at {state_root}")
    print(f"target repo: {config.target.repo} ({target})")
    print(f"main branch: {config.target.main_branch}")
    print(f"agents:      {', '.join(config.agents)}")
    return 0


def cmd_lanes_list(args: argparse.Namespace) -> int:
    config, store = _config_and_store(args.config)
    lanes = store.load_lanes()
    if not lanes:
        print("(no lanes registered)")
        return 0
    print(f"{'NAME':30} {'TASK':12} {'STATUS':10} {'BRANCH'}")
    for lane in lanes:
        print(f"{lane.name:30} {lane.task_id:12} {lane.status:10} {lane.branch}")
    return 0


def cmd_lanes_create(args: argparse.Namespace) -> int:
    config, store = _config_and_store(args.config)
    plan = plan_lane(
        config,
        name=args.name,
        task_id=args.task,
        scope=args.scope,
    )
    del config  # used only for plan construction; explicit drop documents intent
    print(f"lane plan: {plan.name}")
    print(f"  task:     {plan.task_id}")
    print(f"  scope:    {plan.scope}")
    print(f"  branch:   {plan.branch}")
    print(f"  base:     {plan.base_branch}")
    print(f"  worktree: {plan.worktree_path}")
    print()
    print("commands:")
    print(plan.render_commands())
    if args.dry_run:
        print()
        print("(dry-run; nothing executed)")
        return 0
    lane = apply_plan(plan, store, owner=args.owner)
    print()
    print(f"lane registered: {lane.name} (head={lane.head_sha[:12]})")
    return 0


def cmd_lanes_status(args: argparse.Namespace) -> int:
    config, store = _config_and_store(args.config)
    lanes = store.load_lanes()
    print(f"target repo: {config.target.repo}")
    print(f"state dir:   {store.root}")
    print(f"lane count:  {len(lanes)}")
    counts: dict[str, int] = {}
    for lane in lanes:
        counts[lane.status] = counts.get(lane.status, 0) + 1
    for status, n in sorted(counts.items()):
        print(f"  {status:10} {n}")
    return 0


def cmd_lanes_retire(args: argparse.Namespace) -> int:
    _, store = _config_and_store(args.config)
    if args.dry_run:
        new = plan_retire(store, args.name)
        print(f"would retire lane: {new.name}")
        print(f"  branch:    {new.branch}")
        print(f"  worktree:  {new.worktree}")
        print("note: harness does NOT remove the worktree from disk; operator does that manually.")
        return 0
    new = retire_lane(store, args.name)
    print(f"lane retired: {new.name}")
    return 0


def cmd_task_render(args: argparse.Namespace) -> int:
    try:
        packet = load_packet(args.task_file)
    except TaskPacketError as exc:
        print(f"task packet error: {exc}", file=sys.stderr)
        return 2
    try:
        text = render_prompt(packet, args.agent, templates_dir=Path(args.templates_dir))
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"render error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_gate_start(args: argparse.Namespace) -> int:
    _, store = _config_and_store(args.config)
    plan = plan_gate(store, name=args.name, command=args.command)
    print(plan.render())
    if args.dry_run:
        print()
        print("(dry-run; no process spawned, no state file written)")
        return 0
    state = gate_initialise(plan, store)
    print()
    print(f"gate initialised: status={state.status}")
    print(f"state file: {plan.state_path}")
    print("note: this slice does not spawn the gate process. Run the planned")
    print("      command externally and use a supervisor to call the state")
    print("      transition API. See docs/architecture.md for the planned spawn.")
    return 0


def cmd_gate_status(args: argparse.Namespace) -> int:
    _, store = _config_and_store(args.config)
    plan = plan_gate(store, name=args.name, command="placeholder")
    state = read_state(plan)
    if state is None:
        print(f"gate {args.name!r} has no state file at {plan.state_path}")
        return 1
    print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_gate_tail(args: argparse.Namespace) -> int:
    _, store = _config_and_store(args.config)
    plan = plan_gate(store, name=args.name, command="placeholder")
    text = tail_text(plan, lines=args.lines)
    if not text:
        print(f"(no log at {plan.log_path})")
        return 1
    print(text)
    return 0


def cmd_pr_check(args: argparse.Namespace) -> int:
    _, store = _config_and_store(args.config)
    if args.dry_run:
        print(plan_pr_check(pr_number=args.pr, head_sha=args.head_sha))
        return 0
    if not gh_available():
        print(plan_pr_check(pr_number=args.pr, head_sha=args.head_sha))
        print()
        print("warning: 'gh' is not on PATH; live PR check skipped.")
        return 1
    result = run_pr_check(store, pr_number=args.pr, head_sha=args.head_sha)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.merge_ready else 1


def _config_and_store(config_path: str) -> tuple[HarnessConfig, StateStore]:
    config = load_config(config_path)
    store = StateStore(config.state_path)
    store.ensure_layout()
    return config, store


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

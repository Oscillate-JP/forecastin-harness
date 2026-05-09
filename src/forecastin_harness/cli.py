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
from .gate_supervisor import DEFAULT_DEADLINE_SECONDS, stop as gate_stop, summarise as gate_summarise, supervise
from .gates import initialise as gate_initialise
from .gates import plan_gate, read_state, tail_text
from .pr_gate import (
    PRMergeError,
    gh_available,
    plan_pr_check,
    plan_pr_merge,
    render_merge_command,
    run_pr_check,
)
from .prompts import (
    SUPPORTED_AGENTS,
    TaskPacketError,
    load_packet,
    render_prompt,
)
from .rendering import COMMAND_MODES, CommandSpec, from_argv, from_string
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
    p_g_start.add_argument(
        "--mode",
        choices=COMMAND_MODES,
        default=None,
        help="Command rendering mode (posix, powershell, argv). Default: host shell.",
    )
    p_g_start.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_DEADLINE_SECONDS,
        help=f"Wall-clock deadline before kill. Default: {DEFAULT_DEADLINE_SECONDS}s.",
    )
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

    p_g_stop = p_gate_sub.add_parser("stop", help="Stop a running gate.")
    p_g_stop.add_argument("--config", required=True)
    p_g_stop.add_argument("--name", required=True)
    p_g_stop.set_defaults(func=cmd_gate_stop)

    p_g_summarise = p_gate_sub.add_parser(
        "summarise", help="Print a one-shot operator-facing summary of a gate."
    )
    p_g_summarise.add_argument("--config", required=True)
    p_g_summarise.add_argument("--name", required=True)
    p_g_summarise.set_defaults(func=cmd_gate_summarise)

    # pr
    p_pr = sub.add_parser("pr", help="Pull-request gate operations.")
    p_pr_sub = p_pr.add_subparsers(dest="pr_cmd", required=True)

    p_pr_check = p_pr_sub.add_parser("check", help="Run or plan a PR check.")
    p_pr_check.add_argument("--config", required=True)
    p_pr_check.add_argument("--pr", type=int, required=True)
    p_pr_check.add_argument("--head-sha", required=True)
    _add_dry_run(p_pr_check)
    p_pr_check.set_defaults(func=cmd_pr_check)

    p_pr_merge = p_pr_sub.add_parser(
        "merge",
        help="Plan or execute a guarded PR merge using the harness's merge_ready record.",
    )
    p_pr_merge.add_argument("--config", required=True)
    p_pr_merge.add_argument("--pr", type=int, required=True)
    p_pr_merge.add_argument("--head-sha", required=True)
    p_pr_merge.add_argument(
        "--operator-confirmed",
        action="store_true",
        help="Operator confirms intent. Required to execute.",
    )
    p_pr_merge.add_argument(
        "--execute",
        action="store_true",
        help="Run the rendered gh pr merge command. Requires --operator-confirmed.",
    )
    _add_dry_run(p_pr_merge)
    p_pr_merge.set_defaults(func=cmd_pr_merge)

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
    spec = _build_command_spec(args.command, args.mode)
    plan = plan_gate(store, name=args.name, command=spec.display())
    print(plan.render())
    if args.dry_run:
        print()
        print(f"command (rendered, mode={spec.mode}):")
        print(f"  {spec.display()}")
        print()
        print("(dry-run; no process spawned, no state file written)")
        return 0
    # Idempotent state initialisation, then supervise to terminal.
    gate_initialise(plan, store)
    result = supervise(
        store,
        name=args.name,
        command=spec,
        deadline_seconds=args.deadline_seconds,
    )
    print()
    print(
        f"gate {args.name} -> {result.state.status} "
        f"(exit_code={result.state.exit_code}, timed_out={result.timed_out})"
    )
    print(f"log: {plan.log_path}")
    return 0 if result.state.status == "passed" else 1


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
    if not plan.log_path.exists():
        print(f"(no log at {plan.log_path})")
        return 1
    if not text:
        print(f"(log file is empty: {plan.log_path})")
        return 0
    print(text)
    return 0


def cmd_gate_stop(args: argparse.Namespace) -> int:
    _, store = _config_and_store(args.config)
    try:
        state = gate_stop(store, name=args.name)
    except FileNotFoundError as exc:
        print(f"gate stop error: {exc}", file=sys.stderr)
        return 2
    print(f"gate {args.name} -> {state.status}")
    return 0


def cmd_gate_summarise(args: argparse.Namespace) -> int:
    _, store = _config_and_store(args.config)
    summary = gate_summarise(store, name=args.name)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "passed" else 1


def _build_command_spec(command: str, mode: str | None) -> CommandSpec:
    """Map CLI args to a CommandSpec.

    ``mode='argv'`` interprets ``command`` as JSON-encoded argv list so the
    operator can supply non-shell-evaluated commands from the CLI without
    juggling quoting. Other modes pass the snippet straight to from_string().
    """
    if mode == "argv":
        try:
            argv = json.loads(command)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--mode argv requires JSON array argv, got: {exc}") from exc
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise SystemExit("--mode argv requires a JSON array of strings")
        return from_argv(argv)
    return from_string(command, mode=mode)  # type: ignore[arg-type]


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


def cmd_pr_merge(args: argparse.Namespace) -> int:
    config, store = _config_and_store(args.config)
    try:
        plan = plan_pr_merge(
            store,
            pr_number=args.pr,
            head_sha=args.head_sha,
            repo=config.target.repo,
        )
    except PRMergeError as exc:
        print(f"merge refused: {exc}", file=sys.stderr)
        return 2

    cmd = render_merge_command(plan)
    print("merge plan:")
    print(f"  pr:        #{plan.pr_number}")
    print(f"  head_sha:  {plan.head_sha}")
    print(f"  verdict:   {plan.verdict}")
    print(f"  ts:        {plan.merge_ready_ts}")
    print(f"  command:   {cmd}")
    if args.dry_run:
        print()
        print("(dry-run; nothing executed)")
        return 0
    if not args.execute:
        print()
        print("note: not executed. Pass --execute together with --operator-confirmed to run.")
        return 0
    if not args.operator_confirmed:
        print(
            "refused: --execute requires --operator-confirmed.",
            file=sys.stderr,
        )
        return 2
    if not gh_available():
        print("refused: 'gh' is not on PATH; cannot execute merge.", file=sys.stderr)
        return 2
    # Execution is the operator's act; record it in the audit log first.
    store.append_event(
        {
            "kind": "pr.merge.execute",
            "pr": plan.pr_number,
            "head_sha": plan.head_sha,
            "command": cmd,
        }
    )
    import subprocess  # local import; this is the only place in the CLI that runs gh
    rc = subprocess.run(plan.argv, check=False).returncode
    if rc != 0:
        print(f"gh exited {rc}; merge may have failed", file=sys.stderr)
    return rc


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

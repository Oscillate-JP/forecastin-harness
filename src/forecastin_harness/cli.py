"""``forecastin-harness`` command-line entry point.

The CLI is intentionally thin: each subcommand wires arguments through to the
relevant module. Plan-only is the default for every state-changing command;
the operator must pass an explicit ``--apply`` to mutate state or contact
GitHub. v0.3 inverts the v0.2 ``--dry-run`` semantics so the CLI is
fail-closed: a mistyped command without ``--apply`` never mutates state.

Every command emits human-readable text on stdout and a non-zero exit code
on validation failure so it is safe to compose in a shell pipeline.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import ConfigError, HarnessConfig, load_config
from .controller import (
    TaskPacketLoadError,
    diagnose,
    enumerate_processes,
    probe_git,
    render_doctor_report,
    render_validation_report,
    validate_packet_file,
)
from .lane_classifier import classify_changes, render_classification
from .merge_evidence import (
    render_merge_evidence,
    verify_merge_payload,
    verify_reachable_from_main,
)
from .push_watchdog import (
    DEFAULT_AGE_THRESHOLD_SECONDS,
    DEFAULT_OUTPUT_SILENCE_SECONDS,
    evaluate_processes as evaluate_processes_for_watchdog,
    render_watchdog_report,
)
from .root_guard import DEFAULT_EXPECTED_ROOT, DEFAULT_FORBIDDEN_ROOTS, check_root
from .runtime_provenance import (
    evaluate_mounts,
    load_mounts_from_docker_inspect,
    render_provenance_report,
)
from .task_packet_templates import GENERATORS
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

    p_l_create = p_lanes_sub.add_parser("create", help="Create a new lane (plan-only by default).")
    p_l_create.add_argument("--config", required=True)
    p_l_create.add_argument("--name", required=True)
    p_l_create.add_argument("--task", required=True, help="Task id, e.g. FOR-235.")
    p_l_create.add_argument("--scope", required=True)
    p_l_create.add_argument("--owner", default=None)
    _add_apply(p_l_create)
    p_l_create.set_defaults(func=cmd_lanes_create)

    p_l_status = p_lanes_sub.add_parser("status", help="Print a lane status report.")
    p_l_status.add_argument("--config", required=True)
    p_l_status.set_defaults(func=cmd_lanes_status)

    p_l_retire = p_lanes_sub.add_parser("retire", help="Retire a lane.")
    p_l_retire.add_argument("--config", required=True)
    p_l_retire.add_argument("--name", required=True)
    _add_apply(p_l_retire)
    p_l_retire.set_defaults(func=cmd_lanes_retire)

    # task render
    p_task = sub.add_parser("task", help="Task packet operations.")
    p_task_sub = p_task.add_subparsers(dest="task_cmd", required=True)
    p_task_render = p_task_sub.add_parser("render", help="Render an agent prompt from a task packet.")
    p_task_render.add_argument("--task-file", required=True)
    p_task_render.add_argument("--agent", required=True, choices=SUPPORTED_AGENTS)
    p_task_render.add_argument(
        "--templates-dir",
        default=None,
        help=(
            "Directory holding <agent>_prompt.md templates. Default: load "
            "from the installed forecastin_harness.templates package via "
            "importlib.resources (works from a wheel install, no repo layout "
            "assumption)."
        ),
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
    _add_apply(p_g_start)
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
    _add_apply(p_pr_check)
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
    _add_apply(p_pr_merge)
    p_pr_merge.set_defaults(func=cmd_pr_merge)

    # controller (read-only safety checks)
    p_ctrl = sub.add_parser(
        "controller",
        help="Controller safety checks (validate task packets, doctor environment).",
    )
    p_ctrl_sub = p_ctrl.add_subparsers(dest="controller_cmd", required=True)

    p_ctrl_validate = p_ctrl_sub.add_parser(
        "validate",
        help="Validate a task packet against controller safety rules.",
    )
    p_ctrl_validate.add_argument(
        "--task-file",
        required=True,
        help="Path to the YAML task packet to validate.",
    )
    p_ctrl_validate.set_defaults(func=cmd_controller_validate)

    p_ctrl_doctor = p_ctrl_sub.add_parser(
        "doctor",
        help="Report active processes, current branch, and cwd-vs-target match.",
    )
    p_ctrl_doctor.add_argument(
        "--config",
        default=None,
        help="Optional adapter YAML; needed to compare cwd against target.path.",
    )
    p_ctrl_doctor.set_defaults(func=cmd_controller_doctor)

    # preflight (root, runtime provenance, lane policy, watchdog, merge evidence)
    p_pre = sub.add_parser(
        "preflight",
        help="Pre-action safety checks (root, runtime, lane-policy, watchdog, merge-evidence).",
    )
    p_pre_sub = p_pre.add_subparsers(dest="preflight_cmd", required=True)

    p_pre_root = p_pre_sub.add_parser(
        "root",
        help="Refuse to proceed if cwd is not the expected coordinator root.",
    )
    p_pre_root.add_argument(
        "--expected",
        default=str(DEFAULT_EXPECTED_ROOT),
        help=f"Expected coordinator root. Default: {DEFAULT_EXPECTED_ROOT}",
    )
    p_pre_root.add_argument(
        "--forbidden",
        action="append",
        default=None,
        help=(
            "Additional forbidden root. Repeat for multiple. "
            f"Always-forbidden defaults: {[str(p) for p in DEFAULT_FORBIDDEN_ROOTS]}"
        ),
    )
    p_pre_root.set_defaults(func=cmd_preflight_root)

    p_pre_runtime = p_pre_sub.add_parser(
        "runtime",
        help="Verify Docker bind mounts originate from the coordinator root.",
    )
    p_pre_runtime.add_argument(
        "--inspect-json",
        default=None,
        help=(
            "Path to a JSON file containing the output of `docker inspect <id>...`. "
            "When omitted, the CLI reads JSON from stdin."
        ),
    )
    p_pre_runtime.add_argument(
        "--allowed-root",
        action="append",
        default=None,
        help=(
            "Allowed bind-source root. Repeat for multiple. "
            f"Default: {DEFAULT_EXPECTED_ROOT}"
        ),
    )
    p_pre_runtime.add_argument(
        "--forbidden-root",
        action="append",
        default=None,
        help=(
            "Forbidden bind-source root. Repeat for multiple. "
            f"Always-forbidden defaults: {[str(p) for p in DEFAULT_FORBIDDEN_ROOTS]}"
        ),
    )
    p_pre_runtime.add_argument(
        "--service",
        action="append",
        default=None,
        help="Required service to verify. Repeat for multiple. Default: backend, frontend.",
    )
    p_pre_runtime.set_defaults(func=cmd_preflight_runtime)

    p_pre_lane = p_pre_sub.add_parser(
        "lane-policy",
        help="Classify a changed-files list into a gate kind.",
    )
    p_pre_lane.add_argument(
        "--files",
        default=None,
        help=(
            "Path to a newline-delimited file listing changed paths. "
            "When omitted, reads from stdin (one path per line)."
        ),
    )
    p_pre_lane.set_defaults(func=cmd_preflight_lane_policy)

    p_pre_watch = p_pre_sub.add_parser(
        "watchdog",
        help="Detect hung pre-push pytest / no-output processes.",
    )
    p_pre_watch.add_argument(
        "--age-threshold-seconds",
        type=int,
        default=DEFAULT_AGE_THRESHOLD_SECONDS,
        help=f"Default: {DEFAULT_AGE_THRESHOLD_SECONDS}s.",
    )
    p_pre_watch.add_argument(
        "--silence-threshold-seconds",
        type=int,
        default=DEFAULT_OUTPUT_SILENCE_SECONDS,
        help=f"Default: {DEFAULT_OUTPUT_SILENCE_SECONDS}s.",
    )
    p_pre_watch.set_defaults(func=cmd_preflight_watchdog)

    p_pre_merge = p_pre_sub.add_parser(
        "merge-evidence",
        help=(
            "Verify a PR's merge using gh pr view JSON. Refuses banner text. "
            "Optionally verifies reachability from origin/main."
        ),
    )
    p_pre_merge.add_argument(
        "--pr",
        type=int,
        required=True,
        help="PR number to verify.",
    )
    p_pre_merge.add_argument(
        "--repo",
        required=True,
        help="GitHub <owner>/<repo>, e.g. Oscillate-JP/Forecastin",
    )
    p_pre_merge.add_argument(
        "--check-reachability",
        default=None,
        help=(
            "Optional path to the local git checkout. When set, also runs "
            "`git merge-base --is-ancestor <merge_commit> origin/main`."
        ),
    )
    p_pre_merge.add_argument(
        "--main-ref",
        default="origin/main",
        help="Default: origin/main",
    )
    p_pre_merge.set_defaults(func=cmd_preflight_merge_evidence)

    # task generate (templates)
    p_task_gen = p_task_sub.add_parser(
        "generate",
        help="Generate a starter task packet for a known drain shape.",
    )
    p_task_gen.add_argument(
        "--kind",
        required=True,
        choices=sorted(GENERATORS.keys()),
        help="Drain shape to generate.",
    )
    p_task_gen.add_argument(
        "--target-repo",
        required=True,
        help="Path to target repo working copy (becomes packet.target_repo).",
    )
    p_task_gen.add_argument(
        "--worktree",
        required=True,
        help="Path to lane worktree (becomes packet.worktree).",
    )
    p_task_gen.add_argument(
        "--out",
        default=None,
        help="Output path for generated YAML. When omitted, writes to stdout.",
    )
    p_task_gen.add_argument(
        "--option",
        action="append",
        default=None,
        help=(
            "Extra generator option as key=value. Repeat for multiple. "
            "See task_packet_templates docstring for the per-kind option set."
        ),
    )
    p_task_gen.set_defaults(func=cmd_task_generate)

    return parser


def _add_apply(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply the planned change. Without this flag the command is plan-only.",
    )


# ---------------- command implementations ----------------


def cmd_init(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    target = Path(args.target_repo).resolve()
    config_target = Path(config.target.path).resolve()
    if target != config_target:
        # Compare after .resolve() so symlinks and case-only differences
        # (Windows) collapse to the same canonical path before mismatch errors.
        print(
            "target repo path does not match config.target.path:\n"
            f"  --target-repo:      {target}\n"
            f"  config.target.path: {config_target}\n"
            "Update one so they agree before re-running init.",
            file=sys.stderr,
        )
        return 2
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
    if not args.apply:
        print()
        print("(plan-only; pass --apply to create the lane)")
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
    if not args.apply:
        new = plan_retire(store, args.name)
        print(f"would retire lane: {new.name}")
        print(f"  branch:    {new.branch}")
        print(f"  worktree:  {new.worktree}")
        print("note: harness does NOT remove the worktree from disk; operator does that manually.")
        print()
        print("(plan-only; pass --apply to retire the lane)")
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
    # When --templates-dir is unset, pass None straight through so render_prompt
    # loads the agent template from the installed package via importlib.resources.
    templates_dir = Path(args.templates_dir) if args.templates_dir else None
    try:
        text = render_prompt(packet, args.agent, templates_dir=templates_dir)
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
    if not args.apply:
        print()
        print(f"command (rendered, mode={spec.mode}):")
        print(f"  {spec.display()}")
        print()
        print("(plan-only; pass --apply to spawn the gate process)")
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
    config, store = _config_and_store(args.config)
    if not args.apply:
        print(plan_pr_check(pr_number=args.pr, head_sha=args.head_sha))
        print()
        print("(plan-only; pass --apply to invoke 'gh' against GitHub)")
        return 0
    if not gh_available():
        print(plan_pr_check(pr_number=args.pr, head_sha=args.head_sha))
        print()
        print("warning: 'gh' is not on PATH; live PR check skipped.")
        return 1
    # Pin the gate to the configured target so 'gh' cannot fall back to the
    # cwd's git origin; pass main_branch so pr-base-branch can verify the PR
    # is heading at the expected branch.
    result = run_pr_check(
        store,
        pr_number=args.pr,
        head_sha=args.head_sha,
        repo=config.target.repo,
        main_branch=config.target.main_branch,
    )
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
    if not args.apply:
        print()
        print(
            "(plan-only; pass --apply --execute --operator-confirmed to actually merge)"
        )
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


def cmd_controller_validate(args: argparse.Namespace) -> int:
    """Validate a task packet. Exit 0 on PASS, 1 on FAIL, 2 on load error."""
    try:
        report = validate_packet_file(args.task_file)
    except TaskPacketLoadError as exc:
        print(f"task packet load error: {exc}", file=sys.stderr)
        return 2
    print(render_validation_report(report))
    return 0 if report.ok else 1


def cmd_controller_doctor(args: argparse.Namespace) -> int:
    """Report process and git state. Exit 0 always (read-only)."""
    target_path: Path | None = None
    if args.config is not None:
        try:
            config = load_config(args.config)
            target_path = config.target.path
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
    cwd = Path.cwd()
    rows = enumerate_processes()
    report = diagnose(
        cwd=cwd,
        target_path=target_path,
        processes=rows,
        git_probe=probe_git,
    )
    print(render_doctor_report(report))
    return 0


def _config_and_store(config_path: str) -> tuple[HarnessConfig, StateStore]:
    config = load_config(config_path)
    store = StateStore(config.state_path)
    store.ensure_layout()
    return config, store


# ---------------- preflight commands ----------------


def cmd_preflight_root(args: argparse.Namespace) -> int:
    """Refuse to proceed if cwd is not the expected coordinator root."""
    expected = Path(args.expected)
    forbidden_extra = [Path(p) for p in (args.forbidden or [])]
    forbidden = list(DEFAULT_FORBIDDEN_ROOTS) + forbidden_extra
    result = check_root(Path.cwd(), expected=expected, forbidden=forbidden)
    print(result.message)
    return 0 if result.ok else 2


def cmd_preflight_runtime(args: argparse.Namespace) -> int:
    """Verify Docker bind mounts come from the coordinator root."""
    if args.inspect_json:
        raw = Path(args.inspect_json).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        print("preflight runtime: no JSON provided on --inspect-json or stdin", file=sys.stderr)
        return 2
    mounts = load_mounts_from_docker_inspect(raw)
    allowed = [Path(p) for p in (args.allowed_root or [str(DEFAULT_EXPECTED_ROOT)])]
    forbidden_extra = [Path(p) for p in (args.forbidden_root or [])]
    forbidden = list(DEFAULT_FORBIDDEN_ROOTS) + forbidden_extra
    services = tuple(args.service or ("backend", "frontend"))
    report = evaluate_mounts(
        mounts,
        allowed_roots=allowed,
        forbidden_roots=forbidden,
        required_services=services,
    )
    print(render_provenance_report(report))
    return 0 if report.runtime_evidence_valid else 2


def cmd_preflight_lane_policy(args: argparse.Namespace) -> int:
    """Classify a changed-files list into a gate kind."""
    if args.files:
        raw = Path(args.files).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    paths = [line.strip() for line in raw.splitlines() if line.strip()]
    report = classify_changes(paths)
    print(render_classification(report))
    if report.kind == "empty":
        return 2
    return 0


def cmd_preflight_watchdog(args: argparse.Namespace) -> int:
    """Detect hung pre-push pytest / no-output processes."""
    rows = enumerate_processes()
    report = evaluate_processes_for_watchdog(
        rows,
        age_threshold_seconds=args.age_threshold_seconds,
        output_silence_seconds=args.silence_threshold_seconds,
    )
    print(render_watchdog_report(report))
    return 0 if report.ok else 2


def cmd_preflight_merge_evidence(args: argparse.Namespace) -> int:
    """Verify a PR's merge using gh pr view JSON. Refuse banner text."""
    try:
        out = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(args.pr),
                "--repo",
                args.repo,
                "--json",
                "number,state,mergedAt,mergeCommit,url",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        print(f"preflight merge-evidence: gh invocation failed: {exc}", file=sys.stderr)
        return 2
    if out.returncode != 0:
        print(out.stderr, file=sys.stderr)
        return 2
    evidence = verify_merge_payload(out.stdout, expected_pr=args.pr)
    reachable: bool | None = None
    if args.check_reachability and evidence.merge_commit_sha:
        reachable = verify_reachable_from_main(
            Path(args.check_reachability),
            evidence.merge_commit_sha,
            main_ref=args.main_ref,
        )
    print(render_merge_evidence(evidence, reachable_from_main=reachable))
    if not evidence.ok:
        return 2
    if reachable is False:
        return 2
    return 0


# ---------------- task generate ----------------


def cmd_task_generate(args: argparse.Namespace) -> int:
    """Generate a starter task packet for a known drain shape."""
    import yaml

    fn = GENERATORS[args.kind]
    extra: dict[str, str] = {}
    for kv in args.option or []:
        if "=" not in kv:
            print(f"--option must be key=value, got {kv!r}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        extra[k.strip()] = v.strip()
    try:
        packet = fn(target_repo=args.target_repo, worktree=args.worktree, **extra)
    except TypeError as exc:
        print(f"missing required option(s) for kind {args.kind!r}: {exc}", file=sys.stderr)
        return 2
    rendered = yaml.safe_dump(packet, sort_keys=False, default_flow_style=False)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(rendered)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

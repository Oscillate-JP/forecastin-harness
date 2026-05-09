"""Controller safety checks: ``validate`` and ``doctor``.

Both commands are read-only by design. They exist because the live FOR-220
run exposed a class of failures the harness had no defence against:

* an agent drifted from a feature lane into gate repair;
* long-running shells became unmanaged and produced confusing pytest output;
* "bypass-permissions" mode appeared in the loop;
* official gate validation was confused with diagnostic pytest enumeration.

``controller validate`` rejects task packets that would let any of those
patterns recur. ``controller doctor`` reports the live environment so an
operator can spot orphan test processes and cwd/repo confusion *before*
firing a lane.

Design choices:

* Pure-function validators return a :class:`ValidationReport`. The CLI is
  a thin wrapper that prints findings and exits non-zero if any are
  errors. The same function is reused in tests.
* Process enumeration is injectable. ``diagnose`` accepts an iterable of
  :class:`ProcessRow` so tests can hand in a fixed list without spawning
  shells. The default enumerator shells out to ``tasklist`` (Windows) or
  ``ps`` (POSIX) with ``shell=False`` and swallows missing-binary errors.
* Git probing is also injectable. The default probe runs ``git`` with
  ``shell=False`` and returns ``(branch, dirty)``; on failure both are
  ``None`` rather than raising.
* No network. No secrets. No mutation. ``doctor`` runs offline.
"""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

import yaml

#: Lane types the controller recognises. ``feature`` and ``gate-fix`` are
#: the two we actively validate as separable; ``review`` is for read-only
#: review lanes; ``controller`` is reserved for harness-meta work.
ALLOWED_LANE_TYPES: tuple[str, ...] = ("feature", "gate-fix", "review", "controller")

#: Path prefixes that mark a file as gate / CI / hook plumbing.
GATE_REPAIR_PATH_PREFIXES: tuple[str, ...] = (
    "scripts/ci.sh",
    "scripts/claude/",
    "scripts/install-hooks",
    ".pre-commit-config",
    ".github/workflows/",
    "infrastructure/docker/",
    "Makefile",
    "backend/pytest.ini",
    "backend/conftest.py",
    "tests/conftest.py",
)

#: Path prefixes that mark a file as feature / product code.
FEATURE_PATH_PREFIXES: tuple[str, ...] = (
    "backend/app/",
    "frontend/src/",
    "frontend/public/",
    "src/",
)

#: Tokens that, if they appear *anywhere* in a packet's stringy fields,
#: indicate the operator is trying to authorise hook bypass. The check
#: is intentionally substring-based so encoded variants still match.
BYPASS_TOKENS: tuple[str, ...] = (
    "--no-verify",
    "bypass-permissions",
    "bypass_permissions",
    "ignore-hooks",
    "skip-hooks",
    "--no-gpg-sign",
)

#: Final-report sub-headings the packet must promise to deliver. Matched
#: case-insensitively as substrings so phrasing is up to the author.
REQUIRED_FINAL_REPORT_TOPICS: tuple[str, ...] = (
    "changed files",
    "tests",
    "remaining risks",
)

#: Pytest processes older than this on the wall clock are flagged as
#: possible orphans by ``doctor``. 5 minutes was chosen because anything
#: shorter is plausible normal use; longer than that without operator
#: knowledge typically means a stranded background shell.
LONG_RUNTIME_THRESHOLD_SECONDS: int = 300

_PYTEST_PATTERNS: tuple[str, ...] = ("pytest", "py.test")
_PYTHON_NAMES: tuple[str, ...] = (
    "python",
    "python.exe",
    "python3",
    "python3.11",
    "python3.12",
    "py.exe",
)
_SHELL_NAMES: tuple[str, ...] = (
    "bash",
    "bash.exe",
    "sh",
    "zsh",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "cmd.exe",
)


# ---------------- data ----------------


Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class ValidationFinding:
    """A single safety finding against a task packet."""

    code: str
    message: str
    severity: Severity = "error"


@dataclass(frozen=True)
class ValidationReport:
    """Result of validating one task packet."""

    packet_path: Path
    findings: tuple[ValidationFinding, ...] = ()

    @property
    def errors(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ProcessRow:
    """A single row in a tasklist/ps style process enumeration.

    ``runtime_seconds`` is ``None`` when the underlying enumerator could
    not parse it; doctor treats unknown runtime as "not long-running"
    rather than guessing.
    """

    pid: int
    name: str
    cmdline: str
    runtime_seconds: int | None = None


@dataclass(frozen=True)
class DoctorReport:
    """Snapshot of the local environment for operator inspection."""

    cwd: Path
    target_path: Path | None
    cwd_matches_target: bool
    current_branch: str | None
    dirty: bool | None
    python_processes: tuple[ProcessRow, ...] = ()
    pytest_processes: tuple[ProcessRow, ...] = ()
    shell_processes: tuple[ProcessRow, ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


class TaskPacketLoadError(ValueError):
    """Raised when the controller cannot read a task packet from disk."""


# ---------------- validate ----------------


def load_packet_dict(path: str | Path) -> Mapping[str, Any]:
    """Load a YAML task packet without invoking the strict prompts validator.

    The controller's job is to inspect raw packet contents and surface
    problems by code, so it must not error out in
    :class:`prompts.TaskPacketError` before reaching the safety checks.
    """
    p = Path(path)
    if not p.is_file():
        raise TaskPacketLoadError(f"task packet not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TaskPacketLoadError(f"invalid YAML in {p}: {exc}") from exc
    if raw is None:
        raise TaskPacketLoadError(f"task packet is empty: {p}")
    if not isinstance(raw, Mapping):
        raise TaskPacketLoadError(
            f"task packet root must be a mapping in {p}, got {type(raw).__name__}"
        )
    return raw


def validate_packet(
    packet: Mapping[str, Any],
    *,
    packet_path: Path | str = Path("<memory>"),
) -> ValidationReport:
    """Run every controller safety check against an already-loaded packet."""
    findings: list[ValidationFinding] = []

    findings.extend(_check_objective(packet))
    findings.extend(_check_target_repo(packet))
    findings.extend(_check_worktree(packet))
    findings.extend(_check_official_gate(packet))
    findings.extend(_check_max_runtime(packet))
    findings.extend(_check_forbidden_files(packet))
    findings.extend(_check_final_report(packet))
    findings.extend(_check_lane_type_and_mix(packet))
    findings.extend(_check_merge_authority(packet))
    findings.extend(_check_bypass_permissions(packet))

    return ValidationReport(packet_path=Path(packet_path), findings=tuple(findings))


def validate_packet_file(path: str | Path) -> ValidationReport:
    """Convenience: load + validate in one call. Raises on YAML/IO errors."""
    p = Path(path)
    raw = load_packet_dict(p)
    return validate_packet(raw, packet_path=p)


def _check_objective(packet: Mapping[str, Any]) -> list[ValidationFinding]:
    mission = packet.get("mission")
    if not isinstance(mission, str) or not mission.strip():
        return [
            ValidationFinding(
                "missing_objective",
                "mission/objective is missing or empty",
            )
        ]
    return []


def _check_target_repo(packet: Mapping[str, Any]) -> list[ValidationFinding]:
    target = packet.get("target_repo")
    if not isinstance(target, str) or not target.strip():
        return [
            ValidationFinding(
                "missing_target_repo",
                "target_repo is missing or empty",
            )
        ]
    return []


def _check_worktree(packet: Mapping[str, Any]) -> list[ValidationFinding]:
    worktree = packet.get("worktree")
    if not isinstance(worktree, str) or not worktree.strip():
        return [
            ValidationFinding(
                "missing_worktree",
                "worktree is missing or empty",
            )
        ]
    return []


def _check_official_gate(packet: Mapping[str, Any]) -> list[ValidationFinding]:
    gate = packet.get("official_gate_command")
    if not isinstance(gate, str) or not gate.strip():
        return [
            ValidationFinding(
                "missing_official_gate",
                "official_gate_command is missing or empty",
            )
        ]
    return []


def _check_max_runtime(packet: Mapping[str, Any]) -> list[ValidationFinding]:
    runtime = packet.get("max_runtime_minutes")
    # bool is a subclass of int — disallow it explicitly so True/False
    # cannot pose as a runtime budget.
    if isinstance(runtime, bool) or not isinstance(runtime, int) or runtime <= 0:
        return [
            ValidationFinding(
                "missing_max_runtime",
                "max_runtime_minutes must be a positive integer",
            )
        ]
    return []


def _check_forbidden_files(packet: Mapping[str, Any]) -> list[ValidationFinding]:
    files = packet.get("files")
    if not isinstance(files, Mapping):
        return [
            ValidationFinding(
                "missing_forbidden_files",
                "files.forbidden is missing (files block must declare forbidden paths)",
            )
        ]
    forbidden = files.get("forbidden")
    if not isinstance(forbidden, list) or not forbidden:
        return [
            ValidationFinding(
                "missing_forbidden_files",
                "files.forbidden must be a non-empty list",
            )
        ]
    return []


def _check_final_report(packet: Mapping[str, Any]) -> list[ValidationFinding]:
    report = packet.get("final_report")
    if not isinstance(report, list) or not report:
        return [
            ValidationFinding(
                "missing_final_report_contract",
                "final_report must be a non-empty list naming the sections "
                "the agent must deliver (changed files, tests, remaining risks)",
            )
        ]
    joined = " ".join(str(item).lower() for item in report)
    out: list[ValidationFinding] = []
    for required in REQUIRED_FINAL_REPORT_TOPICS:
        if required not in joined:
            slug = required.replace(" ", "_")
            out.append(
                ValidationFinding(
                    f"final_report_missing_{slug}",
                    f"final_report contract must promise '{required}'",
                )
            )
    return out


def _check_lane_type_and_mix(packet: Mapping[str, Any]) -> list[ValidationFinding]:
    lane_type = packet.get("lane_type")
    out: list[ValidationFinding] = []
    if lane_type not in ALLOWED_LANE_TYPES:
        out.append(
            ValidationFinding(
                "invalid_lane_type",
                f"lane_type must be one of {ALLOWED_LANE_TYPES}, got {lane_type!r}",
            )
        )
        # Skip the mix check if we don't even know the lane type.
        return out

    if lane_type not in ("feature", "gate-fix"):
        return out

    files = packet.get("files")
    allowed: list[str] = []
    if isinstance(files, Mapping):
        raw_allowed = files.get("allowed")
        if isinstance(raw_allowed, list):
            allowed = [str(p) for p in raw_allowed if isinstance(p, str)]

    feature_paths = [p for p in allowed if _matches_any(p, FEATURE_PATH_PREFIXES)]
    gate_paths = [p for p in allowed if _matches_any(p, GATE_REPAIR_PATH_PREFIXES)]

    if feature_paths and gate_paths:
        out.append(
            ValidationFinding(
                "mixed_feature_and_gate_fix",
                "task mixes feature paths and gate-fix paths in files.allowed; "
                "split into two PRs. "
                f"feature: {feature_paths}; gate-fix: {gate_paths}",
            )
        )
    elif lane_type == "feature" and gate_paths and not feature_paths:
        out.append(
            ValidationFinding(
                "feature_lane_only_touches_gate_paths",
                f"lane_type=feature but files.allowed lists only gate-repair paths {gate_paths}",
            )
        )
    elif lane_type == "gate-fix" and feature_paths and not gate_paths:
        out.append(
            ValidationFinding(
                "gate_fix_lane_only_touches_feature_paths",
                f"lane_type=gate-fix but files.allowed lists only feature paths {feature_paths}",
            )
        )

    return out


def _check_merge_authority(packet: Mapping[str, Any]) -> list[ValidationFinding]:
    merge_authority = packet.get("merge_authority")
    if merge_authority != "human-only":
        return [
            ValidationFinding(
                "autonomous_merge_not_allowed",
                f"merge_authority must equal 'human-only', got {merge_authority!r}",
            )
        ]
    return []


def _check_bypass_permissions(packet: Mapping[str, Any]) -> list[ValidationFinding]:
    out: list[ValidationFinding] = []
    flag = packet.get("no_bypass_permissions")
    if flag is not True:
        out.append(
            ValidationFinding(
                "bypass_permissions_not_disallowed",
                f"no_bypass_permissions must equal true, got {flag!r}",
            )
        )

    for path, value in _walk_strings(packet):
        haystack = value.lower()
        for token in BYPASS_TOKENS:
            if token in haystack:
                out.append(
                    ValidationFinding(
                        "bypass_token_detected",
                        f"bypass token {token!r} detected at {path}",
                    )
                )
                break
    return out


def _walk_strings(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    """Yield (dotted-path, string) for every string anywhere in ``value``."""
    if isinstance(value, str):
        yield path or "(root)", value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            child = f"{path}.{k}" if path else str(k)
            yield from _walk_strings(v, child)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            child = f"{path}[{i}]"
            yield from _walk_strings(v, child)


def _matches_any(path: str, prefixes: Iterable[str]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


# ---------------- doctor ----------------


GitProbe = Callable[[Path], "tuple[str | None, bool | None]"]
ProcessEnumerator = Callable[[], "list[ProcessRow]"]


def diagnose(
    *,
    cwd: Path,
    target_path: Path | None,
    processes: Iterable[ProcessRow],
    git_probe: GitProbe | None = None,
) -> DoctorReport:
    """Pure synthesis function: produce a :class:`DoctorReport` from inputs.

    Tests inject ``processes`` (a fixed list) and ``git_probe`` (a stub
    returning ``(branch, dirty)``) so the function never touches the live
    filesystem or the live process table.
    """
    rows = list(processes)

    pytest_rows = tuple(r for r in rows if _row_is_pytest(r))
    # Exclude rows already classified as pytest from the python bucket so
    # the operator does not see a process counted twice. The check is by
    # PID, not by row identity, so any synthetic enumerator works.
    pytest_pids = {r.pid for r in pytest_rows}
    python_rows = tuple(
        r for r in rows if r.pid not in pytest_pids and _row_is_python(r)
    )
    shell_rows = tuple(r for r in rows if _row_is_shell(r))

    cwd_matches = False
    if target_path is not None:
        try:
            cwd_matches = cwd.resolve() == target_path.resolve()
        except OSError:
            cwd_matches = False

    branch: str | None = None
    dirty: bool | None = None
    if git_probe is not None:
        branch, dirty = git_probe(cwd)

    warnings: list[str] = []

    long_pytests = [
        r
        for r in pytest_rows
        if r.runtime_seconds is not None
        and r.runtime_seconds >= LONG_RUNTIME_THRESHOLD_SECONDS
    ]
    if len(long_pytests) >= 2:
        warnings.append(
            f"{len(long_pytests)} pytest processes have run for "
            f">= {LONG_RUNTIME_THRESHOLD_SECONDS}s — possible orphans "
            f"(PIDs: {[r.pid for r in long_pytests]})"
        )

    if target_path is not None and not cwd_matches:
        warnings.append(
            f"cwd ({cwd}) does not match configured target ({target_path})"
        )

    return DoctorReport(
        cwd=cwd,
        target_path=target_path,
        cwd_matches_target=cwd_matches,
        current_branch=branch,
        dirty=dirty,
        python_processes=python_rows,
        pytest_processes=pytest_rows,
        shell_processes=shell_rows,
        warnings=tuple(warnings),
    )


def _row_is_pytest(row: ProcessRow) -> bool:
    haystack = f"{row.name} {row.cmdline}".lower()
    return any(p in haystack for p in _PYTEST_PATTERNS)


def _row_is_python(row: ProcessRow) -> bool:
    name = row.name.lower()
    return any(name == p or name.startswith(p + " ") for p in _PYTHON_NAMES)


def _row_is_shell(row: ProcessRow) -> bool:
    name = row.name.lower()
    return any(name == s for s in _SHELL_NAMES)


# ---------------- live process / git enumerators ----------------


def enumerate_processes() -> list[ProcessRow]:
    """Best-effort live enumeration. Never raises; returns ``[]`` on failure."""
    # ``os.name`` is checked at runtime; Pyright's type-narrowing fires on
    # the literal "nt" branch which is fine — we only need either branch
    # to execute on the host the operator is on.
    name = os.name
    if name == "nt":
        return _enum_via_tasklist()
    return _enum_via_ps()


def _enum_via_tasklist() -> list[ProcessRow]:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/V"],
            shell=False,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return []
    rows: list[ProcessRow] = []
    reader = csv.reader(io.StringIO(out))
    header = next(reader, None)
    if not header:
        return []
    # tasklist /V columns: Image Name, PID, Session Name, Session#,
    # Mem Usage, Status, User Name, CPU Time, Window Title.
    for row in reader:
        if len(row) < 8:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        rows.append(
            ProcessRow(
                pid=pid,
                name=row[0],
                cmdline=row[-1] if row[-1] else row[0],
                runtime_seconds=_parse_hms(row[7]),
            )
        )
    return rows


def _enum_via_ps() -> list[ProcessRow]:
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,etime,comm,args"],
            shell=False,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return []
    rows: list[ProcessRow] = []
    lines = out.splitlines()[1:]
    for line in lines:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        rows.append(
            ProcessRow(
                pid=pid,
                name=parts[2],
                cmdline=parts[3],
                runtime_seconds=_parse_etime(parts[1]),
            )
        )
    return rows


_HMS_RE = re.compile(r"^\s*(\d+):(\d{2}):(\d{2})(?:\.\d+)?\s*$")
_ETIME_DAYS_RE = re.compile(r"^\s*(\d+)-(\d+):(\d{2}):(\d{2})\s*$")
_ETIME_HMS_RE = re.compile(r"^\s*(\d+):(\d{2}):(\d{2})\s*$")
_ETIME_MS_RE = re.compile(r"^\s*(\d+):(\d{2})\s*$")


def _parse_hms(s: str) -> int | None:
    """Parse ``HH:MM:SS`` (tasklist CPU Time) into seconds."""
    m = _HMS_RE.match(s)
    if not m:
        return None
    h, mm, ss = m.groups()
    return int(h) * 3600 + int(mm) * 60 + int(ss)


def _parse_etime(s: str) -> int | None:
    """Parse a POSIX ``ps etime`` string (``[[DD-]HH:]MM:SS``) into seconds."""
    m = _ETIME_DAYS_RE.match(s)
    if m:
        d, h, mm, ss = m.groups()
        return int(d) * 86400 + int(h) * 3600 + int(mm) * 60 + int(ss)
    m = _ETIME_HMS_RE.match(s)
    if m:
        h, mm, ss = m.groups()
        return int(h) * 3600 + int(mm) * 60 + int(ss)
    m = _ETIME_MS_RE.match(s)
    if m:
        mm, ss = m.groups()
        return int(mm) * 60 + int(ss)
    return None


def probe_git(cwd: Path) -> tuple[str | None, bool | None]:
    """Return ``(branch, dirty)`` for the git checkout at ``cwd``.

    Both values are ``None`` when ``git`` is missing or ``cwd`` is not a
    checkout. ``dirty`` is ``True`` when ``git status --porcelain`` emits
    any output, ``False`` otherwise.
    """
    branch: str | None = None
    dirty: bool | None = None
    try:
        branch_out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if branch_out.returncode == 0:
            branch = branch_out.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None, None

    try:
        status_out = subprocess.run(
            ["git", "-C", str(cwd), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if status_out.returncode == 0:
            dirty = bool(status_out.stdout.strip())
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        dirty = None

    return branch, dirty


# ---------------- printable rendering ----------------


def render_validation_report(report: ValidationReport) -> str:
    """Human-readable rendering used by the CLI."""
    lines: list[str] = []
    lines.append(f"task packet: {report.packet_path}")
    if report.ok and not report.warnings:
        lines.append("verdict: PASS - no findings")
        return "\n".join(lines)
    lines.append(f"verdict: {'PASS' if report.ok else 'FAIL'}")
    if report.errors:
        lines.append("")
        lines.append(f"errors ({len(report.errors)}):")
        for f in report.errors:
            lines.append(f"  [{f.code}] {f.message}")
    if report.warnings:
        lines.append("")
        lines.append(f"warnings ({len(report.warnings)}):")
        for f in report.warnings:
            lines.append(f"  [{f.code}] {f.message}")
    return "\n".join(lines)


def render_doctor_report(report: DoctorReport) -> str:
    """Human-readable rendering used by the CLI."""
    lines: list[str] = []
    lines.append(f"cwd:                {report.cwd}")
    lines.append(f"target_path:        {report.target_path or '(no config)'}")
    lines.append(f"cwd_matches_target: {report.cwd_matches_target}")
    lines.append(f"current_branch:     {report.current_branch or '(unknown)'}")
    lines.append(
        f"dirty:              "
        f"{'(unknown)' if report.dirty is None else str(report.dirty).lower()}"
    )
    lines.append("")
    lines.append(f"pytest processes ({len(report.pytest_processes)}):")
    for r in report.pytest_processes:
        lines.append(_format_process_row(r))
    lines.append("")
    lines.append(f"python processes ({len(report.python_processes)}):")
    for r in report.python_processes:
        lines.append(_format_process_row(r))
    lines.append("")
    lines.append(f"shell processes ({len(report.shell_processes)}):")
    for r in report.shell_processes:
        lines.append(_format_process_row(r))
    if report.warnings:
        lines.append("")
        lines.append(f"warnings ({len(report.warnings)}):")
        for w in report.warnings:
            lines.append(f"  ! {w}")
    return "\n".join(lines)


def _format_process_row(r: ProcessRow) -> str:
    runtime = "?" if r.runtime_seconds is None else f"{r.runtime_seconds}s"
    return f"  pid={r.pid:>7}  runtime={runtime:>8}  name={r.name}  cmd={r.cmdline}"

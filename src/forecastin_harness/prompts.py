"""Render agent-specific prompts from a canonical task packet.

The packet schema is documented in ``docs/agent-contract.md`` and lives in
``templates/task_packet.yaml``. The same packet can drive Claude, Codex, and
Gemini prompts via their respective Markdown templates in ``templates/``.

Templates use Python's :class:`string.Template` ``$identifier`` substitution
(stdlib, no Jinja dependency). All keys in the template must resolve; missing
keys raise :class:`KeyError` rather than silently producing blanks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Mapping

import yaml

SUPPORTED_AGENTS: tuple[str, ...] = ("claude", "codex", "gemini")

_REQUIRED_KEYS = (
    "mission",
    "scope",
    "files",
    "acceptance",
    "tests",
    "stop_conditions",
    "evidence",
)


class TaskPacketError(ValueError):
    """Raised when a task packet fails validation."""


@dataclass(frozen=True)
class TaskPacket:
    mission: str
    scope: str
    files_allowed: tuple[str, ...]
    files_forbidden: tuple[str, ...]
    acceptance: tuple[str, ...]
    tests: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    evidence: tuple[str, ...]
    task_id: str = ""
    branch: str = ""
    base_sha: str = ""
    extra: Mapping[str, Any] | None = None

    def to_substitutions(self) -> dict[str, str]:
        """Flatten the packet to ``$identifier`` -> string substitutions."""
        return {
            "task_id": self.task_id or "(unset)",
            "branch": self.branch or "(unset)",
            "base_sha": self.base_sha or "(unset)",
            "mission": self.mission,
            "scope": self.scope,
            "files_allowed": _bullet_lines(self.files_allowed) or "(none specified)",
            "files_forbidden": _bullet_lines(self.files_forbidden) or "(none specified)",
            "acceptance": _bullet_lines(self.acceptance),
            "tests": _bullet_lines(self.tests),
            "stop_conditions": _bullet_lines(self.stop_conditions),
            "evidence": _bullet_lines(self.evidence),
        }


def load_packet(path: str | Path) -> TaskPacket:
    """Load and validate a YAML task packet."""
    p = Path(path)
    if not p.is_file():
        raise TaskPacketError(f"task packet not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise TaskPacketError(f"task packet root must be a mapping in {p}")
    return parse_packet(raw)


def parse_packet(raw: Mapping[str, Any]) -> TaskPacket:
    missing = [k for k in _REQUIRED_KEYS if k not in raw]
    if missing:
        raise TaskPacketError(f"task packet missing required keys: {missing}")

    files = raw.get("files") or {}
    if not isinstance(files, Mapping):
        raise TaskPacketError("files must be a mapping with allowed/forbidden lists")

    return TaskPacket(
        mission=_require_str(raw, "mission"),
        scope=_require_str(raw, "scope"),
        files_allowed=_to_str_tuple(files.get("allowed", []), "files.allowed"),
        files_forbidden=_to_str_tuple(files.get("forbidden", []), "files.forbidden"),
        acceptance=_to_str_tuple(raw["acceptance"], "acceptance"),
        tests=_to_str_tuple(raw["tests"], "tests"),
        stop_conditions=_to_str_tuple(raw["stop_conditions"], "stop_conditions"),
        evidence=_to_str_tuple(raw["evidence"], "evidence"),
        task_id=str(raw.get("task_id", "")),
        branch=str(raw.get("branch", "")),
        base_sha=str(raw.get("base_sha", "")),
        extra={k: v for k, v in raw.items() if k not in _RECOGNISED_KEYS} or None,
    )


_RECOGNISED_KEYS = set(_REQUIRED_KEYS) | {"task_id", "branch", "base_sha"}


def render_prompt(packet: TaskPacket, agent: str, *, templates_dir: Path) -> str:
    """Render the agent-specific prompt for a packet.

    ``agent`` must be one of :data:`SUPPORTED_AGENTS`. The corresponding
    template ``<agent>_prompt.md`` is loaded from ``templates_dir``.
    """
    if agent not in SUPPORTED_AGENTS:
        raise ValueError(f"unsupported agent {agent!r}; allowed: {SUPPORTED_AGENTS}")
    template_path = Path(templates_dir) / f"{agent}_prompt.md"
    if not template_path.is_file():
        raise FileNotFoundError(f"agent template not found: {template_path}")
    template = Template(template_path.read_text(encoding="utf-8"))
    subs = packet.to_substitutions()
    # Template.substitute raises KeyError if the template references an unknown
    # variable, which is exactly the behaviour we want.
    return template.substitute(subs)


def _require_str(raw: Mapping[str, Any], key: str) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v.strip():
        raise TaskPacketError(f"{key!r} must be a non-empty string")
    return v.strip()


def _to_str_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # Accept a single string; normalise to a one-element tuple.
        return (value,)
    if not isinstance(value, list):
        raise TaskPacketError(f"{field_name} must be a list of strings")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise TaskPacketError(
                f"{field_name}[{i}] must be a string, got {type(item).__name__}"
            )
        out.append(item)
    return tuple(out)


def _bullet_lines(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items)

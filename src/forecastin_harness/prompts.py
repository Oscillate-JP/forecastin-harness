"""Render agent-specific prompts from a canonical task packet.

The packet schema is documented in ``docs/agent-contract.md`` and lives in
``forecastin_harness/templates/task_packet.yaml`` (shipped inside the wheel).
The same packet can drive Claude, Codex, and Gemini prompts via their
respective Markdown templates in ``forecastin_harness/templates/``.

Templates use Python's :class:`string.Template` ``$identifier`` substitution
(stdlib, no Jinja dependency). All keys in the template must resolve; missing
keys raise :class:`KeyError` rather than silently producing blanks.

Templates are loaded via :mod:`importlib.resources` by default, so
``render_prompt`` works equally well from an editable checkout and from an
installed wheel. Callers may still pass ``templates_dir=<Path>`` to override
the location for tests or for shipping a custom template set.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any, Mapping

import yaml

SUPPORTED_AGENTS: tuple[str, ...] = ("claude", "codex", "gemini")

#: Package that owns the shipped template files. The four files
#: ``claude_prompt.md``, ``codex_prompt.md``, ``gemini_prompt.md`` and
#: ``task_packet.yaml`` live here and are declared in ``pyproject.toml``'s
#: ``[tool.setuptools.package-data]`` table so they survive ``pip install``.
TEMPLATES_PACKAGE: str = "forecastin_harness.templates"

_REQUIRED_KEYS = (
    "mission",
    "scope",
    "files",
    "acceptance",
    "tests",
    "stop_conditions",
    "evidence",
)

#: Lists that MUST contain at least one entry. Empty lists here mean the lane
#: definition is incomplete: an agent with no acceptance criteria, no tests,
#: no stop conditions or no evidence requirements has no contract to honour.
_REQUIRED_NONEMPTY: tuple[str, ...] = (
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
    target_repo: str = ""
    worktree: str = ""
    extra: Mapping[str, Any] | None = None

    def to_substitutions(self) -> dict[str, str]:
        """Flatten the packet to ``$identifier`` -> string substitutions."""
        return {
            "task_id": self.task_id or "(unset)",
            "branch": self.branch or "(unset)",
            "base_sha": self.base_sha or "(unset)",
            "target_repo": self.target_repo or "(unset)",
            "worktree": self.worktree or "(unset)",
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

    parsed_lists: dict[str, tuple[str, ...]] = {
        name: _to_str_tuple(raw[name], name) for name in _REQUIRED_NONEMPTY
    }
    for name in _REQUIRED_NONEMPTY:
        if not parsed_lists[name]:
            raise TaskPacketError(
                f"{name!r} must contain at least one entry; got empty list"
            )

    return TaskPacket(
        mission=_require_str(raw, "mission"),
        scope=_require_str(raw, "scope"),
        files_allowed=_to_str_tuple(files.get("allowed", []), "files.allowed"),
        files_forbidden=_to_str_tuple(files.get("forbidden", []), "files.forbidden"),
        acceptance=parsed_lists["acceptance"],
        tests=parsed_lists["tests"],
        stop_conditions=parsed_lists["stop_conditions"],
        evidence=parsed_lists["evidence"],
        task_id=str(raw.get("task_id", "")),
        branch=str(raw.get("branch", "")),
        base_sha=str(raw.get("base_sha", "")),
        target_repo=str(raw.get("target_repo", "")),
        worktree=str(raw.get("worktree", "")),
        extra={k: v for k, v in raw.items() if k not in _RECOGNISED_KEYS} or None,
    )


_RECOGNISED_KEYS = set(_REQUIRED_KEYS) | {
    "task_id",
    "branch",
    "base_sha",
    "target_repo",
    "worktree",
}


def render_prompt(
    packet: TaskPacket,
    agent: str,
    *,
    templates_dir: Path | str | None = None,
) -> str:
    """Render the agent-specific prompt for a packet.

    ``agent`` must be one of :data:`SUPPORTED_AGENTS`. The corresponding
    template ``<agent>_prompt.md`` is loaded from ``templates_dir`` when
    supplied, or from the wheel's packaged
    :data:`TEMPLATES_PACKAGE` resources when ``templates_dir`` is ``None``
    (the default).

    Templates use :class:`string.Template`; references to unknown ``$keys``
    raise :class:`KeyError` rather than silently producing blanks.
    """
    if agent not in SUPPORTED_AGENTS:
        raise ValueError(f"unsupported agent {agent!r}; allowed: {SUPPORTED_AGENTS}")
    text = _read_template_text(agent, templates_dir)
    template = Template(text)
    subs = packet.to_substitutions()
    # Template.substitute raises KeyError if the template references an unknown
    # variable, which is exactly the behaviour we want.
    return template.substitute(subs)


def _read_template_text(agent: str, templates_dir: Path | str | None) -> str:
    """Load ``<agent>_prompt.md`` either from a directory or from package data.

    The two paths are kept here in one place so callers do not need to think
    about the editable-vs-installed distinction.
    """
    filename = f"{agent}_prompt.md"
    if templates_dir is None:
        try:
            return (
                resources.files(TEMPLATES_PACKAGE)
                .joinpath(filename)
                .read_text(encoding="utf-8")
            )
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise FileNotFoundError(
                f"agent template not found in package {TEMPLATES_PACKAGE}: "
                f"{filename} ({exc})"
            ) from exc
    template_path = Path(templates_dir) / filename
    if not template_path.is_file():
        raise FileNotFoundError(f"agent template not found: {template_path}")
    return template_path.read_text(encoding="utf-8")


def default_templates_dir() -> Path | None:
    """Return the templates directory the CLI should pre-fill, or ``None``.

    Contract for the CLI integrator (the CLI module itself is not edited
    here):

    * Returns ``None`` when the harness is running from an installed wheel.
      The CLI should treat ``None`` as "use packaged resources" and pass
      ``templates_dir=None`` to :func:`render_prompt`. ``render_prompt`` then
      loads templates via :mod:`importlib.resources`.

    * Returns a :class:`pathlib.Path` to ``<repo>/templates`` if such a
      directory exists on disk relative to the current source file. This
      preserves the legacy editable-checkout layout where the templates lived
      at the repo root, and lets the operator drop in a custom override
      without touching package data.

    Callers must accept ``None`` as a valid value and forward it unchanged.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "templates"
        # Skip the packaged copy at src/forecastin_harness/templates/ — the
        # wheel-install path already finds it via importlib.resources, and
        # using it as ``templates_dir`` would wedge tests into believing the
        # legacy layout still exists.
        if candidate.is_dir() and candidate.parent.name != "forecastin_harness":
            return candidate
    return None


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

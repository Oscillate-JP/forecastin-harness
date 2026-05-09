"""Load and validate the target-repo adapter configuration.

The harness is target-repo-agnostic: every behaviour is parameterised by a
YAML adapter file. See ``configs/forecastin.example.yaml`` for the canonical
example and ``docs/forecastin-adapter.md`` for the field-by-field rationale.

The schema is intentionally narrow: validation rejects unknown top-level keys
so typos surface early. Optional fields fall back to documented defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

_ALLOWED_TOP_KEYS = {
    "target",
    "worktrees",
    "agents",
    "commands",
    "health",
    "merge",
    "state",
}

_ALLOWED_AGENTS = {"claude", "codex", "gemini"}

_ALLOWED_MERGE_POLICIES = {"operator-confirmed", "auto"}


class ConfigError(ValueError):
    """Raised when the adapter config fails validation."""


@dataclass(frozen=True)
class TargetSpec:
    repo: str  # "<owner>/<name>" on GitHub, e.g. "Oscillate-JP/Forecastin"
    path: Path  # absolute local path to the target repo working copy
    main_branch: str = "main"


@dataclass(frozen=True)
class WorktreeSpec:
    root: Path  # filesystem root under which lane worktrees are created


@dataclass(frozen=True)
class CommandSpec:
    backend_test: str | None = None
    frontend_test: str | None = None
    backend_lint: str | None = None
    frontend_lint: str | None = None
    full_ci: str | None = None


@dataclass(frozen=True)
class HealthSpec:
    backend_url: str | None = None
    frontend_url: str | None = None


@dataclass(frozen=True)
class MergeSpec:
    policy: str = "operator-confirmed"


@dataclass(frozen=True)
class StateSpec:
    """Where harness state is written, *relative to the target repo path*.

    Defaults to ``.harness/state``. Storing state under the target repo means
    one harness instance per checkout; multiple checkouts get independent
    state directories without manual scoping.
    """

    dir: str = ".harness/state"


@dataclass(frozen=True)
class HarnessConfig:
    target: TargetSpec
    worktrees: WorktreeSpec
    agents: tuple[str, ...]
    commands: CommandSpec = field(default_factory=CommandSpec)
    health: HealthSpec = field(default_factory=HealthSpec)
    merge: MergeSpec = field(default_factory=MergeSpec)
    state: StateSpec = field(default_factory=StateSpec)

    @property
    def state_path(self) -> Path:
        return self.target.path / self.state.dir


def load_config(path: str | Path) -> HarnessConfig:
    """Load and validate a YAML adapter config from disk.

    Raises :class:`ConfigError` for any validation failure with a precise
    message naming the offending field.
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
    return parse_config(raw)


def parse_config(raw: Mapping[str, Any]) -> HarnessConfig:
    """Validate a parsed mapping into a :class:`HarnessConfig`."""
    unknown = set(raw.keys()) - _ALLOWED_TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level keys: {sorted(unknown)}")

    target = _parse_target(raw.get("target"))
    worktrees = _parse_worktrees(raw.get("worktrees"))
    agents = _parse_agents(raw.get("agents"))
    commands = _parse_commands(raw.get("commands"))
    health = _parse_health(raw.get("health"))
    merge = _parse_merge(raw.get("merge"))
    state = _parse_state(raw.get("state"))

    return HarnessConfig(
        target=target,
        worktrees=worktrees,
        agents=agents,
        commands=commands,
        health=health,
        merge=merge,
        state=state,
    )


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if value is None:
        raise ConfigError(f"missing required section: {field_name}")
    if not isinstance(value, Mapping):
        raise ConfigError(f"section {field_name!r} must be a mapping")
    return value


def _parse_target(value: Any) -> TargetSpec:
    section = _require_mapping(value, "target")
    repo = section.get("repo")
    path = section.get("path")
    main_branch = section.get("main_branch", "main")
    if not isinstance(repo, str) or "/" not in repo:
        raise ConfigError("target.repo must be a string '<owner>/<name>'")
    if not isinstance(path, str) or not path:
        raise ConfigError("target.path must be a non-empty string")
    if not isinstance(main_branch, str) or not main_branch:
        raise ConfigError("target.main_branch must be a non-empty string")
    return TargetSpec(repo=repo, path=Path(path), main_branch=main_branch)


def _parse_worktrees(value: Any) -> WorktreeSpec:
    section = _require_mapping(value, "worktrees")
    root = section.get("root")
    if not isinstance(root, str) or not root:
        raise ConfigError("worktrees.root must be a non-empty string")
    return WorktreeSpec(root=Path(root))


def _parse_agents(value: Any) -> tuple[str, ...]:
    if value is None:
        raise ConfigError("missing required section: agents")
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise ConfigError("agents must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"agent entries must be strings, got {type(item).__name__}")
        if item not in _ALLOWED_AGENTS:
            raise ConfigError(
                f"unknown agent {item!r}; allowed: {sorted(_ALLOWED_AGENTS)}"
            )
        out.append(item)
    if not out:
        raise ConfigError("agents must list at least one agent")
    return tuple(out)


def _parse_commands(value: Any) -> CommandSpec:
    if value is None:
        return CommandSpec()
    if not isinstance(value, Mapping):
        raise ConfigError("commands must be a mapping")
    return CommandSpec(
        backend_test=_opt_str(value, "backend_test"),
        frontend_test=_opt_str(value, "frontend_test"),
        backend_lint=_opt_str(value, "backend_lint"),
        frontend_lint=_opt_str(value, "frontend_lint"),
        full_ci=_opt_str(value, "full_ci"),
    )


def _parse_health(value: Any) -> HealthSpec:
    if value is None:
        return HealthSpec()
    if not isinstance(value, Mapping):
        raise ConfigError("health must be a mapping")
    return HealthSpec(
        backend_url=_opt_str(value, "backend_url"),
        frontend_url=_opt_str(value, "frontend_url"),
    )


def _parse_merge(value: Any) -> MergeSpec:
    if value is None:
        return MergeSpec()
    if not isinstance(value, Mapping):
        raise ConfigError("merge must be a mapping")
    policy = value.get("policy", "operator-confirmed")
    if policy not in _ALLOWED_MERGE_POLICIES:
        raise ConfigError(
            f"merge.policy must be one of {sorted(_ALLOWED_MERGE_POLICIES)}, got {policy!r}"
        )
    return MergeSpec(policy=policy)


def _parse_state(value: Any) -> StateSpec:
    if value is None:
        return StateSpec()
    if not isinstance(value, Mapping):
        raise ConfigError("state must be a mapping")
    dir_val = value.get("dir", ".harness/state")
    if not isinstance(dir_val, str) or not dir_val:
        raise ConfigError("state.dir must be a non-empty string")
    return StateSpec(dir=dir_val)


def _opt_str(value: Mapping[str, Any], key: str) -> str | None:
    v = value.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ConfigError(f"{key} must be a string if set")
    return v

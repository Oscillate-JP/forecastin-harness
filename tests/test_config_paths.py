"""Path-validation guards for the adapter config loader.

These tests guard the wrong-repo-mutation footgun: today, the loader would
accept a relative ``target.path`` and silently resolve it against the
process CWD — meaning a harness invocation from the wrong directory could
mutate the wrong checkout. They also lock in the warning surface for
worktrees living outside the target tree (legal but worth flagging).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forecastin_harness.config import ConfigError, parse_config


def _payload(target_path: Path | str, **overrides) -> dict:
    """Build a minimal valid payload, then apply overrides."""
    payload = {
        "target": {
            "repo": "Oscillate-JP/Forecastin",
            "path": str(target_path),
            "main_branch": "main",
        },
        "worktrees": {
            "root": str(Path(str(target_path)) / ".harness" / "worktrees"),
        },
        "agents": ["claude"],
    }
    for key, value in overrides.items():
        payload[key] = value
    return payload


def test_parse_config_rejects_relative_target_path(tmp_path: Path) -> None:
    payload = {
        "target": {
            "repo": "Oscillate-JP/Forecastin",
            "path": "relative/path",
            "main_branch": "main",
        },
        # worktrees.root absolute so we hit the target.path check (it runs first).
        "worktrees": {"root": str(tmp_path / "wt")},
        "agents": ["claude"],
    }
    with pytest.raises(ConfigError, match="target.path must be absolute"):
        parse_config(payload)


def test_parse_config_rejects_relative_worktree_root(fake_target_repo: Path) -> None:
    payload = _payload(
        fake_target_repo,
        worktrees={"root": "relative/wt/path"},
    )
    with pytest.raises(ConfigError, match="worktrees.root must be absolute"):
        parse_config(payload)


def test_parse_config_warns_when_worktree_root_outside_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    elsewhere = tmp_path / "elsewhere" / "worktrees"
    elsewhere.mkdir(parents=True)

    payload = {
        "target": {
            "repo": "Oscillate-JP/Forecastin",
            "path": str(target),
            "main_branch": "main",
        },
        "worktrees": {"root": str(elsewhere)},
        "agents": ["claude"],
    }
    cfg = parse_config(payload)
    assert cfg.warnings, "expected a containment warning"
    assert any("not contained in" in w for w in cfg.warnings)
    assert any(str(elsewhere) in w for w in cfg.warnings)


def test_parse_config_no_warnings_when_worktree_root_inside_target(
    fake_target_repo: Path,
) -> None:
    cfg = parse_config(_payload(fake_target_repo))
    assert cfg.warnings == ()


def test_parse_config_rejects_absolute_state_dir(
    fake_target_repo: Path, tmp_path: Path
) -> None:
    # Use an actually-absolute path on the host platform (tmp_path is
    # absolute on both POSIX and Windows; "/foo" is not absolute on Windows).
    abs_state = str(tmp_path / "alt-state")
    assert Path(abs_state).is_absolute()
    payload = _payload(
        fake_target_repo,
        state={"dir": abs_state},
    )
    with pytest.raises(ConfigError, match="state.dir must be relative"):
        parse_config(payload)

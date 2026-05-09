"""Tests for the adapter config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from forecastin_harness.config import (
    ConfigError,
    HarnessConfig,
    load_config,
    parse_config,
)


def test_parse_config_minimum_required_fields(fake_target_repo: Path) -> None:
    cfg = parse_config(
        {
            "target": {
                "repo": "Oscillate-JP/Forecastin",
                "path": str(fake_target_repo),
                "main_branch": "main",
            },
            "worktrees": {"root": str(fake_target_repo / ".harness/worktrees")},
            "agents": ["claude"],
        }
    )
    assert isinstance(cfg, HarnessConfig)
    assert cfg.target.repo == "Oscillate-JP/Forecastin"
    assert cfg.target.main_branch == "main"
    assert cfg.agents == ("claude",)
    assert cfg.merge.policy == "operator-confirmed"  # default
    assert cfg.state.dir == ".harness/state"  # default
    # state_path is computed from target.path + state.dir
    assert cfg.state_path == fake_target_repo / ".harness/state"


def test_parse_config_rejects_unknown_top_keys(fake_target_repo: Path) -> None:
    with pytest.raises(ConfigError, match="unknown top-level keys"):
        parse_config(
            {
                "target": {"repo": "a/b", "path": str(fake_target_repo)},
                "worktrees": {"root": str(fake_target_repo)},
                "agents": ["claude"],
                "bogus": True,
            }
        )


def test_parse_config_rejects_unknown_agent(fake_target_repo: Path) -> None:
    with pytest.raises(ConfigError, match="unknown agent"):
        parse_config(
            {
                "target": {"repo": "a/b", "path": str(fake_target_repo)},
                "worktrees": {"root": str(fake_target_repo)},
                "agents": ["wishful"],
            }
        )


def test_parse_config_requires_repo_format() -> None:
    with pytest.raises(ConfigError, match="<owner>/<name>"):
        parse_config(
            {
                "target": {"repo": "no-slash-here", "path": "x"},
                "worktrees": {"root": "y"},
                "agents": ["claude"],
            }
        )


def test_parse_config_rejects_invalid_merge_policy(fake_target_repo: Path) -> None:
    with pytest.raises(ConfigError, match="merge.policy"):
        parse_config(
            {
                "target": {"repo": "a/b", "path": str(fake_target_repo)},
                "worktrees": {"root": str(fake_target_repo)},
                "agents": ["claude"],
                "merge": {"policy": "rubber-stamp"},
            }
        )


def test_parse_config_empty_agents_rejected(fake_target_repo: Path) -> None:
    with pytest.raises(ConfigError, match="at least one agent"):
        parse_config(
            {
                "target": {"repo": "a/b", "path": str(fake_target_repo)},
                "worktrees": {"root": str(fake_target_repo)},
                "agents": [],
            }
        )


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.yaml")


def test_load_config_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("target: {\n  unterminated: ", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)


def test_load_config_round_trip(example_config_file: Path) -> None:
    cfg = load_config(example_config_file)
    assert cfg.target.repo == "Oscillate-JP/Forecastin"
    assert "claude" in cfg.agents and "codex" in cfg.agents and "gemini" in cfg.agents
    assert cfg.commands.full_ci == "bash scripts/ci.sh"
    assert cfg.health.backend_url == "http://localhost:9000/health"

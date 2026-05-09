"""Test fixtures shared across the harness test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forecastin_harness.config import HarnessConfig, parse_config


def _example_config_payload(target_path: Path) -> dict:
    return {
        "target": {
            "repo": "Oscillate-JP/Forecastin",
            "path": str(target_path),
            "main_branch": "main",
        },
        "worktrees": {
            "root": str(target_path / ".harness" / "worktrees"),
        },
        "agents": ["claude", "codex", "gemini"],
        "commands": {
            "backend_test": "cd backend && pytest -x",
            "frontend_test": "cd frontend && npm test",
            "full_ci": "bash scripts/ci.sh",
        },
        "health": {
            "backend_url": "http://localhost:9000/health",
            "frontend_url": "http://localhost:3002",
        },
        "merge": {"policy": "operator-confirmed"},
        "state": {"dir": ".harness/state"},
    }


@pytest.fixture
def fake_target_repo(tmp_path: Path) -> Path:
    """A directory that *looks* like a git repo to the harness, without git ops."""
    target = tmp_path / "target"
    target.mkdir()
    (target / ".git").mkdir()  # init() only checks existence
    return target


@pytest.fixture
def example_config(fake_target_repo: Path) -> HarnessConfig:
    return parse_config(_example_config_payload(fake_target_repo))


@pytest.fixture
def example_config_file(tmp_path: Path, fake_target_repo: Path) -> Path:
    payload = _example_config_payload(fake_target_repo)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return config_file


@pytest.fixture
def repo_root() -> Path:
    """Repository root, used to locate the shipped templates directory."""
    return Path(__file__).resolve().parents[1]

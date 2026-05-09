"""Tests for task packet validation and per-agent prompt rendering."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forecastin_harness.prompts import (
    SUPPORTED_AGENTS,
    TaskPacketError,
    load_packet,
    parse_packet,
    render_prompt,
)


def _valid_packet_dict() -> dict:
    return {
        "task_id": "FOR-235",
        "branch": "lane/for-235-settings",
        "base_sha": "abc1234",
        "mission": "Repair the GET /settings/groups endpoint so it returns 200.",
        "scope": "Backend only; do not change frontend.",
        "files": {
            "allowed": ["backend/app/api/v1/endpoints/settings.py"],
            "forbidden": ["frontend/**"],
        },
        "acceptance": ["pytest passes for settings endpoint tests"],
        "tests": ["cd backend && pytest backend/tests/api/v1/endpoints/test_settings.py -x"],
        "stop_conditions": ["all acceptance checks pass"],
        "evidence": ["paste failing-state and passing-state outputs"],
    }


def test_parse_packet_valid() -> None:
    packet = parse_packet(_valid_packet_dict())
    assert packet.task_id == "FOR-235"
    assert packet.branch == "lane/for-235-settings"
    assert packet.files_allowed == ("backend/app/api/v1/endpoints/settings.py",)
    assert packet.files_forbidden == ("frontend/**",)


def test_parse_packet_missing_required_key() -> None:
    raw = _valid_packet_dict()
    raw.pop("mission")
    with pytest.raises(TaskPacketError, match="missing required keys"):
        parse_packet(raw)


def test_parse_packet_rejects_non_string_list_item() -> None:
    raw = _valid_packet_dict()
    raw["acceptance"] = ["ok", 42]
    with pytest.raises(TaskPacketError, match="acceptance"):
        parse_packet(raw)


def test_parse_packet_accepts_single_string_for_list_field() -> None:
    raw = _valid_packet_dict()
    raw["tests"] = "pytest -x"
    packet = parse_packet(raw)
    assert packet.tests == ("pytest -x",)


def test_load_packet_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TaskPacketError, match="not found"):
        load_packet(tmp_path / "missing.yaml")


@pytest.mark.parametrize("agent", SUPPORTED_AGENTS)
def test_render_prompt_for_each_agent(agent: str, repo_root: Path, tmp_path: Path) -> None:
    raw = _valid_packet_dict()
    yaml_path = tmp_path / "packet.yaml"
    yaml_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    packet = load_packet(yaml_path)
    text = render_prompt(packet, agent, templates_dir=repo_root / "templates")
    # Substitutions must be applied — no $identifier should leak through.
    assert "$mission" not in text
    assert "$files_allowed" not in text
    assert "$task_id" not in text
    # Agent-specific signature: each template names its agent in the heading.
    assert agent in text.lower()
    # Mission text appears verbatim.
    assert raw["mission"].splitlines()[0] in text


def test_render_prompt_rejects_unknown_agent(repo_root: Path) -> None:
    packet = parse_packet(_valid_packet_dict())
    with pytest.raises(ValueError, match="unsupported agent"):
        render_prompt(packet, "wishful", templates_dir=repo_root / "templates")


def test_shipped_template_packet_validates(repo_root: Path) -> None:
    """The example packet shipped in templates/ must itself be valid."""
    packet = load_packet(repo_root / "templates" / "task_packet.yaml")
    assert packet.mission
    assert packet.scope
    assert packet.files_allowed

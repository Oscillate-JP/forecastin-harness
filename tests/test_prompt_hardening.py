"""Hardening tests for v0.3 task-packet schema and packaged template loading.

Covers:

* Empty critical-list rejection at packet load
  (``acceptance``, ``tests``, ``stop_conditions``, ``evidence``).
* New ``target_repo`` and ``worktree`` substitutions surface in every
  rendered agent prompt.
* :func:`render_prompt` loads templates from the wheel's package data when
  ``templates_dir`` is omitted — the key prerequisite for shipping the
  harness as an installable wheel.
* The example packet shipped under ``forecastin_harness/templates/`` is
  itself valid against the hardened schema, so it survives ``pip install``
  unchanged.
"""

from __future__ import annotations

from importlib import resources

import pytest

from forecastin_harness.prompts import (
    SUPPORTED_AGENTS,
    TEMPLATES_PACKAGE,
    TaskPacketError,
    load_packet,
    parse_packet,
    render_prompt,
)


def _valid_packet_dict() -> dict:
    return {
        "task_id": "FOR-9001",
        "branch": "lane/for-9001-hardening",
        "base_sha": "deadbeefcafef00d",
        "target_repo": "Oscillate-JP/Forecastin",
        "worktree": "/tmp/.harness/worktrees/lane-for-9001-hardening",
        "mission": "Reject empty critical lists in the task packet schema.",
        "scope": "prompts.py only.",
        "files": {
            "allowed": ["src/forecastin_harness/prompts.py"],
            "forbidden": ["src/forecastin_harness/cli.py"],
        },
        "acceptance": ["pytest passes"],
        "tests": ["python -m pytest -q"],
        "stop_conditions": ["all acceptance checks pass"],
        "evidence": ["paste pytest summary"],
    }


@pytest.mark.parametrize(
    "field",
    ["acceptance", "tests", "stop_conditions", "evidence"],
)
def test_parse_packet_rejects_empty_critical_list_parametrised(field: str) -> None:
    """Belt-and-braces parametrised covering all four required-non-empty fields.

    Each named test below is also kept individually (per the brief) so that
    a regression report names the exact field at fault.
    """
    raw = _valid_packet_dict()
    raw[field] = []
    with pytest.raises(TaskPacketError, match=field):
        parse_packet(raw)


def test_parse_packet_rejects_empty_acceptance() -> None:
    raw = _valid_packet_dict()
    raw["acceptance"] = []
    with pytest.raises(TaskPacketError, match="acceptance"):
        parse_packet(raw)


def test_parse_packet_rejects_empty_tests() -> None:
    raw = _valid_packet_dict()
    raw["tests"] = []
    with pytest.raises(TaskPacketError, match="tests"):
        parse_packet(raw)


def test_parse_packet_rejects_empty_stop_conditions() -> None:
    raw = _valid_packet_dict()
    raw["stop_conditions"] = []
    with pytest.raises(TaskPacketError, match="stop_conditions"):
        parse_packet(raw)


def test_parse_packet_rejects_empty_evidence() -> None:
    raw = _valid_packet_dict()
    raw["evidence"] = []
    with pytest.raises(TaskPacketError, match="evidence"):
        parse_packet(raw)


@pytest.mark.parametrize("agent", SUPPORTED_AGENTS)
def test_render_prompt_substitutes_target_repo_and_worktree(agent: str) -> None:
    """Each agent's rendered prompt must surface the new identity fields."""
    raw = _valid_packet_dict()
    packet = parse_packet(raw)
    text = render_prompt(packet, agent)
    assert raw["target_repo"] in text, (
        f"target_repo missing from rendered {agent} prompt"
    )
    assert raw["worktree"] in text, (
        f"worktree missing from rendered {agent} prompt"
    )
    # Section header should be present so operators can find it visually.
    assert "Repository identity" in text


def test_render_prompt_substitutes_unset_target_repo_with_fallback() -> None:
    """Missing target_repo / worktree fall back to ``(unset)``, not blank."""
    raw = _valid_packet_dict()
    raw.pop("target_repo")
    raw.pop("worktree")
    packet = parse_packet(raw)
    text = render_prompt(packet, "claude")
    assert "(unset)" in text


def test_render_prompt_uses_packaged_templates_when_no_dir() -> None:
    """With ``templates_dir=None`` the prompt comes from packaged resources.

    We assert two structural markers that originate inside the packaged
    template files (``Repository identity`` heading + the agent-specific
    title line) — neither comes from substitutions, so the only way they
    can land in the rendered string is via a successful packaged-template
    read.
    """
    packet = parse_packet(_valid_packet_dict())
    text = render_prompt(packet, "claude", templates_dir=None)
    assert "Repository identity" in text
    assert text.startswith("# Claude task:")


def test_packaged_template_packet_validates() -> None:
    """``task_packet.yaml`` ships in the wheel and must satisfy the schema.

    Loaded via :mod:`importlib.resources` rather than a relative filesystem
    path so the test passes from a wheel install too.
    """
    yaml_path = resources.files(TEMPLATES_PACKAGE).joinpath("task_packet.yaml")
    # ``files()`` returns a Traversable; ``load_packet`` accepts ``str`` or
    # ``Path``, so coerce explicitly.
    from pathlib import Path

    packet = load_packet(Path(str(yaml_path)))
    assert packet.mission
    assert packet.scope
    assert packet.files_allowed
    # The example sets target_repo and worktree, so ensure the parser kept
    # them rather than silently dropping unrecognised keys.
    assert packet.target_repo
    assert packet.worktree

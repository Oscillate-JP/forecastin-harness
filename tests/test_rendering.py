"""Tests for cross-platform command rendering."""

from __future__ import annotations

import pytest

from forecastin_harness.rendering import (
    COMMAND_MODES,
    CommandSpec,
    from_argv,
    from_string,
)


def test_command_modes_constant() -> None:
    assert set(COMMAND_MODES) == {"posix", "powershell", "argv"}


def test_from_string_posix() -> None:
    spec = from_string("echo hi", mode="posix")
    assert spec.to_subprocess_argv() == ["bash", "-lc", "echo hi"]
    assert "bash" in spec.display()


def test_from_string_powershell() -> None:
    spec = from_string("Write-Host hi", mode="powershell")
    argv = spec.to_subprocess_argv()
    assert argv[0] == "powershell"
    assert "-Command" in argv
    assert argv[-1] == "Write-Host hi"


def test_from_argv_round_trip() -> None:
    spec = from_argv(["python", "-c", "import sys; sys.exit(0)"])
    assert spec.to_subprocess_argv() == ["python", "-c", "import sys; sys.exit(0)"]
    assert "python" in spec.display()


def test_command_spec_rejects_mismatched_fields() -> None:
    with pytest.raises(ValueError):
        CommandSpec(mode="posix", argv=("a",))
    with pytest.raises(ValueError):
        CommandSpec(mode="argv", snippet="ls")


def test_command_spec_rejects_empty_inputs() -> None:
    with pytest.raises(ValueError):
        CommandSpec(mode="posix", snippet="")
    with pytest.raises(ValueError):
        CommandSpec(mode="argv", argv=())


def test_from_string_rejects_argv_mode() -> None:
    with pytest.raises(ValueError, match="from_argv"):
        from_string("anything", mode="argv")

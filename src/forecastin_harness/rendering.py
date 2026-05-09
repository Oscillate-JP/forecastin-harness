"""Cross-platform command rendering.

A user-supplied gate command can be expressed in three modes:

* ``posix`` — a shell snippet evaluated by ``bash -lc <snippet>``.
* ``powershell`` — a snippet evaluated by ``powershell -Command <snippet>``.
* ``argv`` — an explicit argv list. Most predictable; no shell parsing.

In every mode, the harness invokes the OS process via ``subprocess.Popen``
with ``shell=False``. When a shell is needed (POSIX / PowerShell modes) the
shell binary is named explicitly and the snippet is passed as a single
argument. We never call Popen with ``shell=True``; that keeps argv
quoting predictable across Windows ↔ POSIX ↔ Python versions.

The ``auto`` factory picks a sensible default for the host OS for the
benefit of the planner output, but the supervisor still requires the
operator (or test) to pass a concrete :class:`CommandSpec` rather than
guessing.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from typing import Literal

CommandMode = Literal["posix", "powershell", "argv"]
COMMAND_MODES: tuple[CommandMode, ...] = ("posix", "powershell", "argv")


@dataclass(frozen=True)
class CommandSpec:
    """A renderable, executable command across shells.

    ``snippet`` is a shell snippet for ``posix`` and ``powershell`` modes.
    ``argv`` is the explicit-arglist form. Exactly one of the two is set
    per instance; the other is ``None``.
    """

    mode: CommandMode
    snippet: str | None = None
    argv: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.mode == "argv":
            if not self.argv or self.snippet is not None:
                raise ValueError("argv mode requires non-empty argv and no snippet")
        else:
            if not self.snippet or self.argv is not None:
                raise ValueError(f"{self.mode} mode requires non-empty snippet and no argv")

    def display(self) -> str:
        """Operator-facing single-line rendering for plan output."""
        if self.mode == "argv":
            assert self.argv is not None
            return " ".join(_shquote(a) for a in self.argv)
        if self.mode == "powershell":
            return f"powershell -NoProfile -NonInteractive -Command {self.snippet!r}"
        return f"bash -lc {self.snippet!r}"

    def to_subprocess_argv(self) -> list[str]:
        """argv passed to ``subprocess.Popen`` (always with ``shell=False``)."""
        if self.mode == "argv":
            assert self.argv is not None
            return list(self.argv)
        if self.mode == "powershell":
            assert self.snippet is not None
            return [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                self.snippet,
            ]
        # posix
        assert self.snippet is not None
        return ["bash", "-lc", self.snippet]


def from_string(snippet: str, *, mode: CommandMode | None = None) -> CommandSpec:
    """Build a :class:`CommandSpec` from a shell snippet.

    If ``mode`` is omitted, defaults to ``posix`` on Linux/macOS and
    ``powershell`` on Windows. Pass ``mode`` explicitly when a test or
    operator wants the other shell.
    """
    chosen = mode or _default_shell_mode()
    if chosen == "argv":
        raise ValueError("argv mode requires from_argv(), not from_string()")
    return CommandSpec(mode=chosen, snippet=snippet)


def from_argv(argv: list[str] | tuple[str, ...]) -> CommandSpec:
    return CommandSpec(mode="argv", argv=tuple(argv))


def _default_shell_mode() -> CommandMode:
    return "powershell" if os.name == "nt" else "posix"


def _shquote(arg: str) -> str:
    # Read os.name through getattr so type-checkers don't constant-fold to
    # the host platform's literal value. Both branches are exercised
    # across host OSes; tests force the mode rather than the OS.
    name = getattr(os, "name")
    if name == "nt":
        if not arg or any(ch in arg for ch in ' "\\\t'):
            return '"' + arg.replace('"', '\\"') + '"'
        return arg
    return shlex.quote(arg)

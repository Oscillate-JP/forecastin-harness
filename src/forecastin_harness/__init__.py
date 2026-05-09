"""forecastin-harness — standalone AI-agent harness for the Forecastin programme.

This package is intentionally small. The public surface is the CLI defined in
:mod:`forecastin_harness.cli`. Library callers should depend on the named
modules (``config``, ``state``, ``worktrees``, ``gates``, ``prompts``,
``pr_gate``); everything else is internal.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]

"""Packaged template files for forecastin-harness.

This sub-package exists so :func:`importlib.resources.files` returns a
single concrete :class:`importlib.resources.abc.Traversable` (a real
filesystem path under setuptools) rather than a ``MultiplexedPath`` over
namespace packages. The four sibling files are declared as
``package-data`` in the project ``pyproject.toml`` and are loaded at
runtime by :mod:`forecastin_harness.prompts`.

Do not add Python code here.
"""

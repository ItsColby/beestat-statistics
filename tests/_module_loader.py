"""Load isolated product modules without executing the Home Assistant entrypoint."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from collections.abc import Iterable
from pathlib import Path


def load_module(root: Path, package_name: str, name: str) -> types.ModuleType:
    package = sys.modules.setdefault(package_name, types.ModuleType(package_name))
    package.__path__ = [str(root)]
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.{name}", root / f"{name}.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def preserve_modules(testcase: unittest.TestCase, names: Iterable[str]) -> None:
    """Restore the named ``sys.modules`` entries when the test finishes."""

    old_modules = {key: sys.modules.get(key) for key in names}

    def restore() -> None:
        for key, module in old_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    testcase.addCleanup(restore)

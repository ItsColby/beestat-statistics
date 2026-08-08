"""Run tests that do not require Home Assistant or its pytest harness."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HA_IMPORT_ROOTS = frozenset(
    {
        "homeassistant",
        "pytest_homeassistant_custom_component",
    }
)


def discover_home_assistant_test_files(
    test_files: tuple[Path, ...],
) -> tuple[Path, ...]:
    """Return test modules that directly import Home Assistant or its harness."""

    discovered: list[Path] = []
    for path in test_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_roots.add(node.module.split(".", 1)[0])
        if imported_roots & HA_IMPORT_ROOTS:
            discovered.append(path)
    return tuple(discovered)


def dependency_light_test_files() -> tuple[Path, ...]:
    """Return every test module that does not directly require the HA harness."""

    test_files = tuple(sorted(TESTS.glob("test_*.py")))
    ha_test_files = set(discover_home_assistant_test_files(test_files))
    if not ha_test_files:
        raise RuntimeError("No Home Assistant test modules were discovered")
    return tuple(path for path in test_files if path not in ha_test_files)


def main() -> int:
    """Run the dependency-light suite without importing HA-only modules."""

    suite = unittest.TestSuite()
    for path in dependency_light_test_files():
        suite.addTests(unittest.TestLoader().discover(str(TESTS), pattern=path.name))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

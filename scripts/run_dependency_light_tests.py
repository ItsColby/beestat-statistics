"""Run tests that do not require Home Assistant or its pytest harness."""

from __future__ import annotations

import argparse
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

    test_files = tuple(sorted(TESTS.rglob("test_*.py")))
    if any(path.parent != TESTS for path in test_files):
        raise RuntimeError("Dependency-light discovery requires flat tests/test_*.py")
    ha_test_files = set(discover_home_assistant_test_files(test_files))
    if not ha_test_files:
        raise RuntimeError("No Home Assistant test modules were discovered")
    selected = tuple(path for path in test_files if path not in ha_test_files)
    if not selected:
        raise RuntimeError("No dependency-light test modules were discovered")
    return selected


def main(argv: list[str] | None = None) -> int:
    """Run the dependency-light suite without importing HA-only modules."""

    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        suite = _load_suite(dependency_light_test_files())
    except (OSError, RuntimeError, SyntaxError, ImportError) as err:
        print(f"Dependency-light discovery failed: {err}", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun > len(result.skipped) else 1


def _load_suite(test_files: tuple[Path, ...]) -> unittest.TestSuite:
    """Load every selected module and reject tests unittest would silently omit."""

    suite = unittest.TestSuite()
    for path in test_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test_")
            for node in tree.body
        ):
            raise RuntimeError(f"{path.name} contains tests outside unittest.TestCase")
        module_suite = unittest.TestLoader().discover(str(TESTS), pattern=path.name)
        if not module_suite.countTestCases():
            raise RuntimeError(f"{path.name} did not collect any tests")
        module = sys.modules.get(path.stem)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                candidate = getattr(module, node.name, None)
                if isinstance(candidate, type) and not issubclass(
                    candidate, unittest.TestCase
                ):
                    raise RuntimeError(
                        f"{path.name} contains a non-unittest test class"
                    )
        suite.addTests(module_suite)
    return suite


if __name__ == "__main__":
    raise SystemExit(main())

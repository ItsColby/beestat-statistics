"""Run tests that do not require Home Assistant or its pytest harness."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HA_TEST_FILENAMES = frozenset(
    {
        "test_config_flow_ha.py",
        "test_runtime_ha.py",
    }
)


def dependency_light_test_files() -> tuple[Path, ...]:
    """Return dependency-light test modules and verify the HA owners exist."""

    test_files = tuple(sorted(TESTS.glob("test_*.py")))
    filenames = {path.name for path in test_files}
    missing_ha_modules = HA_TEST_FILENAMES - filenames
    if missing_ha_modules:
        missing = ", ".join(sorted(missing_ha_modules))
        raise RuntimeError(
            f"Declared Home Assistant test modules are missing: {missing}"
        )
    return tuple(path for path in test_files if path.name not in HA_TEST_FILENAMES)


def main() -> int:
    """Run the dependency-light suite without importing HA-only modules."""

    suite = unittest.TestSuite()
    for path in dependency_light_test_files():
        suite.addTests(unittest.TestLoader().discover(str(TESTS), pattern=path.name))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

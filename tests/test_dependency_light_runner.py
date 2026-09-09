"""Regression tests for complete, fail-closed dependency-light test selection."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import run_dependency_light_tests as runner


class DependencyLightRunnerTests(unittest.TestCase):
    def test_discovery_recognizes_both_home_assistant_import_forms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = []
            for name, content in {
                "test_core.py": "import homeassistant.core\n",
                "test_harness.py": "from pytest_homeassistant_custom_component import common\n",
                "test_unit.py": "import unittest\n",
            }.items():
                path = root / name
                path.write_text(content, encoding="utf-8")
                files.append(path)
            self.assertEqual(
                tuple(files[:2]),
                runner.discover_home_assistant_test_files(tuple(files)),
            )
            with patch.object(runner, "TESTS", root):
                self.assertEqual((files[2],), runner.dependency_light_test_files())

    def test_empty_or_all_ha_discovery_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(runner, "TESTS", root):
                with self.assertRaisesRegex(RuntimeError, "No Home Assistant"):
                    runner.dependency_light_test_files()
                (root / "test_ha.py").write_text(
                    "import homeassistant\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(RuntimeError, "No dependency-light"):
                    runner.dependency_light_test_files()

    def test_nested_module_is_reported_instead_of_silently_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "nested" / "test_missing.py").write_text("", encoding="utf-8")
            with (
                patch.object(runner, "TESTS", root),
                self.assertRaisesRegex(RuntimeError, "flat"),
            ):
                runner.dependency_light_test_files()

    def test_empty_module_and_pytest_functions_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for text, message in (
                ("", "did not collect"),
                ("def test_example(): pass\n", "outside unittest"),
                ("async def test_example(): pass\n", "outside unittest"),
            ):
                path = root / "test_example.py"
                path.write_text(text, encoding="utf-8")
                with (
                    patch.object(runner, "TESTS", root),
                    patch.object(
                        unittest.TestLoader,
                        "discover",
                        return_value=unittest.TestSuite(),
                    ),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    runner._load_suite((path,))

    def test_cli_discovery_failure_returns_nonzero(self) -> None:
        with (
            patch.object(
                runner,
                "dependency_light_test_files",
                side_effect=RuntimeError("No tests"),
            ),
            redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(2, runner.main([]))
        self.assertIn("discovery failed", output.getvalue())

    def test_mixed_module_cannot_silently_omit_a_pytest_class(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "test_example.py"
            path.write_text("class TestExample: pass\n", encoding="utf-8")
            module = SimpleNamespace(TestExample=type("TestExample", (), {}))
            with (
                patch.object(runner, "TESTS", root),
                patch.dict(runner.sys.modules, {"test_example": module}),
                patch.object(
                    unittest.TestLoader,
                    "discover",
                    return_value=unittest.TestSuite(
                        [unittest.FunctionTestCase(lambda: None)]
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, "non-unittest test class"),
            ):
                runner._load_suite((path,))

    def test_cli_pass_failure_and_entirely_skipped_suite(self) -> None:
        for successful, skipped, expected in (
            (True, [], 0),
            (False, [], 1),
            (True, [("test", "reason")], 1),
        ):
            result = unittest.TestResult()
            result.testsRun = 1
            result.skipped = skipped
            if not successful:
                result.failures.append(
                    (unittest.FunctionTestCase(lambda: None), "failure")
                )
            with (
                self.subTest(expected=expected, skipped=bool(skipped)),
                patch.object(runner, "dependency_light_test_files", return_value=()),
                patch.object(runner, "_load_suite", return_value=unittest.TestSuite()),
                patch.object(unittest.TextTestRunner, "run", return_value=result),
            ):
                self.assertEqual(expected, runner.main([]))


if __name__ == "__main__":
    unittest.main()

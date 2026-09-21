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
from tests import test_ha_quality_static as quality


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

    def test_home_assistant_lane_uses_pytest_and_preserves_its_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = root / "test_unit.py"
            unit.write_text("import unittest\n", encoding="utf-8")
            core = root / "test_core.py"
            core.write_text("from homeassistant import core\n", encoding="utf-8")
            harness = root / "test_harness.py"
            harness.write_text(
                "import pytest_homeassistant_custom_component\n", encoding="utf-8"
            )
            (root / "feature_test.py").write_text(
                "def test_future_native_collection(): pass\n", encoding="utf-8"
            )
            for exit_code in (0, 1, 5):
                with (
                    self.subTest(exit_code=exit_code),
                    patch.object(runner, "TESTS", root),
                    patch.dict(runner.sys.modules, {"pytest": SimpleNamespace()}),
                    patch(
                        "pytest.main", create=True, return_value=exit_code
                    ) as collect,
                    patch.object(runner, "_load_suite") as unit_loader,
                ):
                    self.assertEqual(exit_code, runner.main(["--home-assistant"]))
                    collect.assert_called_once_with(
                        [str(root), "-q", f"--ignore={unit}"]
                    )
                    unit_loader.assert_not_called()

    def test_nested_ha_modules_reach_native_pytest_with_exact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            nested = tests / "nested"
            nested.mkdir(parents=True)
            unit = tests / "test_unit.py"
            unit.write_text("import unittest\n", encoding="utf-8")
            core = nested / "test_core.py"
            core.write_text("from homeassistant import core\n", encoding="utf-8")
            alternate = nested / "feature_test.py"
            alternate.write_text("def test_native(): pass\n", encoding="utf-8")
            selected = ["tests/nested/test_core.py", "tests/nested/feature_test.py"]
            with (
                patch.object(runner, "ROOT", root),
                patch.object(runner, "TESTS", tests),
                patch.dict(runner.sys.modules, {"pytest": SimpleNamespace()}),
                patch("pytest.main", create=True, return_value=5) as collect,
            ):
                self.assertEqual((unit,), runner.dependency_light_test_files())
                self.assertEqual(5, runner.main(["--home-assistant"]))
                collect.assert_called_once_with([str(tests), "-q", f"--ignore={unit}"])
                collect.reset_mock()
                self.assertEqual(
                    5,
                    runner.main(
                        [
                            "--home-assistant",
                            "--test",
                            selected[0],
                            "--test",
                            selected[1],
                        ]
                    ),
                )
                collect.assert_called_once_with(["-q", str(core), str(alternate)])
                for paths in (
                    selected,
                    [selected[0]] * 2,
                    ["tests/nested/../nested/test_core.py"],
                ):
                    with self.subTest(paths=paths), self.assertRaises(RuntimeError):
                        runner.validate_test_selection(paths, home_assistant=False)
                for paths in (
                    [selected[0]] * 2,
                    ["tests/nested/../nested/test_core.py"],
                ):
                    with self.subTest(paths=paths), self.assertRaises(RuntimeError):
                        runner.validate_test_selection(paths, home_assistant=True)

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

    def test_static_ha_guard_reads_nested_paths_without_losing_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("first", "second"):
                path = root / "tests" / folder / "test_ha.py"
                path.parent.mkdir(parents=True)
                path.write_text("import homeassistant\n", encoding="utf-8")
            check = quality.HomeAssistantQualityStaticTest(
                "test_discovered_ha_modules_fail_closed_without_harness"
            )
            with patch.object(quality, "ROOT", root):
                check.test_discovered_ha_modules_fail_closed_without_harness()
                path.write_text(
                    "import homeassistant\nraise unittest.SkipTest()\n",
                    encoding="utf-8",
                )
                with self.assertRaises(AssertionError):
                    check.test_discovered_ha_modules_fail_closed_without_harness()

    def test_empty_module_and_pytest_functions_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for text, message in (
                ("", "did not collect"),
                ("def test_example(): pass\n", "outside unittest"),
                ("async def test_example(): pass\n", "outside unittest"),
                (
                    (
                        "import unittest\n"
                        "class Kept(unittest.TestCase):\n"
                        "    def test_kept(self): pass\n"
                        "def testBehavior(): raise AssertionError('must not vanish')\n"
                    ),
                    "outside unittest",
                ),
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

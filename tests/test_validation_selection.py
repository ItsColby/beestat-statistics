"""Protect affected selection, dependency boundaries, and aggregate failures."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_dependency_light_tests as planner


def setUpModule() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    environment_patch = patch.dict(os.environ, environment, clear=True)
    environment_patch.start()
    unittest.addModuleCleanup(environment_patch.stop)


class ValidationSelectionTests(unittest.TestCase):
    def _planning_fixture(self, directory: Path, *, snapshot: bool) -> tuple[Path, str]:
        """Give CLI tests their own baseline, including the runner's unborn snapshot."""
        source = directory / "source"
        for relative in (
            planner.PLANNER,
            "scripts/check_public_safety.py",
            "scripts/verify-release-local.sh",
            "tests/test_runtime_ha.py",
            planner.METADATA_TEST,
            "README.md",
        ):
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Validation",
            GIT_COMMITTER_NAME="Validation",
            GIT_AUTHOR_EMAIL="validation@example.com",
            GIT_COMMITTER_EMAIL="validation@example.com",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
        )

        def git(root, *args):
            return subprocess.check_output(
                ["git", "-C", str(root), *args], env=env, text=True
            ).strip()

        git(source, "-c", "init.templateDir=", "init", "-q")
        git(source, "add", "-A", "-f")
        baseline = git(source, "commit-tree", git(source, "write-tree"), "-m", "base")
        git(source, "update-ref", "HEAD", baseline)
        if not snapshot:
            return source, ""
        target = directory / "snapshot"
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"))
        git(target, "-c", "init.templateDir=", "init", "-q")
        git(target, "add", "-A", "-f")
        self.assertNotEqual(
            0,
            subprocess.run(
                ["git", "-C", str(target), "rev-parse", "--verify", "HEAD"],
                env=env,
                capture_output=True,
                check=False,
            ).returncode,
        )
        # Snapshots have an index, but planning must bind an explicit source baseline.
        return target, str(source / ".git")

    def test_tooling_changes_keep_runtime_and_support_lanes_out(self):
        runner = (ROOT / "scripts/verify-release-local.sh").read_text()
        with patch.object(planner, "_git", return_value=runner):
            plan = planner.build_plan(["scripts/verify-release-local.sh"])
        self.assertTrue(plan["shell"])
        self.assertIn("tests/test_validation_selection.py", plan["unit_tests"])
        self.assertFalse(plan["jobs"]["minimum"])
        self.assertFalse(plan["jobs"]["current"])
        self.assertEqual([], plan["ha_tests"])

    def test_runner_dependency_changes_select_their_actual_consumers(self):
        path = "scripts/verify-release-local.sh"
        current = (ROOT / path).read_text()
        pins = planner.runner_dependencies(current)
        for key, minimum, current_lane, release in (
            ("minimum", True, False, False),
            ("current", False, True, False),
            ("minimum_requirements", True, False, False),
            ("current_requirements", False, True, False),
            ("python_image", True, True, False),
            ("hassfest_image", False, False, True),
            ("mypy", True, False, False),
            ("ruff", False, False, False),
            ("actionlint_image", False, False, False),
            ("actionlint", False, False, False),
            ("zizmor", False, False, False),
            ("shellcheck-py", False, False, False),
        ):
            previous = current.replace(pins[key], pins[key] + ".previous")
            with (
                self.subTest(key=key),
                patch.object(planner, "_git", return_value=previous) as git,
            ):
                plan = planner.build_plan(
                    [path], base="reviewed-base", git_directory="git-owner"
                )
                self.assertEqual([], plan["unresolved"])
                self.assertEqual(minimum, plan["jobs"]["minimum"])
                self.assertEqual(current_lane, plan["jobs"]["current"])
                self.assertEqual(release, plan["jobs"]["release"])
                self.assertFalse(plan["jobs"]["hacs"])
                git.assert_called_once_with(
                    "show", "reviewed-base:" + path, git_directory="git-owner"
                )
                if key == "mypy":
                    self.assertEqual([], plan["ha_tests"])
                    command = planner.lane_command(plan, "minimum")
                    self.assertIn(pins["mypy"], command)
                    self.assertNotIn("pytest", command)
                    self.assertNotIn("--home-assistant", command)
                if key == "ruff":
                    self.assertIn(planner.PRODUCT + "/__init__.py", plan["python"])
                    self.assertIn(pins["ruff"], planner.lane_command(plan, "unit"))
                if key in {"minimum", "current"}:
                    self.assertTrue(plan["lane_tests"][key])
                    other = "current" if key == "minimum" else "minimum"
                    self.assertEqual([], plan["lane_tests"][other])
                if key in {"actionlint_image", "actionlint", "zizmor", "shellcheck-py"}:
                    self.assertTrue(plan["workflow"])
                if key == "shellcheck-py":
                    self.assertTrue(plan["shell"])

    def test_workflow_dependency_changes_select_only_the_changed_job(self):
        path = ".github/workflows/validate.yaml"
        current = (ROOT / path).read_text()
        declarations = planner.workflow_dependencies(current)
        for job, lane in (
            ("home_assistant_minimum", "minimum"),
            ("home_assistant_current", "current"),
            ("hacs", "hacs"),
            ("hassfest", "release"),
        ):
            prefix, body = current.split("  " + job + ":\n", 1)
            previous = (
                prefix
                + "  "
                + job
                + ":\n"
                + body.replace("runs-on: ubuntu-24.04", "runs-on: ubuntu-22.04", 1)
            )
            with (
                self.subTest(lane=lane),
                patch.object(planner, "_git", return_value=previous),
            ):
                plan = planner.build_plan([path])
                self.assertEqual([], plan["unresolved"])
                for name in ("minimum", "current", "release", "hacs"):
                    self.assertEqual(name == lane, plan["jobs"][name])
        previous = current.replace("category: integration", "category: previous")
        with patch.object(planner, "_git", return_value=previous):
            plan = planner.build_plan([path])
        self.assertEqual([], plan["unresolved"])
        self.assertTrue(plan["jobs"]["hacs"])
        self.assertFalse(plan["jobs"]["minimum"])
        self.assertFalse(plan["jobs"]["current"])
        self.assertFalse(plan["jobs"]["release"])
        self.assertIn("current", declarations)

        for job, old, new, lane in (
            ("home_assistant_minimum", "--only minimum", "--only current", "minimum"),
            ("home_assistant_current", "--only current", "--only minimum", "current"),
            ("home_assistant_current", "fetch-depth: 0", "fetch-depth: 1", "current"),
            (
                "home_assistant_current",
                "    steps:",
                '    env:\n      CHECK: "value # data"\n    steps:',
                "current",
            ),
            (
                "home_assistant_current",
                "        run: |",
                "        env:\n          CHECK: |\n            # literal data\n        run: |",
                "current",
            ),
        ):
            prefix, body = current.split("  " + job + ":\n", 1)
            previous = prefix + "  " + job + ":\n" + body.replace(old, new, 1)
            with (
                self.subTest(job=job, change=new),
                patch.object(planner, "_git", return_value=previous),
            ):
                plan = planner.build_plan([path])
                self.assertEqual([], plan["unresolved"])
                for name in ("minimum", "current", "release", "hacs"):
                    self.assertEqual(name == lane, plan["jobs"][name])

        read_text = Path.read_text
        for key, scalar in (
            ("CHECK", '"value # before"'),
            ("CHECK", "|\n        # before"),
            ("name", '"value # before"'),
        ):
            prefix, body = current.split("  home_assistant_current:\n", 1)
            candidate = (
                prefix
                + "  home_assistant_current:\n"
                + body.replace(
                    "    steps:", f"    env:\n      {key}: {scalar}\n    steps:", 1
                )
            )
            previous = candidate.replace("# before", "# after")

            def read_candidate(source, *args, candidate=candidate, **kwargs):
                return (
                    candidate
                    if source == ROOT / path
                    else read_text(source, *args, **kwargs)
                )

            with (
                self.subTest(key=key, scalar=scalar),
                patch.object(planner, "_git", return_value=previous),
                patch.object(Path, "read_text", read_candidate),
            ):
                plan = planner.build_plan([path])
                self.assertEqual([], plan["unresolved"])
                for name in ("minimum", "current", "release", "hacs"):
                    self.assertEqual(name == "current", plan["jobs"][name])

        # YAML comments do not change execution, but hashes inside scalar data do.
        previous = current.replace("fetch-depth: 0", "fetch-depth: 0 # explanation")
        previous = "# workflow explanation\n" + previous.replace(
            "    steps:", "    # job explanation\n    steps:"
        )
        with patch.object(planner, "_git", return_value=previous):
            plan = planner.build_plan([path])
        self.assertEqual([], plan["unresolved"])
        self.assertFalse(
            any(
                plan["jobs"][lane] for lane in ("minimum", "current", "release", "hacs")
            )
        )

        for previous in (
            current.replace("VALIDATION_FULL:", "VALIDATION_FULL_PREVIOUS:", 1),
            "defaults:\n  run:\n    shell: bash\n" + current,
        ):
            with (
                self.subTest(shared=previous[:60]),
                patch.object(planner, "_git", return_value=previous),
            ):
                plan = planner.build_plan([path])
                self.assertEqual([], plan["unresolved"])
                self.assertTrue(all(plan["jobs"].values()))

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_pyproject_semantic_changes_use_real_base_and_tool_consumers(self):
        baseline = (
            "[tool.ruff]\nline-length = 88\n"
            "[tool.mypy]\nstrict = true\n"
            '[tool.unrelated]\nvalue = "retained"\nnot_a_number = nan\n'
        )
        cases = (
            ("comment", baseline + "# explanation\n", set(), False),
            ("format", baseline.replace(" = ", "="), set(), False),
            ("ruff", baseline.replace("88", "89"), {"python"}, False),
            ("mypy", baseline.replace("true", "false"), {"minimum"}, False),
            ("type", baseline.replace("true", "1"), {"minimum"}, False),
            (
                "removed",
                baseline.replace("[tool.ruff]\nline-length = 88\n", ""),
                {"python"},
                False,
            ),
            (
                "both",
                baseline.replace("88", "89").replace("true", "false"),
                {"python", "minimum"},
                False,
            ),
            ("unknown", baseline.replace('"retained"', '"changed"'), set(), True),
            (
                "pytest",
                baseline + '[tool.pytest.ini_options]\naddopts = "-x"\n',
                set(),
                True,
            ),
            ("project", baseline + '[project]\nname = "example"\n', set(), True),
            ("invalid", baseline + "[invalid\n", set(), True),
            ("deleted", None, set(), True),
        )
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self._planning_fixture(Path(directory), snapshot=False)
            product = root / planner.PRODUCT / "__init__.py"
            product.parent.mkdir(parents=True)
            product.write_text('"""Synthetic product input."""\n')
            config = root / "pyproject.toml"
            config.write_text(baseline)
            env = dict(
                os.environ,
                GIT_AUTHOR_NAME="Validation",
                GIT_COMMITTER_NAME="Validation",
                GIT_AUTHOR_EMAIL="validation@example.com",
                GIT_COMMITTER_EMAIL="validation@example.com",
            )

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(root), *args], env=env, text=True
                ).strip()

            git("add", "-A", "-f")
            before = git("commit-tree", git("write-tree"), "-m", "configuration base")
            git("update-ref", "HEAD", before)
            for name, candidate, consumers, unresolved in cases:
                with self.subTest(change=name):
                    if candidate is None:
                        config.unlink()
                    else:
                        config.write_text(candidate)
                    git("add", "-A", "-f")
                    after = git(
                        "commit-tree", git("write-tree"), "-p", before, "-m", name
                    )
                    git("update-ref", "HEAD", after)
                    with patch.object(planner, "ROOT", root):
                        paths = planner.changed_paths(before, after, None)
                        plan = planner.build_plan(paths, before, str(root / ".git"))
                        self.assertEqual(["pyproject.toml"], paths)
                        self.assertEqual(unresolved, bool(plan["unresolved"]))
                        self.assertEqual("python" in consumers, bool(plan["python"]))
                        self.assertEqual(
                            "minimum" in consumers, plan["jobs"]["minimum"]
                        )
                        self.assertEqual([], plan["ha_tests"])
                        self.assertFalse(plan["jobs"]["current"])
                        self.assertFalse(plan["jobs"]["release"])
                        self.assertFalse(plan["jobs"]["hacs"])
                        self.assertTrue(plan["safety"])
                        if "minimum" in consumers:
                            command = planner.lane_command(plan, "minimum")
                            self.assertIn("-m mypy", command)
                            self.assertNotIn("--home-assistant", command)
                        if "python" in consumers:
                            self.assertIn(
                                "-m ruff check", planner.lane_command(plan, "unit")
                            )
                        if name == "mypy":
                            mixed = planner.build_plan(
                                [*paths, "tests/test_runtime_ha.py"], before
                            )
                            self.assertTrue(mixed["jobs"]["minimum"])
                            self.assertTrue(mixed["jobs"]["current"])
                            self.assertEqual(
                                ["tests/test_runtime_ha.py"], mixed["ha_tests"]
                            )

    def test_pyproject_comparison_requires_available_valid_base(self):
        for previous in (OSError("missing comparison"), ValueError("malformed base")):
            with patch.object(planner, "_git", side_effect=previous):
                plan = planner.build_plan(["pyproject.toml"])
            self.assertTrue(plan["unresolved"])
            self.assertFalse(plan["jobs"]["minimum"])
        with patch.object(planner, "_git", return_value="[invalid"):
            self.assertTrue(planner.build_plan(["pyproject.toml"])["unresolved"])

    def test_unavailable_dependency_comparison_is_unresolved(self):
        with patch.object(planner, "_git", side_effect=OSError("missing comparison")):
            plan = planner.build_plan(["scripts/verify-release-local.sh"])
        self.assertTrue(
            any(
                "dependency comparison unavailable" in item
                for item in plan["unresolved"]
            )
        )
        self.assertFalse(plan["jobs"]["minimum"])
        self.assertFalse(plan["jobs"]["current"])
        with self.assertRaises(ValueError):
            planner.runner_dependencies("missing declarations")
        runner = (ROOT / "scripts/verify-release-local.sh").read_text()
        with self.assertRaisesRegex(ValueError, "requirements declaration"):
            planner.runner_dependencies(
                runner.replace("pip install --upgrade -r", "pip install -r")
            )
        with self.assertRaises(ValueError):
            planner.workflow_dependencies(
                "jobs:\n  future_job:\n    uses: unknown/action@ref"
            )
        workflow = (ROOT / ".github/workflows/validate.yaml").read_text()
        for job_id in ("future_job", "extra-job", "job2", "FutureJob", "_job-2"):
            for comment in ("", " # synthetic", " \t# synthetic"):
                with (
                    self.subTest(job_id=job_id, comment=comment),
                    self.assertRaisesRegex(ValueError, "job dependency mapping"),
                ):
                    planner.workflow_dependencies(
                        workflow.rstrip()
                        + f"\n  {job_id}:{comment}\n"
                        + "    runs-on: ubuntu-24.04\n"
                        + "    steps:\n      - run: exit 1\n"
                    )
        commented = workflow
        for job_id in (
            "plan",
            "unit",
            "home_assistant_minimum",
            "home_assistant_current",
            "hassfest",
            "hacs",
            "release_gate",
        ):
            commented = commented.replace(
                f"  {job_id}:\n", f"  {job_id}: # explanation\n"
            )
        self.assertEqual(
            planner.workflow_dependencies(workflow),
            planner.workflow_dependencies(commented),
        )

    def test_api_snapshot_selects_its_offline_consumer(self):
        plan = planner.build_plan(["docs/beestat-api-surface.json"])
        self.assertEqual([], plan["unresolved"])
        self.assertEqual([planner.API_SURFACE_TEST], plan["unit_tests"])
        self.assertFalse(plan["workflow"])
        self.assertTrue(plan["safety"])
        self.assertEqual({name: name == "unit" for name in planner.JOBS}, plan["jobs"])
        command = planner.lane_command(plan, "unit")
        self.assertIn("--test tests/test_api_surface_checker.py", command)
        self.assertNotIn("python scripts/check_beestat_api_surface.py", command)

    def test_api_workflow_dependency_changes_select_offline_audit_and_workflow_checks(
        self,
    ):
        path = planner.API_SURFACE_WORKFLOW
        current = (ROOT / path).read_text()
        previous_versions = (
            current,
            current.replace("ubuntu-24.04", "ubuntu-22.04"),
            current.replace('python-version: "3.14"', 'python-version: "3.13"'),
            current.replace("uses: actions/checkout@", "uses: previous/checkout@"),
            current.replace("persist-credentials: false", "persist-credentials: true"),
            current.replace("python scripts/", "python -B scripts/"),
            "defaults:\n  run:\n    shell: bash\n" + current,
        )
        for previous in previous_versions:
            with (
                self.subTest(previous=previous),
                patch.object(planner, "_git", return_value=previous),
            ):
                plan = planner.build_plan([path])
                self.assertEqual([], plan["unresolved"])
                self.assertEqual(
                    {planner.API_SURFACE_TEST},
                    set(plan["unit_tests"]),
                )
                self.assertEqual([], plan["ha_tests"])
                self.assertTrue(plan["workflow"])
                self.assertTrue(plan["safety"])
                self.assertEqual(
                    {name: name == "unit" for name in planner.JOBS}, plan["jobs"]
                )
                command = planner.lane_command(plan, "unit")
                self.assertIn("zizmor --strict-collection --persona auditor .", command)
                self.assertNotIn("python scripts/check_beestat_api_surface.py", command)

    def test_api_workflow_unknown_jobs_and_unavailable_comparison_fail_closed(self):
        current = (ROOT / planner.API_SURFACE_WORKFLOW).read_text()
        for previous in (
            current.replace("  check:", "  renamed:"),
            current + "\n  future_job:\n    runs-on: ubuntu-24.04\n",
            current + "\n  future-job2:\n    runs-on: ubuntu-24.04\n",
            current + "\n  plan:\n    runs-on: ubuntu-24.04\n",
        ):
            with self.subTest(previous=previous):
                with self.assertRaisesRegex(ValueError, "job dependency mapping"):
                    planner.workflow_dependencies(previous, api_surface=True)
                with patch.object(planner, "_git", return_value=previous):
                    plan = planner.build_plan([planner.API_SURFACE_WORKFLOW])
                self.assertTrue(plan["unresolved"])
        with patch.object(planner, "_git", side_effect=OSError("missing comparison")):
            plan = planner.build_plan([planner.API_SURFACE_WORKFLOW])
        self.assertTrue(plan["unresolved"])
        plan = planner.build_plan([".github/workflows/unknown.yaml"])
        self.assertTrue(
            any(
                "Unresolved workflow dependency owner" in item
                for item in plan["unresolved"]
            )
        )

    def test_api_workflow_candidate_environment_must_match_its_unit_consumer(self):
        path = planner.API_SURFACE_WORKFLOW
        current = (ROOT / path).read_text()
        read_text = Path.read_text
        for candidate, resolved in (
            (
                current.replace('python-version: "3.14"', 'python-version: "3.13"'),
                False,
            ),
            (current.replace("ubuntu-24.04", "windows-2025"), False),
            (current.replace('python-version: "3.14"', "python-version: '3.14'"), True),
            (
                current.replace(
                    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
                    "actions/setup-python@" + "a" * 40,
                ),
                True,
            ),
        ):

            def read_candidate(source, *args, candidate=candidate, **kwargs):
                return (
                    candidate
                    if source == ROOT / path
                    else read_text(source, *args, **kwargs)
                )

            with (
                self.subTest(candidate=candidate),
                patch.object(planner, "_git", return_value=current),
                patch.object(Path, "read_text", read_candidate),
            ):
                plan = planner.build_plan([path])
            self.assertEqual(resolved, not plan["unresolved"])
            if resolved:
                self.assertIn(planner.API_SURFACE_TEST, plan["unit_tests"])
                self.assertTrue(plan["workflow"])
                self.assertEqual(
                    {name: name == "unit" for name in planner.JOBS}, plan["jobs"]
                )
            else:
                self.assertTrue(
                    any("environment differs" in item for item in plan["unresolved"])
                )

    def test_retained_document_contracts_select_their_static_consumer(self):
        self.assertFalse((ROOT / "docs/removed.md").exists())
        for path in [
            "README.md",
            "RELEASE_NOTES.md",
            "docs/development.md",
            "docs/usage.md",
            "docs/architecture.md",
            "docs/removed.md",
        ]:
            with self.subTest(path=path):
                plan = planner.build_plan([path])
                self.assertIn(planner.METADATA_TEST, plan["unit_tests"])
                self.assertEqual([], plan["ha_tests"])
                self.assertEqual(
                    {name: name == "unit" for name in planner.JOBS}, plan["jobs"]
                )

    def test_only_changed_support_environment_runs(self):
        for path, lane, other in (
            ("requirements-ha-test.txt", "minimum", "current"),
            ("requirements-ha-current.txt", "current", "minimum"),
        ):
            with self.subTest(path=path):
                plan = planner.build_plan([path])
                self.assertTrue(plan["jobs"][lane])
                self.assertFalse(plan["jobs"][other])
                self.assertTrue(plan["ha_tests"])
                self.assertFalse(plan["jobs"]["release"])

    def test_documentation_json_examples_select_parse_and_public_safety_checks(self):
        for path in (
            "docs/examples/hourly-history-v3.json",
            "docs/examples/removed.json",
        ):
            with self.subTest(path=path):
                plan = planner.build_plan([path])
                self.assertEqual([], plan["unresolved"])
                self.assertEqual([planner.METADATA_TEST], plan["unit_tests"])
                self.assertEqual([], plan["ha_tests"])
                self.assertTrue(plan["safety"])
                self.assertFalse(plan["workflow"])
                self.assertFalse(plan["shell"])
                self.assertEqual(
                    {name: name == "unit" for name in planner.JOBS}, plan["jobs"]
                )
                self.assertIn(
                    "scripts/check_public_safety.py", planner.lane_command(plan, "unit")
                )

    def test_unknown_json_configuration_remains_unresolved(self):
        for path in (
            "config.json",
            "docs/config.json",
            "docs/examples/config/settings.json",
            "docs/examples/settings.yaml",
        ):
            with self.subTest(path=path):
                plan = planner.build_plan([path])
                self.assertEqual([path], plan["unresolved"])

    def test_runtime_change_reaches_real_import_consumers(self):
        plan = planner.build_plan([planner.PRODUCT + "/const.py"])
        self.assertTrue(plan["jobs"]["current"])
        self.assertTrue(plan["jobs"]["minimum"])
        self.assertTrue(plan["ha_tests"])
        self.assertEqual([], plan["unresolved"])

    def test_config_flow_change_reaches_framework_loaded_native_consumer(self):
        path = planner.PRODUCT + "/config_flow.py"
        native_test = "tests/test_config_flow_ha.py"
        plan = planner.build_plan([path])
        self.assertEqual([], plan["unresolved"])
        self.assertEqual([native_test], plan["ha_tests"])
        for lane in ("minimum", "current"):
            self.assertTrue(plan["jobs"][lane])
            self.assertEqual([native_test], plan["lane_tests"][lane])
        self.assertIn("tests/test_config_flow_helpers.py", plan["unit_tests"])
        self.assertIn(planner.METADATA_TEST, plan["unit_tests"])

    def test_test_only_change_uses_its_native_lane(self):
        path = "tests/test_runtime_ha.py"
        plan = planner.build_plan([path])
        self.assertIn(path, plan["ha_tests"])
        self.assertTrue(plan["jobs"]["current"])
        self.assertFalse(plan["jobs"]["minimum"])

    def test_unknown_change_is_not_converted_to_a_full_plan(self):
        plan = planner.build_plan(["future/unknown.py"])
        self.assertEqual(["future/unknown.py"], plan["unresolved"])
        self.assertFalse(plan["jobs"]["current"])

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_real_ref_comparison_binds_clean_candidate_and_resolves_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(
                os.environ,
                GIT_AUTHOR_NAME="Validation",
                GIT_COMMITTER_NAME="Validation",
                GIT_AUTHOR_EMAIL="validation@example.com",
                GIT_COMMITTER_EMAIL="validation@example.com",
            )

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(root), *args], env=env, text=True
                ).strip()

            git("-c", "init.templateDir=", "init", "-q")
            source = root / "README.md"
            source.write_text("before\n")
            git("add", "README.md")
            before = git("commit-tree", git("write-tree"), "-m", "base")
            git("update-ref", "HEAD", before)
            source.write_text("after\n")
            git("add", "README.md")
            after = git(
                "commit-tree", git("write-tree"), "-p", before, "-m", "candidate"
            )
            git("update-ref", "HEAD", after)
            with patch.object(planner, "ROOT", root):
                self.assertEqual(
                    ["README.md"], planner.changed_paths(before, after, None)
                )
                empty_paths = planner.changed_paths(after, after, None)
                self.assertEqual([], empty_paths)
                self.assertEqual(
                    ["README.md"],
                    planner.changed_paths(before, after, None, str(root / ".git")),
                )
                with self.assertRaisesRegex(ValueError, "checked-out candidate"):
                    planner.changed_paths(before, before, None)
                source.write_text("uncommitted\n")
                with self.assertRaisesRegex(ValueError, "clean candidate"):
                    planner.changed_paths(before, after, None)
            self.assertFalse(any(planner.build_plan(empty_paths)["jobs"].values()))

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_native_identity_survives_replacements_and_refuses_inherited_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "candidate"
            root.mkdir()
            env = dict(
                os.environ,
                GIT_AUTHOR_NAME="Validation",
                GIT_COMMITTER_NAME="Validation",
                GIT_AUTHOR_EMAIL="validation@example.com",
                GIT_COMMITTER_EMAIL="validation@example.com",
                PYTHONDONTWRITEBYTECODE="1",
            )
            # The runner disables replacements; this fixture first proves one exists.
            env.pop("GIT_NO_REPLACE_OBJECTS", None)

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(root), *args], env=env, text=True
                ).strip()

            git("-c", "init.templateDir=", "init", "-q")
            (root / "scripts").mkdir()
            for relative in (
                planner.PLANNER,
                "scripts/check_public_safety.py",
                "scripts/verify-release-local.ps1",
                "scripts/verify-release-local.sh",
            ):
                shutil.copy2(ROOT / relative, root / relative)
            (root / "README.md").write_text("before\n")
            git("add", ".")
            before = git("commit-tree", git("write-tree"), "-m", "base")
            git("update-ref", "HEAD", before)
            (root / "README.md").write_text("after\n")
            git("add", ".")
            after = git(
                "commit-tree", git("write-tree"), "-p", before, "-m", "candidate"
            )
            git("update-ref", "HEAD", after)
            git("replace", before, after)
            self.assertEqual("", git("diff", "--name-only", before, after))
            with patch.object(planner, "ROOT", root):
                self.assertEqual(
                    ["README.md"], planner.changed_paths(before, after, None)
                )
                self.assertEqual(
                    "before\n", planner._git("show", before + ":README.md")
                )
                for overrides in (
                    {"GIT_DIR": str(root / ".git")},
                    {
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "core.worktree",
                        "GIT_CONFIG_VALUE_0": str(root),
                    },
                ):
                    with (
                        self.subTest(overrides=tuple(overrides)),
                        patch.dict(os.environ, overrides),
                        self.assertRaisesRegex(ValueError, "Inherited local Git"),
                    ):
                        planner.changed_paths(before, after, None)
                with patch.dict(
                    os.environ,
                    {
                        "GIT_CONFIG_KEY_0": "core.worktree",
                        "GIT_CONFIG_VALUE_0": "unused",
                    },
                ):
                    self.assertEqual(after, planner._git("rev-parse", "HEAD").strip())
                nested = root / "nested"
                nested.mkdir()
                with (
                    patch.object(planner, "ROOT", nested),
                    self.assertRaisesRegex(ValueError, "target root"),
                ):
                    planner._git("rev-parse", "HEAD")

            cli = [sys.executable, str(root / planner.PLANNER)]
            if planner.PLANNER.endswith("run_dependency_light_tests.py"):
                cli.append("--plan")
            selection = ["--base", before, "--head", after]

            def run(command, overrides=None):
                return subprocess.run(
                    command,
                    env={**env, **(overrides or {})},
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )

            for adapter in ([], ["--git-directory", str(root / ".git")]):
                result = run([*cli, *selection, *adapter])
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(["README.md"], json.loads(result.stdout)["paths"])
            # A snapshot may legitimately use metadata outside its own root.
            snapshot = Path(directory) / "snapshot"
            shutil.copytree(root, snapshot, ignore=shutil.ignore_patterns(".git"))
            snapshot_cli = [*cli]
            snapshot_cli[1] = str(snapshot / planner.PLANNER)
            result = run(
                [*snapshot_cli, *selection, "--git-directory", str(root / ".git")]
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(["README.md"], json.loads(result.stdout)["paths"])

            wrong = Path(directory) / "wrong"
            subprocess.run(
                ["git", "-c", "init.templateDir=", "init", "-q", str(wrong)],
                check=True,
                env=env,
            )
            wrong_env = {"GIT_DIR": str(wrong / ".git"), "GIT_WORK_TREE": str(wrong)}
            result = run([*cli, *selection], wrong_env)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Inherited local Git", result.stderr)
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            if powershell:
                wrapper = [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(root / "scripts/verify-release-local.ps1"),
                    "-PlanOnly",
                    "-Base",
                    before,
                    "-Head",
                    after,
                ]
                result = run(wrapper)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(["README.md"], json.loads(result.stdout)["paths"])
                result = run(wrapper, wrong_env)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("Inherited local Git", result.stderr)
            if os.name == "posix" and shutil.which("bash"):
                wrapper = [
                    "bash",
                    str(root / "scripts/verify-release-local.sh"),
                    "affected",
                    "native",
                    str(root / ".git"),
                    *selection,
                    "--plan-only",
                ]
                result = run(wrapper, {"VALIDATION_PYTHON": sys.executable})
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(["README.md"], json.loads(result.stdout)["paths"])
                result = run(
                    wrapper, {**wrong_env, "VALIDATION_PYTHON": sys.executable}
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn("Inherited local Git", result.stderr)

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_cli_keeps_dependency_comparison_when_base_ref_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(
                os.environ,
                GIT_AUTHOR_NAME="Validation",
                GIT_COMMITTER_NAME="Validation",
                GIT_AUTHOR_EMAIL="validation@example.com",
                GIT_COMMITTER_EMAIL="validation@example.com",
            )

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(root), *args], env=env, text=True
                ).strip()

            git("-c", "init.templateDir=", "init", "-q")
            relative = "scripts/verify-release-local.sh"
            runner = root / relative
            runner.parent.mkdir()
            current = (ROOT / relative).read_text()
            pin = planner.runner_dependencies(current)["minimum"]
            runner.write_text(current.replace(pin, pin + ".previous"))
            git("add", ".")
            before = git("commit-tree", git("write-tree"), "-m", "base")
            git("update-ref", "HEAD", before)
            git("update-ref", "refs/heads/moving-base", before)
            runner.write_text(current)
            git("add", ".")
            after = git(
                "commit-tree", git("write-tree"), "-p", before, "-m", "candidate"
            )
            git("update-ref", "HEAD", after)
            native_changed_paths = planner.changed_paths

            def move_after_diff(*args):
                paths = native_changed_paths(*args)
                git("update-ref", "refs/heads/moving-base", after)
                return paths

            entrypoint = getattr(planner, "plan_main", None) or planner.main
            output = io.StringIO()
            with (
                patch.object(planner, "ROOT", root),
                patch.object(planner, "changed_paths", side_effect=move_after_diff),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(
                    0, entrypoint(["--base", "moving-base", "--head", after])
                )
            plan = json.loads(output.getvalue())
            self.assertEqual([relative], plan["paths"])
            self.assertTrue(plan["jobs"]["minimum"])
            self.assertFalse(plan["jobs"]["current"])

    def test_missing_input_and_traversal_fail(self):
        with self.assertRaises(ValueError):
            planner.changed_paths(None, None, None)
        for path in (
            "../outside.py",
            "/absolute.py",
            "Q:" + "/synthetic.py",
            "-option",
            "tests\\escape.py",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                planner.changed_paths(None, None, [path])

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("bash"), "requires native Bash"
    )
    def test_workflow_aggregate_requires_exact_planned_results(self):
        workflow = (ROOT / ".github/workflows/validate.yaml").read_text()
        gate = workflow.split("\n  release_gate:\n", 1)[1]
        command = textwrap.dedent(gate.split("        run: |\n", 1)[1])
        plan = planner.build_plan(["tests/test_runtime_ha.py"])
        names = {
            "unit": "UNIT",
            "minimum": "HOME_ASSISTANT_MINIMUM",
            "current": "HOME_ASSISTANT_CURRENT",
            "release": "HASSFEST",
            "hacs": "HACS",
        }
        results = {
            names[job]: "success" if selected else "skipped"
            for job, selected in plan["jobs"].items()
        }
        cases = [("planned success", {}, True)]
        cases.extend(
            (f"{job}: {replacement}", {names[job]: replacement}, False)
            for job, selected in plan["jobs"].items()
            for replacement in (
                ("skipped", "failure", "cancelled") if selected else ("success",)
            )
        )
        cases.extend(
            (f"plan: {status}", {"PLAN_STATUS": status}, False)
            for status in ("skipped", "failure", "cancelled")
        )
        cases.append(
            (
                "unresolved plan",
                {"PLAN": json.dumps({**plan, "unresolved": ["unknown.input"]})},
                False,
            )
        )
        for case, overrides, expected in cases:
            with self.subTest(case=case):
                result = subprocess.run(
                    [
                        "bash",
                        "--noprofile",
                        "--norc",
                        "-e",
                        "-o",
                        "pipefail",
                        "-c",
                        command,
                    ],
                    env={
                        **os.environ,
                        "PATH": str(Path(sys.executable).parent)
                        + os.pathsep
                        + os.environ["PATH"],
                        "PLAN_STATUS": "success",
                        "PLAN": json.dumps(plan),
                        **results,
                        **overrides,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(
                    expected, result.returncode == 0, result.stdout + result.stderr
                )

    def test_workflow_and_packaging_flags_accumulate(self):
        workflow = (ROOT / ".github/workflows/validate.yaml").read_text()
        with patch.object(planner, "_git", return_value=workflow):
            plan = planner.build_plan(
                [
                    ".github/workflows/validate.yaml",
                    "scripts/verify-release-local.ps1",
                    "hacs.json",
                    planner.PRODUCT + "/translations/en.json",
                ]
            )
        self.assertTrue(plan["workflow"])
        self.assertTrue(plan["jobs"]["hacs"])
        self.assertFalse(plan["jobs"]["current"])

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_plan_cli_rejects_unresolved_paths_without_launching_jobs(self):
        for snapshot in (False, True):
            with (
                self.subTest(snapshot=snapshot),
                tempfile.TemporaryDirectory() as directory,
            ):
                root, git_directory = self._planning_fixture(
                    Path(directory), snapshot=snapshot
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        str(root / planner.PLANNER),
                        "--plan",
                        "--path",
                        "unknown.input",
                        *(["--git-directory", git_directory] if git_directory else []),
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("Unresolved applicability", result.stderr)
                self.assertEqual("", result.stdout)

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_plan_cli_is_json_and_needs_no_ha_import(self):
        for snapshot in (False, True):
            with (
                self.subTest(snapshot=snapshot),
                tempfile.TemporaryDirectory() as directory,
            ):
                root, git_directory = self._planning_fixture(
                    Path(directory), snapshot=snapshot
                )
                command = [
                    sys.executable,
                    str(root / planner.PLANNER),
                    "--plan",
                    "--path",
                    "README.md",
                    "--plan-only",
                ]
                if snapshot:
                    unbound = subprocess.run(
                        command, cwd=root, capture_output=True, text=True, check=False
                    )
                    self.assertEqual(2, unbound.returncode)
                    self.assertIn("Validation plan failed", unbound.stderr)
                    self.assertEqual("", unbound.stdout)
                    command.extend(["--git-directory", git_directory])
                result = subprocess.run(
                    command, cwd=root, capture_output=True, text=True, check=False
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                plan = json.loads(result.stdout)
                self.assertEqual(["README.md"], plan["paths"])
                self.assertEqual([], plan["ha_tests"])

    def test_selected_test_rejects_wrong_lane_and_missing_files(self):
        for paths, ha in (
            (["tests/test_runtime_ha.py"], False),
            (["tests/test_dependency_light_runner.py"], True),
            (["tests/missing.py"], False),
            (["tests/test_dependency_light_runner.py"] * 2, False),
        ):
            with self.subTest(paths=paths, ha=ha), self.assertRaises(RuntimeError):
                planner.validate_test_selection(paths, home_assistant=ha)

    def test_selected_alternate_name_reaches_native_pytest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            path = tests / "future_test.py"
            path.write_text("def test_native(): pass\n")
            with (
                patch.object(planner, "ROOT", root),
                patch.object(planner, "TESTS", tests),
            ):
                self.assertEqual(
                    (path,),
                    planner.validate_test_selection(
                        ["tests/future_test.py"], home_assistant=True
                    ),
                )
                with self.assertRaises(RuntimeError):
                    planner.validate_test_selection(
                        ["tests/future_test.py"], home_assistant=False
                    )

    def test_direct_file_consumer_is_selected_with_its_dependency(self):
        plan = planner.build_plan([planner.PRODUCT + "/url_validation.py"])
        self.assertIn("tests/test_api_response.py", plan["unit_tests"])
        self.assertIn("tests/test_url_validation.py", plan["unit_tests"])
        self.assertNotIn("tests/test_dependency_light_runner.py", plan["unit_tests"])

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("bash"), "requires native Bash"
    )
    def test_native_affected_lane_propagates_failures_and_cleans_environment(self):
        stand_in = r"""#!/usr/bin/env python3
import json, os, shutil, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if args and (args[0].endswith("plan_validation.py") or "--plan" in args or args[0] == "-c"):
    raise SystemExit(subprocess.call([os.environ["SELECTION_REAL_PYTHON"], *args]))
with Path(os.environ["SELECTION_LOG"]).open("a") as stream:
    stream.write(json.dumps(args) + "\n")
if args[:2] == ["-m", "venv"]:
    target = Path(args[2]) / "bin/python"
    target.parent.mkdir()
    shutil.copyfile(__file__, target)
    target.chmod(0o755)
if os.environ.get("SELECTION_FAIL") == "harness" and any(arg.startswith("pytest-homeassistant-custom-component==") for arg in args):
    raise SystemExit(23)
if os.environ.get("SELECTION_FAIL") == "dependencies" and "--upgrade" in args:
    raise SystemExit(23)
if os.environ.get("SELECTION_FAIL") == "tests" and ("pytest" in args or "unittest" in args or "--home-assistant" in args):
    raise SystemExit(23)
"""
        for snapshot, failure in (
            (snapshot, failure)
            for snapshot in (False, True)
            for failure in ("", "harness", "dependencies", "tests")
        ):
            with (
                self.subTest(snapshot=snapshot, failure=failure),
                tempfile.TemporaryDirectory() as directory,
            ):
                temporary = Path(directory)
                root, git_directory = self._planning_fixture(
                    temporary, snapshot=snapshot
                )
                scratch = temporary / "scratch"
                scratch.mkdir()
                binary = temporary / "bin"
                binary.mkdir()
                fake = binary / "python"
                fake.write_text(stand_in, encoding="utf-8")
                fake.chmod(0o755)
                log = temporary / "calls.jsonl"
                env = dict(
                    os.environ,
                    PATH=str(binary) + os.pathsep + os.environ["PATH"],
                    TMPDIR=str(scratch),
                    SELECTION_LOG=str(log),
                    SELECTION_REAL_PYTHON=sys.executable,
                    VALIDATION_PYTHON=sys.executable,
                    SELECTION_FAIL=failure,
                )
                result = subprocess.run(
                    [
                        "bash",
                        str(root / "scripts/verify-release-local.sh"),
                        "affected",
                        "native",
                        git_directory,
                        "--path",
                        "tests/test_runtime_ha.py",
                        "--only",
                        "current",
                    ],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(
                    23 if failure else 0,
                    result.returncode,
                    result.stdout + result.stderr,
                )
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                environments = [
                    Path(call[2]) for call in calls if call[:2] == ["-m", "venv"]
                ]
                self.assertEqual(1, len(environments))
                self.assertTrue(all(not path.exists() for path in environments))
                self.assertEqual([], list(scratch.iterdir()))
                self.assertEqual(
                    failure != "harness",
                    any("requirements-ha-current.txt" in call for call in calls),
                )
                self.assertFalse(
                    any(
                        call[:2] == ["-m", "pip"] and "requirements-ha-test.txt" in call
                        for call in calls
                    )
                )
                test_calls = [
                    call
                    for call in calls
                    if "pytest" in call or "--home-assistant" in call
                ]
                if failure in {"harness", "dependencies"}:
                    self.assertEqual([], test_calls)
                else:
                    self.assertEqual(1, len(test_calls))
                    self.assertIn("tests/test_runtime_ha.py", test_calls[0])

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("bash"), "requires native Bash"
    )
    def test_native_plan_and_unselected_lane_stop_before_environment_creation(self):
        for snapshot in (False, True):
            with (
                self.subTest(snapshot=snapshot),
                tempfile.TemporaryDirectory() as directory,
            ):
                temporary = Path(directory)
                root, git_directory = self._planning_fixture(
                    temporary, snapshot=snapshot
                )
                scratch = temporary / "scratch"
                scratch.mkdir()
                env = dict(
                    os.environ, TMPDIR=str(scratch), VALIDATION_PYTHON=sys.executable
                )
                for extra, expected in (
                    (["--plan-only"], 0),
                    (["--only", "current"], 2),
                ):
                    result = subprocess.run(
                        [
                            "bash",
                            str(root / "scripts/verify-release-local.sh"),
                            "affected",
                            "native",
                            git_directory,
                            "--path",
                            "README.md",
                            *extra,
                        ],
                        cwd=root,
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    self.assertEqual(
                        expected, result.returncode, result.stdout + result.stderr
                    )
                    self.assertEqual([], list(scratch.iterdir()))


class SnapshotPlanningTests(unittest.TestCase):
    """The execution plan must describe copied files and a pinned baseline."""

    def test_snapshot_replans_captured_consumers_and_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "scripts").mkdir(parents=True)
            (source / "tests").mkdir()
            for relative in (
                planner.PLANNER,
                "scripts/check_public_safety.py",
                "scripts/verify-release-local.sh",
            ):
                shutil.copyfile(ROOT / relative, source / relative)
            (source / "tests/test_public_safety.py").write_text(
                "from scripts import check_public_safety\n", encoding="utf-8"
            )
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith("GIT_")
            }
            env.update(
                GIT_CONFIG_GLOBAL=os.devnull,
                GIT_CONFIG_NOSYSTEM="1",
                PYTHONDONTWRITEBYTECODE="1",
                GIT_AUTHOR_NAME="Validation",
                GIT_COMMITTER_NAME="Validation",
                GIT_AUTHOR_EMAIL="validation@example.com",
                GIT_COMMITTER_EMAIL="validation@example.com",
            )

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(source), *args], env=env, text=True
                ).strip()

            git("-c", "init.templateDir=", "init", "-q")
            git("add", "-A", "-f")
            baseline = git("commit-tree", git("write-tree"), "-m", "baseline")
            git("update-ref", "HEAD", baseline)
            prefix = (
                ["--plan"]
                if planner.PLANNER.endswith("run_dependency_light_tests.py")
                else []
            )

            def call(checkout, *args, input_text=None):
                result = subprocess.run(
                    [sys.executable, str(checkout / planner.PLANNER), *prefix, *args],
                    input=input_text,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                return json.loads(result.stdout)

            initial = call(source, "--path", "scripts/check_public_safety.py")
            ref_initial = call(source, "--base", baseline, "--head", baseline)
            self.assertEqual([], initial["ha_tests"])
            invalid = subprocess.run(
                [
                    sys.executable,
                    str(source / planner.PLANNER),
                    *prefix,
                    "--snapshot-plan",
                    "--git-directory",
                    str(source / ".git"),
                ],
                input=json.dumps({"base": "HEAD", "paths": []}),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(2, invalid.returncode)
            self.assertIn("pinned base", invalid.stderr)
            # Capture an added consumer after preview. The execution plan must
            # find it in the copied source, even after it disappears in the original.
            consumer = "tests/captured_test.py"
            (source / consumer).write_text(
                "from scripts import check_public_safety\n", encoding="utf-8"
            )
            snapshot = root / "snapshot"
            shutil.copytree(source, snapshot, ignore=shutil.ignore_patterns(".git"))
            dirty_snapshot = subprocess.run(
                [
                    sys.executable,
                    str(snapshot / planner.PLANNER),
                    *prefix,
                    "--snapshot-plan",
                    "--git-directory",
                    str(source / ".git"),
                ],
                input=json.dumps(ref_initial),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(2, dirty_snapshot.returncode)
            self.assertIn("clean candidate", dirty_snapshot.stderr)
            (source / consumer).unlink()
            runner = source / "scripts/verify-release-local.sh"
            original_runner = runner.read_text(encoding="utf-8")
            pin = planner.runner_dependencies(original_runner)["ruff"]
            runner.write_text(
                original_runner.replace(pin, "ruff==99.0.0"), encoding="utf-8"
            )
            (source / planner.PLANNER).write_text(
                "raise RuntimeError('changed source')\n"
            )
            captured = call(
                snapshot,
                "--snapshot-plan",
                "--git-directory",
                str(source / ".git"),
                input_text=json.dumps(initial),
            )
            self.assertEqual(baseline, captured["base"])
            self.assertIn(consumer, captured["ha_tests"])
            self.assertIn(consumer, captured["commands"]["current"])
            self.assertIn(pin, captured["commands"]["unit"])
            self.assertNotIn("ruff==99.0.0", captured["commands"]["unit"])


class SourceAdmissionTests(unittest.TestCase):
    """Admission precedes parsing."""

    def test_linked_owner_and_leaf_are_rejected_before_parsing(self):
        for linked_owner in (False, True):
            with (
                self.subTest(linked_owner=linked_owner),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory) / "source"
                root.mkdir()
                external = Path(directory) / "external"
                external.mkdir()
                (external / "input.py").write_text(
                    "not valid python!", encoding="utf-8"
                )
                scripts = root / "scripts"
                try:
                    if linked_owner:
                        scripts.symlink_to(external, target_is_directory=True)
                    else:
                        scripts.mkdir()
                        (scripts / "input.py").symlink_to(external / "input.py")
                except OSError as err:
                    self.skipTest(f"Cannot create synthetic links: {err}")
                with (
                    patch.object(planner, "ROOT", root),
                    patch.object(planner, "_imports") as read,
                    self.assertRaisesRegex(ValueError, "linked path"),
                ):
                    planner.build_plan(["README.md"])
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()

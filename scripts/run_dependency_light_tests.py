"""Run dependency-light tests, or select the real Home Assistant test lane."""

from __future__ import annotations

"""Select validation from changed files and their local Python consumers."""


import argparse
import ast
import json
import os
import re
import shlex
import subprocess
import sys
import unittest
from importlib import import_module
from pathlib import Path, PurePosixPath

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
    """Run one complete test lane with its native test collector."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home-assistant",
        action="store_true",
        help="Run native pytest discovery, excluding dependency-light modules",
    )
    parser.add_argument(
        "--test",
        action="append",
        help="Run only an existing module in the requested lane",
    )
    args = parser.parse_args(argv)
    try:
        if args.home_assistant:
            unit_files = dependency_light_test_files()
            if args.test:
                selected = validate_test_selection(args.test, home_assistant=True)
                return int(
                    import_module("pytest").main(
                        ["-q", *(str(path) for path in selected)]
                    )
                )
            pytest = import_module("pytest")

            return int(
                pytest.main(
                    [str(TESTS), "-q", *(f"--ignore={path}" for path in unit_files)]
                )
            )
        suite = _load_suite(
            validate_test_selection(args.test, home_assistant=False)
            if args.test
            else dependency_light_test_files()
        )
    except (OSError, RuntimeError, SyntaxError, ImportError) as err:
        print(f"Test discovery failed: {err}", file=sys.stderr)
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
            and node.name.startswith("test")
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


def validate_test_selection(
    paths: list[str], *, home_assistant: bool
) -> tuple[Path, ...]:
    """Reject missing, duplicate, traversal and wrong-lane test selections."""
    all_files = tuple(
        sorted(set(TESTS.glob("test_*.py")) | set(TESTS.glob("*_test.py")))
    )
    ha = set(discover_home_assistant_test_files(all_files))
    ha.update(path for path in all_files if path.name.endswith("_test.py"))
    allowed = ha if home_assistant else set(all_files) - ha
    selected = tuple(ROOT / path for path in paths)
    if len(set(selected)) != len(selected) or any(
        path not in allowed for path in selected
    ):
        raise RuntimeError(
            "Selected tests must be existing unique files in the requested lane"
        )
    return selected


ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "custom_components/beestat_statistics"
PLANNER = "scripts/run_dependency_light_tests.py"
TOOL_TESTS = {
    "tests/test_dependency_light_runner.py",
    "tests/test_ha_quality_static.py",
    "tests/test_validation_runner.py",
    "tests/test_public_safety.py",
    "tests/test_validation_selection.py",
}
METADATA_TEST = "tests/test_ha_quality_static.py"
EXTRA_DEPENDENCIES: dict[str, set[str]] = {
    "tests/test_ha_quality_static.py": {
        path.relative_to(ROOT).as_posix() for path in (ROOT / PRODUCT).rglob("*.py")
    }
    | {"README.md", "RELEASE_NOTES.md"},
}
JOBS = ("unit", "minimum", "current", "release", "hacs")


def _git(*args: str, git_directory: str | None = None) -> str:
    command = ["git", "--no-optional-locks"]
    if git_directory:
        command.extend([f"--git-dir={git_directory}", f"--work-tree={ROOT}"])
    else:
        command.extend(["-C", str(ROOT)])
    return subprocess.check_output([*command, *args], text=True)


def changed_paths(
    base: str | None,
    head: str | None,
    paths: list[str] | None,
    git_directory: str | None = None,
) -> list[str]:
    """Require a caller-owned comparison or explicit selection, never infer all."""
    if paths is not None:
        if base or head:
            raise ValueError("Use either --path or --base/--head")
        selected = paths
    else:
        if not base or not head:
            raise ValueError(
                "Affected validation requires --base and --head, or --path"
            )
        base_oid = _git(
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{base}^{{commit}}",
            git_directory=git_directory,
        ).strip()
        head_oid = _git(
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{head}^{{commit}}",
            git_directory=git_directory,
        ).strip()
        if _git("rev-parse", "HEAD", git_directory=git_directory).strip() != head_oid:
            raise ValueError("--head must identify the checked-out candidate")
        if _git(
            "status",
            "--porcelain",
            "--untracked-files=normal",
            git_directory=git_directory,
        ).strip():
            raise ValueError(
                "Ref comparison requires a clean candidate; use explicit paths for working edits"
            )
        selected = _git(
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            base_oid,
            head_oid,
            "--",
            git_directory=git_directory,
        ).split("\0")[:-1]
    for path in selected:
        parsed = PurePosixPath(path)
        if (
            not path
            or "\\" in path
            or parsed.is_absolute()
            or ".." in parsed.parts
            or ":" in path
            or path.startswith("-")
        ):
            raise ValueError(f"Not a repository-relative path: {path!r}")
    return sorted(set(selected))


def _imports(path: str, files: set[str]) -> set[str]:
    """Read syntax only; do not import the product while planning validation."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)
    result: set[str] = set()
    package = path.removesuffix(".py").split("/")[:-1]
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            prefix = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*prefix, *([node.module] if node.module else [])])
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        for name in names:
            stem = name.replace(".", "/")
            if path.startswith("tests/") and (
                stem.startswith(PRODUCT + "/") or stem == PRODUCT
            ):
                initializer = PRODUCT + "/__init__.py"
                if initializer in files:
                    result.add(initializer)
            result.update(
                candidate
                for candidate in (f"{stem}.py", f"{stem}/__init__.py")
                if candidate in files
            )
        # Direct-file test loaders avoid importing the HA package initializer.
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.endswith(".py")
        ):
            candidates = {f"{PRODUCT}/{node.value}", f"scripts/{node.value}"}
            result.update(candidates & files)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_load_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            candidate = f"{PRODUCT}/{node.args[0].value}.py"
            if candidate in files:
                result.add(candidate)
    result.update(EXTRA_DEPENDENCIES.get(path, set()))
    return result


def _test_files(files: set[str]) -> tuple[set[str], set[str]]:
    tests = {
        path
        for path in files
        if path.startswith("tests/")
        and (Path(path).name.startswith("test_") or path.endswith("_test.py"))
    }
    ha = {
        path.relative_to(ROOT).as_posix()
        for path in discover_home_assistant_test_files(
            tuple(ROOT / path for path in sorted(tests))
        )
    }
    ha.update(path for path in tests if path.endswith("_test.py"))
    return tests - ha, ha


def _consumer_closure(changed: set[str], dependencies: dict[str, set[str]]) -> set[str]:
    impacted = set(changed)
    while True:
        consumers = {path for path, uses in dependencies.items() if uses & impacted}
        if consumers <= impacted:
            return impacted
        impacted.update(consumers)


def runner_dependencies(source: str) -> dict[str, str]:
    """Read exact dependency declarations from the existing Bash owner."""
    result = {}

    def one(pattern: str) -> str:
        values = set(re.findall(pattern, source, re.MULTILINE))
        if len(values) != 1:
            raise ValueError(f"Unresolved runner dependency declaration: {pattern}")
        return values.pop()

    for name in ("python_image", "actionlint_image", "hassfest_image"):
        result[name] = one(rf'^{name}="([^"$]+)"$')
    for package in ("ruff", "mypy", "zizmor", "shellcheck-py"):
        result[package] = one(rf'"({package}==[\w.+-]+)"')
    result["actionlint"] = one(
        r"go install (github.com/rhysd/actionlint/cmd/actionlint@\S+)"
    )
    for lane in ("minimum", "current"):
        body = one(rf"(?s)^run_{lane}\(\) \{{\n(.*?)^\}}")
        pins = re.findall(r'"(pytest-homeassistant-custom-component==[\w.+-]+)"', body)
        if len(pins) != 1:
            raise ValueError(f"Unresolved {lane} HA harness declaration")
        result[lane] = pins[0]
        requirements = re.findall(
            r"python -m pip install --upgrade -r ([\w./-]+)(?=\s|$)", body
        )
        if len(requirements) != 1:
            raise ValueError(f"Unresolved {lane} requirements declaration")
        result[lane + "_requirements"] = requirements[0]
    return result


def workflow_dependencies(source: str) -> dict[str, tuple[str, ...]]:
    """Read environment/action inputs in the maintained workflow's job blocks."""
    jobs = dict(
        re.findall(
            r"(?ms)^  ([a-z_]+):\n(.*?)(?=^  [a-z_]+:|\Z)",
            source.split("jobs:\n", 1)[-1],
        )
    )
    names = {
        "unit": "unit",
        "home_assistant_minimum": "minimum",
        "home_assistant_current": "current",
        "hassfest": "release",
        "hacs": "hacs",
    }
    if set(jobs) - set(names) - {"plan", "release_gate"} or set(names) - set(jobs):
        raise ValueError("Unresolved workflow job dependency mapping")
    result = {}
    for name, lane in names.items():
        values = re.findall(
            r"^\s+(?:- )?(uses|python-version|runs-on|category):\s*([^#\n]+)",
            jobs[name],
            re.MULTILINE,
        )
        if not values or any("${{" in value for _, value in values):
            raise ValueError(f"Unresolved workflow dependencies: {name}")
        result[lane] = tuple(
            sorted(
                f"{key}:{value.strip().strip(chr(34)).strip(chr(39))}"
                for key, value in values
            )
        )
    return result


def _select_environment(
    plan: dict, lane: str, ha_files: set[str], files: set[str]
) -> None:
    plan[lane] = True
    plan["lane_tests"][lane] = sorted(ha_files)
    if lane == "minimum":
        plan["lane_typing"][lane] = sorted(
            path for path in files if path.startswith(PRODUCT + "/")
        )


def _select_unit_environment(plan: dict, unit_files: set[str], files: set[str]) -> None:
    plan["unit_tests"] = sorted(unit_files)
    plan["python"] = sorted(files)
    plan["workflow"] = True
    plan["shell"] = True


def _route_runner_dependencies(
    before: str,
    after: str,
    files: set[str],
    unit_files: set[str],
    ha_files: set[str],
    plan: dict,
) -> None:
    old, new = runner_dependencies(before), runner_dependencies(after)
    changed = {key for key in new if new[key] != old[key]}
    for lane in ("minimum", "current"):
        if changed & {lane, lane + "_requirements", "python_image"}:
            _select_environment(plan, lane, ha_files, files)
    if "python_image" in changed:
        _select_unit_environment(plan, unit_files, files)
    if "ruff" in changed:
        plan["python"] = sorted(files)
    if "mypy" in changed:
        plan["minimum"] = True
        plan["lane_typing"]["minimum"] = sorted(
            path for path in files if path.startswith(PRODUCT + "/")
        )
    plan["workflow"] |= bool(
        changed & {"zizmor", "actionlint", "actionlint_image", "shellcheck-py"}
    )
    plan["shell"] |= "shellcheck-py" in changed
    plan["release"] |= "hassfest_image" in changed


def _route_dependency_changes(
    paths: list[str],
    base: str,
    git_directory: str | None,
    files: set[str],
    unit_files: set[str],
    ha_files: set[str],
    plan: dict,
) -> None:
    """Compare dependency inputs, rather than treating every runner edit as HA work."""
    for path in paths:
        if path != "scripts/verify-release-local.sh" and not path.startswith(
            ".github/workflows/"
        ):
            continue
        try:
            if (
                path.startswith(".github/")
                and path != ".github/workflows/validate.yaml"
            ):
                raise ValueError(f"Unresolved workflow dependency owner: {path}")
            before = _git("show", f"{base}:{path}", git_directory=git_directory)
            after = (ROOT / path).read_text(encoding="utf-8")
            if path.endswith(".sh"):
                _route_runner_dependencies(
                    before, after, files, unit_files, ha_files, plan
                )
            else:
                old, new = workflow_dependencies(before), workflow_dependencies(after)
                for lane in new:
                    if old[lane] == new[lane]:
                        continue
                    if lane in {"minimum", "current"}:
                        _select_environment(plan, lane, ha_files, files)
                    elif lane == "unit":
                        _select_unit_environment(plan, unit_files, files)
                    else:
                        plan[lane] = True
        except (OSError, ValueError, subprocess.CalledProcessError) as err:
            plan["unresolved"].append(
                f"{path}: dependency comparison unavailable: {err}"
            )


def build_plan(
    paths: list[str], base: str = "HEAD", git_directory: str | None = None
) -> dict:
    files = {
        path.relative_to(ROOT).as_posix()
        for owner in ("custom_components", "tests", "scripts")
        for path in (ROOT / owner).rglob("*.py")
        if "__pycache__" not in path.parts
    }
    dependencies = {path: _imports(path, files) for path in files}
    unit_files, ha_files = _test_files(files)
    for test in ha_files:
        dependencies[test].update(path for path in files if path == "tests/conftest.py")
    impacted = _consumer_closure(set(paths), dependencies)
    selected = (unit_files | ha_files) & impacted
    plan = {
        "paths": paths,
        "unit_tests": sorted(selected & unit_files),
        "ha_tests": sorted(selected & ha_files),
        "python": sorted(path for path in paths if path in files),
        "typing": sorted(
            path
            for path in impacted
            if path in files and path.startswith(PRODUCT + "/")
        ),
        "lane_tests": {"minimum": [], "current": []},
        "lane_typing": {"minimum": [], "current": []},
        "workflow": False,
        "shell": False,
        "safety": bool(paths),
        "minimum": False,
        "current": False,
        "release": False,
        "hacs": False,
        "unresolved": [],
    }
    for path in paths:
        consumers = _consumer_closure({path}, dependencies) & (unit_files | ha_files)
        _route_path(path, files, consumers, ha_files, plan)
    _route_dependency_changes(
        paths, base, git_directory, files, unit_files, ha_files, plan
    )
    if selected & ha_files:
        plan["current"] = True
        if any(path.startswith(PRODUCT + "/") for path in paths):
            plan["minimum"] = True
    for lane in ("minimum", "current"):
        if plan[lane]:
            plan["lane_tests"][lane] = sorted(
                set(plan["lane_tests"][lane])
                | (
                    (selected & ha_files)
                    if lane == "current"
                    or any(path.startswith(PRODUCT + "/") for path in paths)
                    else set()
                )
            )
            plan["lane_typing"][lane] = sorted(
                set(plan["lane_typing"][lane]) | set(plan["typing"])
            )
    plan["ha_tests"] = sorted(set().union(*map(set, plan["lane_tests"].values())))
    plan["jobs"] = {
        job: bool(plan[job])
        if job != "unit"
        else bool(
            plan["safety"]
            or plan["unit_tests"]
            or plan["python"]
            or plan["workflow"]
            or plan["shell"]
        )
        for job in JOBS
    }
    return plan


def _route_path(
    path: str, files: set[str], selected: set[str], ha_files: set[str], plan: dict
) -> None:
    if path in files:
        if path.startswith(PRODUCT + "/"):
            # Hassfest consumes integration imports and config-flow/schema declarations.
            plan["release"] = True
        if path.startswith(PRODUCT + "/") and not selected & ha_files:
            plan["unresolved"].append(f"No HA consumer found for {path}")
        if path.startswith("scripts/") and not selected:
            plan["unresolved"].append(f"No test consumer found for {path}")
        return
    if path in {"requirements-ha-test.txt", "requirements-ha-current.txt"}:
        lane = "minimum" if path.endswith("-test.txt") else "current"
        _select_environment(plan, lane, ha_files, files)
        plan["unit_tests"] = sorted(set(plan["unit_tests"]) | {METADATA_TEST})
    elif path in {
        "scripts/verify-release-local.sh",
        "scripts/verify-release-local.ps1",
    } or path.startswith(".github/"):
        plan["unit_tests"] = sorted(set(plan["unit_tests"]) | TOOL_TESTS)
        plan["workflow"] |= path.startswith(".github/")
        plan["shell"] |= path.endswith(".sh")
    elif (
        path.startswith(PRODUCT + "/")
        and Path(path).suffix in {".json", ".yaml", ".png"}
    ) or path == "hacs.json":
        plan["unit_tests"] = sorted(set(plan["unit_tests"]) | {METADATA_TEST})
        plan["release"] = True
        plan["hacs"] |= path == "hacs.json" or path.endswith("manifest.json")
    elif PurePosixPath(path).parent == PurePosixPath("docs") and path.endswith(".md"):
        # Include removed documents consumed by retained navigation/link contracts.
        plan["unit_tests"] = sorted(set(plan["unit_tests"]) | {METADATA_TEST})
    elif path == "pyproject.toml":
        plan["unresolved"].append(
            "pyproject.toml: select the affected tool configuration explicitly after review"
        )
    elif path.endswith(".md") or path in {
        "LICENSE",
        ".gitignore",
        ".gitattributes",
    }:
        pass
    else:
        plan["unresolved"].append(path)


def lane_command(plan: dict, lane: str) -> str:
    """Return quoted commands for the existing isolated environment runner."""
    pins = runner_dependencies(
        (ROOT / "scripts/verify-release-local.sh").read_text(encoding="utf-8")
    )
    commands: list[str] = []
    if lane in {"minimum", "current"}:
        if plan["lane_typing"][lane]:
            commands.append(
                shlex.join(["python", "-m", "pip", "install", pins["mypy"]])
            )
        # The runner installs the lane's exact requirements before this command.
        commands.append("python -m pip check")
        if plan["lane_typing"][lane]:
            commands.append(
                shlex.join(["python", "-m", "mypy", *plan["lane_typing"][lane]])
            )
        if plan["lane_tests"][lane]:
            commands.append(
                shlex.join(
                    [
                        "python",
                        PLANNER,
                        "--home-assistant",
                        *(
                            arg
                            for path in plan["lane_tests"][lane]
                            for arg in ("--test", path)
                        ),
                    ]
                )
            )
    else:
        if plan["python"]:
            commands.extend(
                [
                    shlex.join(["python", "-m", "pip", "install", pins["ruff"]]),
                    shlex.join(
                        ["python", "-m", "ruff", "format", "--check", *plan["python"]]
                    ),
                    shlex.join(["python", "-m", "ruff", "check", *plan["python"]]),
                    shlex.join(["python", "-m", "compileall", "-q", *plan["python"]]),
                ]
            )
        if plan["workflow"]:
            commands.extend(
                [
                    shlex.join(["python", "-m", "pip", "install", pins["zizmor"]]),
                    "zizmor --strict-collection --persona auditor .",
                ]
            )
        if plan["shell"]:
            commands.extend(
                [
                    shlex.join(
                        ["python", "-m", "pip", "install", pins["shellcheck-py"]]
                    ),
                    "shellcheck scripts/verify-release-local.sh",
                ]
            )
        if plan["unit_tests"]:
            commands.append(
                shlex.join(
                    [
                        "python",
                        PLANNER,
                        *(
                            arg
                            for path in plan["unit_tests"]
                            for arg in ("--test", path)
                        ),
                    ]
                )
            )
        if plan["safety"]:
            commands.append("python scripts/check_public_safety.py")
    return " &&\n".join(commands) or ":"


def plan_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-directory")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--path", action="append")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--command", choices=JOBS)
    parser.add_argument("--github-output", action="store_true")
    parser.add_argument(
        "--full", action="store_true", help="Explicit complete workflow dispatch"
    )
    args = parser.parse_args(argv)
    try:
        plan = build_plan(
            []
            if args.full
            else changed_paths(args.base, args.head, args.path, args.git_directory),
            base=args.base or "HEAD",
            git_directory=args.git_directory,
        )
        if args.full:
            if args.command or args.path is not None or args.base or args.head:
                raise ValueError(
                    "--full is an explicit workflow plan, not an affected command"
                )
            plan["jobs"] = dict.fromkeys(JOBS, True)
        plan["mode"] = "full" if args.full else "affected"
        if plan["unresolved"]:
            raise ValueError(
                "Unresolved applicability: " + "; ".join(plan["unresolved"])
            )
        if args.command:
            if not plan["jobs"][args.command]:
                raise ValueError(f"The plan did not select {args.command}")
            print(lane_command(plan, args.command))
        elif args.github_output:
            with Path(os.environ["GITHUB_OUTPUT"]).open(
                "a", encoding="utf-8"
            ) as output:
                output.writelines(
                    f"{job}={str(selected).lower()}\n"
                    for job, selected in plan["jobs"].items()
                )
                output.write(f"plan={json.dumps(plan, separators=(',', ':'))}\n")
        else:
            print(json.dumps(plan, indent=2))
    except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as err:
        print(f"Validation plan failed: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(plan_main(sys.argv[2:]) if sys.argv[1:2] == ["--plan"] else main())

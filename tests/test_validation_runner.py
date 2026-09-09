"""Exercise validation orchestration without downloading tools or containers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
GIT = shutil.which("git")
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

FAKE_TOOL = r"""
import json
import os
import shutil
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
event = {"tool": name, "args": args, "cwd": os.getcwd(), "path": sys.argv[0]}
kind = name
if name == "podman":
    mount = args[args.index("-v") + 1].split(":", 1)[0]
    event["mount"] = mount
    event["files"] = sorted(
        str(path.relative_to(mount)) for path in Path(mount).rglob("*")
        if ".git" not in path.relative_to(mount).parts and path.is_file()
    )
    if any("actionlint@" in arg for arg in args):
        kind = "actionlint"
    elif any("hassfest@" in arg for arg in args):
        kind = "release"
    elif "requirements-ha-test.txt" in args[-1]:
        kind = "minimum"
    elif "requirements-ha-current.txt" in args[-1]:
        kind = "current"
    else:
        kind = "unit-python"
elif name == "go":
    target = Path(os.environ["GOBIN"]) / "actionlint"
    shutil.copyfile(__file__, target)
    target.chmod(0o755)
elif name == "python" and args[:2] == ["-m", "venv"]:
    target = Path(args[2]) / "bin" / "python"
    target.parent.mkdir()
    shutil.copyfile(__file__, target)
    target.chmod(0o755)
    event["environment"] = args[2]
elif name == "python" and args[:2] == ["-m", "pip"]:
    kind = "pip"
event["kind"] = kind
with open(os.environ["VALIDATION_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(event) + "\n")
if os.environ.get("VALIDATION_FAIL") == kind:
    sys.exit(23)
"""


@unittest.skipUnless(os.name == "posix" and BASH and GIT, "requires Bash and Git")
class ValidationRunnerTests(unittest.TestCase):
    """Use real Bash, Git, and filesystem operations with fake external tools."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="validation runner ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "source with spaces"
        self.repo.mkdir()
        (self.repo / "scripts").mkdir()
        self.runner = self.repo / "scripts" / "verify-release-local.sh"
        shutil.copyfile(ROOT / "scripts/verify-release-local.sh", self.runner)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        for name in (
            "podman",
            "docker",
            "go",
            "python",
            "pytest",
            "zizmor",
            "shellcheck",
        ):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n{FAKE_TOOL}", encoding="utf-8")
            path.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(self.scratch),
            "VALIDATION_LOG": str(self.log),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.env.pop("VALIDATION_FAIL", None)
        self.git("init", "-q")
        (self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        self.git("add", "-A")

    def git(self, *args: str) -> None:
        subprocess.run(
            [str(GIT), "-C", str(self.repo), *args],
            env=self.env,
            check=True,
            capture_output=True,
        )

    def run_validation(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BASH), str(self.runner), *args],
            cwd=self.root,
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def events(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_invalid_arguments_fail_before_snapshot_or_tools(self) -> None:
        for args in (
            ("bad-mode", "container"),
            ("unit", "bad-backend"),
            ("unit", "container", "", "extra"),
        ):
            with self.subTest(args=args):
                result = self.run_validation(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.events(), [])
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_snapshot_copies_current_candidate_and_cleans_up(self) -> None:
        (self.repo / "deleted.txt").write_text("staged", encoding="utf-8")
        self.git("add", "deleted.txt")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "ignored.txt").write_text("private", encoding="utf-8")
        (self.repo / "new file.txt").write_text("candidate", encoding="utf-8")
        result = self.run_validation("release", "container")
        self.assertEqual(result.returncode, 0, result.stderr)
        event = self.events()[0]
        self.assertIn("new file.txt", event["files"])
        self.assertNotIn("ignored.txt", event["files"])
        self.assertNotIn("deleted.txt", event["files"])
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertFalse(Path(str(event["mount"])).exists())
        self.assertTrue((self.repo / "new file.txt").exists())

    def test_all_retains_early_failure_and_runs_remaining_lanes(self) -> None:
        self.env["VALIDATION_FAIL"] = "actionlint"
        result = self.run_validation("all", "container")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(
            [event["kind"] for event in self.events()],
            ["actionlint", "minimum", "current", "release"],
        )
        self.assertIn("unit: FAIL (exit 23)", result.stderr)
        self.assertIn("current: PASS", result.stdout)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_all_success_reports_every_lane(self) -> None:
        result = self.run_validation("all", "container")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count(": PASS"), 4)
        self.assertEqual(
            [event["kind"] for event in self.events()],
            ["actionlint", "unit-python", "minimum", "current", "release"],
        )
        self.assertEqual(len({event["mount"] for event in self.events()}), 1)

    def test_container_lanes_reuse_only_download_cache(self) -> None:
        for _ in range(2):
            result = self.run_validation("all", "container")
            self.assertEqual(result.returncode, 0, result.stderr)
        python_events = [
            event
            for event in self.events()
            if event["kind"] in {"unit-python", "minimum", "current"}
        ]
        self.assertEqual(len(python_events), 6)
        for event in python_events:
            args = event["args"]
            self.assertEqual(args[:2], ["run", "--rm"])
            self.assertIn("PIP_CACHE_DIR=/pip-cache", args)
            self.assertEqual(
                args[args.index("--mount") + 1],
                "type=volume,source=beestat-statistics-validation-pip,target=/pip-cache",
            )
            self.assertIn("python -m pip install", args[-1])
            if event["kind"] in {"minimum", "current"}:
                self.assertIn("python -m pip check", args[-1])
                self.assertIn("pytest tests -q", args[-1])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_failed_snapshot_does_not_run_validation(self) -> None:
        result = self.run_validation("release", "container", str(self.root / "missing"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_explicit_git_directory_handles_external_cwd_and_spaces(self) -> None:
        result = self.run_validation("release", "container", str(self.repo / ".git"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[0]["kind"], "release")

    def test_native_actionlint_uses_repo_cwd_and_cleans_failure(self) -> None:
        self.env["VALIDATION_FAIL"] = "actionlint"
        result = self.run_validation("unit", "native")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            [event["kind"] for event in self.events()], ["go", "actionlint"]
        )
        self.assertEqual(self.events()[-1]["cwd"], str(self.repo))
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_native_lanes_have_distinct_temporary_environments(self) -> None:
        for lane in ("minimum", "current"):
            result = self.run_validation(lane, "native")
            self.assertEqual(result.returncode, 0, result.stderr)
        environments = [
            event["environment"] for event in self.events() if "environment" in event
        ]
        self.assertEqual(len(environments), 2)
        self.assertNotEqual(*environments)
        self.assertTrue(all(not Path(str(path)).exists() for path in environments))
        pip_events = [event for event in self.events() if event["kind"] == "pip"]
        self.assertTrue(pip_events)
        for event in pip_events:
            self.assertEqual(event["cwd"], str(self.repo))
            self.assertTrue(
                any(str(event["path"]).startswith(str(path)) for path in environments)
            )

    def test_native_install_failure_cannot_reach_tests(self) -> None:
        self.env["VALIDATION_FAIL"] = "pip"
        result = self.run_validation("minimum", "native")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([event["kind"] for event in self.events()], ["python", "pip"])
        self.assertNotIn("minimum: PASS", result.stdout)
        self.assertEqual(list(self.scratch.iterdir()), [])


@unittest.skipUnless(POWERSHELL, "requires PowerShell")
class PowerShellValidationRunnerTests(unittest.TestCase):
    """Verify the Windows wrapper's argument and failure boundaries."""

    def run_wrapper(self, failure: str = "") -> tuple[int, str, list[list[str]]]:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            log = directory / "calls.jsonl"
            harness = directory / "test-wrapper.ps1"
            harness.write_text(
                r"""
$ErrorActionPreference = "Stop"
$script:wslCalls = 0
function global:wsl.exe {
    $script:wslCalls++
    ConvertTo-Json -Compress -InputObject @($args) | Add-Content $env:VALIDATION_LOG
    $global:LASTEXITCODE = 0
    if ($script:wslCalls -eq 1) {
        if ($env:VALIDATION_FAIL -eq "map") { return }
        "/mounted/source with spaces"
    } elseif ($script:wslCalls -eq 2) {
        "/mounted/git with spaces"
    } elseif ($env:VALIDATION_FAIL -eq "validation") {
        $global:LASTEXITCODE = 23
    }
}
function global:git {
    $global:LASTEXITCODE = 0
    if ($env:VALIDATION_FAIL -eq "git") {
        $global:LASTEXITCODE = 23
        return
    }
    "C:" + "\source with spaces\.git"
}
try {
    & $env:VALIDATION_SCRIPT -Mode current
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-File", str(harness)],
                env={
                    **os.environ,
                    "VALIDATION_FAIL": failure,
                    "VALIDATION_LOG": str(log),
                    "VALIDATION_SCRIPT": str(ROOT / "scripts/verify-release-local.ps1"),
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            calls = (
                [
                    json.loads(line)
                    for line in log.read_text(encoding="utf-8-sig").splitlines()
                ]
                if log.exists()
                else []
            )
            return result.returncode, result.stdout + result.stderr, calls

    def test_passes_mapped_paths_as_separate_arguments(self) -> None:
        status, output, calls = self.run_wrapper()
        self.assertEqual(status, 0, output)
        self.assertEqual(
            calls[-1],
            [
                "-d",
                "Ubuntu-24.04",
                # PowerShell consumes -- when calling a function mock.
                "bash",
                "/mounted/source with spaces/scripts/verify-release-local.sh",
                "current",
                "container",
                "/mounted/git with spaces",
            ],
        )

    def test_empty_mapping_and_git_failure_keep_useful_errors(self) -> None:
        for failure, message in (
            ("map", "Could not map the repository into Ubuntu-24.04"),
            ("git", "Could not resolve the repository Git directory"),
        ):
            with self.subTest(failure=failure):
                status, output, calls = self.run_wrapper(failure)
                self.assertEqual(status, 1)
                self.assertIn(message, output)
                self.assertEqual(len(calls), 1)

    def test_native_validation_failure_is_not_reported_as_success(self) -> None:
        status, output, calls = self.run_wrapper("validation")
        self.assertEqual(status, 1)
        self.assertIn("Local release validation failed with exit code 23", output)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()

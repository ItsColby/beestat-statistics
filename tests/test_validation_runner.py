"""Exercise validation orchestration without downloading tools or containers."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
GIT = shutil.which("git")
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

AFFECTED_PLANNER = r"""
import json
import os
import sys

if "--snapshot-plan" in sys.argv and os.environ.get("VALIDATION_MUTATE_SOURCE"):
    from pathlib import Path
    source = Path(os.environ["VALIDATION_MUTATE_SOURCE"])
    (source / "scripts/run_dependency_light_tests.py").write_text("raise RuntimeError('changed original')")
if "--command" in sys.argv:
    raise RuntimeError("Lane command must come from the captured plan")
else:
    selected = os.environ["VALIDATION_PLAN_LANES"].split()
    print(json.dumps({"jobs": {lane: lane in selected for lane in
          ("unit", "minimum", "current", "release", "hacs")}, "workflow": True,
          "safety": True, "paths": [], "base": "HEAD",
          "commands": {lane: "echo snapshot-command" for lane in selected}}))
"""

FAKE_TOOL = r"""
import json
import os
import shutil
import subprocess
import sys
import time
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
    event["tracked_files"] = subprocess.check_output(
        ["git", "-C", mount, "ls-files", "-z"], text=True
    ).split("\0")[:-1]
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
    if any(arg.startswith("shellcheck-py==") for arg in args):
        target = Path(sys.argv[0]).parent / "shellcheck"
        shutil.copyfile(__file__, target)
        target.chmod(0o755)
event["kind"] = kind
with open(os.environ["VALIDATION_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(event) + "\n")
if name == "actionlint":
    shellcheck = shutil.which("shellcheck")
    assert shellcheck == str(Path(sys.argv[0]).parent / "shellcheck")
    subprocess.run([shellcheck, "--version"], check=True)
if os.environ.get("VALIDATION_OVERLAP") and name == "podman" and kind != "actionlint":
    events = Path(os.environ["VALIDATION_LOG"]).parent
    lane = "unit" if kind == "unit-python" else kind
    lanes = set(os.environ.get("VALIDATION_LANES", "unit minimum current release").split())
    if os.environ.get("VALIDATION_AFFECTED"):
        if lane in {"minimum", "current"} and "unit" in lanes:
            assert (events / "unit.done").exists(), "HA started before static validation"
        if lane == "release":
            for peer in lanes & {"minimum", "current"}:
                assert (events / (peer + ".done")).exists(), "Release started before HA"
        overlap = lanes & {"minimum", "current"} if lane in {"minimum", "current"} else {lane}
    else:
        overlap = lanes
    assert args[:2] == ["run", "--rm"]
    if lane != "release":
        assert args[args.index("-v") + 1].endswith(":/workspace:ro")
    (events / (lane + ".started")).touch()
    deadline = time.monotonic() + 5
    while not all((events / (peer + ".started")).exists() for peer in overlap):
        if time.monotonic() > deadline:
            raise SystemExit("The four validation lanes did not overlap")
        time.sleep(0.01)
    if os.environ.get("VALIDATION_INTERRUPT") and (
        not os.environ.get("VALIDATION_AFFECTED") or lane in {"minimum", "current"}
    ):
        while not (events / "interrupt.sent").exists():
            if time.monotonic() > deadline:
                raise SystemExit("The runner was not interrupted")
            time.sleep(0.01)
        time.sleep(0.1)
    failure = os.environ.get("VALIDATION_FAIL")
    failed_lane = "unit" if failure == "unit-python" else failure
    if failed_lane in overlap and failed_lane != lane:
        while not (events / (failed_lane + ".done")).exists():
            if time.monotonic() > deadline:
                raise SystemExit("The failing lane did not finish")
            time.sleep(0.01)
        time.sleep(0.05)
    assert Path(mount).is_dir(), "Payload removed before every lane finished"
    (events / (lane + ".done")).touch()
if os.environ.get("VALIDATION_FAIL") in {kind, "both" if kind in {"minimum", "current"} else kind}:
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
        shutil.copyfile(
            ROOT / "scripts/check_public_safety.py",
            self.repo / "scripts/check_public_safety.py",
        )
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
            "VALIDATION_PYTHON": sys.executable,
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

    def prepare_affected(self, lanes: tuple[str, ...], only: str = "") -> list[str]:
        (self.repo / "scripts/run_dependency_light_tests.py").write_text(
            AFFECTED_PLANNER, encoding="utf-8"
        )
        self.env.update(
            VALIDATION_PYTHON=sys.executable,
            VALIDATION_PLAN_LANES=" ".join(lanes),
            VALIDATION_LANES=only or " ".join(lanes),
            VALIDATION_AFFECTED="1",
            VALIDATION_OVERLAP="1",
        )
        self.log.unlink(missing_ok=True)
        for suffix in ("started", "done"):
            for marker in self.root.glob(f"*.{suffix}"):
                marker.unlink()
        return ["affected", "container", "", *(["--only", only] if only else [])]

    def test_captured_commands_survive_original_planner_changes(self) -> None:
        args = self.prepare_affected(("unit", "minimum", "current", "release"))
        self.env["VALIDATION_MUTATE_SOURCE"] = str(self.repo)
        result = self.run_validation(*args)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        commands = [
            event["args"][-1]
            for event in self.events()
            if event["kind"] in {"unit-python", "minimum", "current"}
        ]
        self.assertEqual(3, len(commands))
        self.assertTrue(all("snapshot-command" in command for command in commands))
        self.assertEqual([], list(self.scratch.iterdir()))

    def test_linked_snapshot_input_stops_before_container_and_cleans(self) -> None:
        target = self.root / "external.py"
        target.write_text(
            "raise RuntimeError('must not be acquired')", encoding="utf-8"
        )
        (self.repo / "linked.py").symlink_to(target)
        result = self.run_validation("release", "container")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.events())
        self.assertEqual([], list(self.scratch.iterdir()))

    def test_snapshot_ignores_inherited_templates_and_global_hooks(self) -> None:
        template = self.root / "inherited template"
        hooks = template / "hooks"
        hooks.mkdir(parents=True)
        hook = hooks / "post-index-change"
        hook.write_text(
            '#!/bin/sh\nprintf triggered > "$VALIDATION_HOOK_MARKER"\nexit 73\n',
            encoding="utf-8",
        )
        hook.chmod(0o755)
        marker = self.root / "hook-ran"
        self.env["VALIDATION_HOOK_MARKER"] = str(marker)
        for setting in ("template", "hooks"):
            with self.subTest(setting=setting):
                if setting == "template":
                    self.env["GIT_TEMPLATE_DIR"] = str(template)
                else:
                    self.env.pop("GIT_TEMPLATE_DIR")
                    self.env["GIT_CONFIG_GLOBAL"] = str(self.root / "global.gitconfig")
                    self.git("config", "--global", "core.hooksPath", str(hooks))
                result = self.run_validation("release", "container")
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertFalse(marker.exists())
                self.assertEqual([], list(self.scratch.iterdir()))

    def test_affected_selection_preserves_order_overlap_and_exclusions(self) -> None:
        for lanes, only in (
            (("unit", "minimum", "current", "release"), ""),
            (("minimum", "current"), ""),
            (("unit",), ""),
            (("minimum",), ""),
            (("current",), ""),
            (("release",), ""),
            (("minimum", "current"), "current"),
        ):
            with self.subTest(lanes=lanes, only=only):
                args = self.prepare_affected(lanes, only)
                result = self.run_validation(*args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(
                    {path.stem for path in self.root.glob("*.done")},
                    set((only,) if only else lanes),
                )
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_affected_failure_drains_selected_lanes_and_blocks_release(self) -> None:
        for failure in ("unit-python", "minimum", "current", "both"):
            with self.subTest(failure=failure):
                args = self.prepare_affected(("unit", "minimum", "current", "release"))
                self.env["VALIDATION_FAIL"] = failure
                result = self.run_validation(*args)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((self.root / "release.done").exists())
                if failure == "unit-python":
                    self.assertFalse((self.root / "minimum.started").exists())
                    self.assertFalse((self.root / "current.started").exists())
                else:
                    for lane in ("minimum", "current"):
                        self.assertTrue((self.root / f"{lane}.done").exists())
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_affected_interrupt_drains_selected_lanes(self) -> None:
        for interrupt in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(interrupt=interrupt):
                args = self.prepare_affected(("unit", "minimum", "current", "release"))
                self.env["VALIDATION_INTERRUPT"] = "1"
                (self.root / "interrupt.sent").unlink(missing_ok=True)
                with subprocess.Popen(
                    [str(BASH), str(self.runner), *args],
                    cwd=self.root,
                    env=self.env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ) as process:
                    try:
                        deadline = time.monotonic() + 10
                        while not all(
                            (self.root / f"{lane}.started").exists()
                            for lane in ("minimum", "current")
                        ):
                            if (
                                process.poll() is not None
                                or time.monotonic() > deadline
                            ):
                                self.fail("The selected HA lanes did not start")
                            time.sleep(0.01)
                        process.send_signal(interrupt)
                        (self.root / "interrupt.sent").touch()
                        stdout, stderr = process.communicate(timeout=30)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=10)
                self.assertEqual(process.returncode, 128 + interrupt, stdout + stderr)
                for lane in ("minimum", "current"):
                    self.assertTrue((self.root / f"{lane}.done").exists())
                self.assertFalse((self.root / "release.done").exists())
                self.assertEqual(list(self.scratch.iterdir()), [])

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
        result = self.run_validation("release", "container", str(self.repo / ".git"))
        self.assertEqual(result.returncode, 0, result.stderr)
        event = self.events()[0]
        self.assertEqual(event["kind"], "release")
        self.assertIn("new file.txt", event["files"])
        self.assertNotIn("ignored.txt", event["files"])
        self.assertNotIn("deleted.txt", event["files"])
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertFalse(Path(str(event["mount"])).exists())
        self.assertTrue((self.repo / "new file.txt").exists())

    def test_snapshot_keeps_source_tracked_ignored_paths_in_its_index(self) -> None:
        (self.repo / "ignored.txt").write_text("tracked candidate", encoding="utf-8")
        self.git("add", "-f", "ignored.txt")
        (self.repo / ".gitignore").write_text("ignored*.txt\n", encoding="utf-8")
        (self.repo / "ignored-untracked.txt").write_text(
            "private scratch", encoding="utf-8"
        )

        result = self.run_validation("release", "container")

        self.assertEqual(result.returncode, 0, result.stderr)
        event = self.events()[0]
        self.assertIn("ignored.txt", event["files"])
        self.assertIn("ignored.txt", event["tracked_files"])
        self.assertNotIn("ignored-untracked.txt", event["files"])
        self.assertNotIn("ignored-untracked.txt", event["tracked_files"])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_all_retains_early_failure_and_runs_remaining_lanes(self) -> None:
        self.env["VALIDATION_FAIL"] = "actionlint"
        result = self.run_validation("all", "container")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        kinds = [event["kind"] for event in self.events()]
        self.assertCountEqual(kinds, ["actionlint", "minimum", "current", "release"])
        self.assertIn("unit: FAIL (exit 23)", result.stderr)
        self.assertIn("current: PASS", result.stdout)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_all_lanes_overlap_and_finish_before_cleanup(self) -> None:
        self.env["VALIDATION_OVERLAP"] = "1"
        result = self.run_validation("all", "container")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count(": PASS"), 4)
        kinds = [event["kind"] for event in self.events()]
        self.assertCountEqual(
            kinds, ["actionlint", "unit-python", "minimum", "current", "release"]
        )
        self.assertLess(kinds.index("actionlint"), kinds.index("unit-python"))
        self.assertEqual(len({event["mount"] for event in self.events()}), 1)
        self.assertEqual(list(self.scratch.iterdir()), [])
        for lane in ("unit", "minimum", "current", "release"):
            self.assertTrue((self.root / (lane + ".done")).exists())

    def test_interrupt_preserves_status_and_waits_before_cleanup(self) -> None:
        self.env["VALIDATION_OVERLAP"] = "1"
        self.env["VALIDATION_INTERRUPT"] = "1"
        lanes = ("unit", "minimum", "current", "release")
        for interrupt in (signal.SIGINT, signal.SIGTERM):
            for failure in ("", "minimum"):
                with self.subTest(interrupt=interrupt, failure=failure):
                    for marker in self.root.glob("*.started"):
                        marker.unlink()
                    for marker in self.root.glob("*.done"):
                        marker.unlink()
                    (self.root / "interrupt.sent").unlink(missing_ok=True)
                    self.env["VALIDATION_FAIL"] = failure
                    with subprocess.Popen(
                        [str(BASH), str(self.runner), "all", "container"],
                        cwd=self.root,
                        env=self.env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    ) as process:
                        try:
                            deadline = time.monotonic() + 10
                            while not all(
                                (self.root / f"{lane}.started").exists()
                                for lane in lanes
                            ):
                                if (
                                    process.poll() is not None
                                    or time.monotonic() > deadline
                                ):
                                    self.fail("The four validation lanes did not start")
                                time.sleep(0.01)
                            process.send_signal(interrupt)
                            (self.root / "interrupt.sent").touch()
                            stdout, stderr = process.communicate(timeout=30)
                        finally:
                            if process.poll() is None:
                                process.kill()
                                process.communicate(timeout=10)
                    for lane in lanes:
                        self.assertTrue(
                            (self.root / f"{lane}.done").exists(), stdout + stderr
                        )
                    self.assertEqual(process.returncode, 128 + interrupt)
                    self.assertEqual(list(self.scratch.iterdir()), [])

    def test_failure_retains_every_lane_result_and_waits_for_all_workers(self) -> None:
        self.env["VALIDATION_OVERLAP"] = "1"
        for failure in ("minimum", "current", "both", "unit-python", "release"):
            with self.subTest(failure=failure):
                for marker in self.root.glob("*.started"):
                    marker.unlink()
                for marker in self.root.glob("*.done"):
                    marker.unlink()
                self.env["VALIDATION_FAIL"] = failure
                result = self.run_validation("all", "container")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                for lane in ("unit", "minimum", "current", "release"):
                    failed = failure == ("unit-python" if lane == "unit" else lane) or (
                        failure == "both" and lane in {"minimum", "current"}
                    )
                    stream = result.stderr if failed else result.stdout
                    outcome = "FAIL (exit 23)" if failed else "PASS"
                    self.assertIn(f"{lane}: {outcome}", stream)
                    self.assertTrue((self.root / (lane + ".done")).exists())
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_container_cache_provisioning_and_payload_failures(self) -> None:
        result = self.run_validation("all", "container")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        python_events = [
            event
            for event in self.events()
            if event["kind"] in {"unit-python", "minimum", "current"}
        ]
        self.assertEqual(len(python_events), 3)
        for event in python_events:
            args = event["args"]
            self.assertEqual(args[:2], ["run", "--rm"])
            self.assertIn("PIP_CACHE_DIR=/pip-cache", args)
            self.assertIn("PIP_COMPILE=0", args)
            self.assertIn("MYPY_CACHE_DIR=/dev/null", args)
            self.assertEqual(
                args[args.index("--mount") + 1],
                "type=volume,source=beestat-statistics-validation-pip,target=/pip-cache",
            )
            self.assertIn("python -m pip install", args[-1])
            self.assertEqual(
                args[-2], "true" if event["kind"] == "unit-python" else "false"
            )
            if event["kind"] in {"minimum", "current"}:
                self.assertIn("python -m pip check", args[-1])
                self.assertIn(
                    "python scripts/run_dependency_light_tests.py --home-assistant",
                    args[-1],
                )
        commands = {event["kind"]: event["args"] for event in python_events}
        apt_calls = ["apt update -qq", "apt install -y -qq --no-install-recommends git"]
        for lane, failure, payload_status, expected_status, expected_calls in (
            ("unit-python", "", 0, 0, [*apt_calls, "payload"]),
            ("minimum", "", 0, 0, ["payload"]),
            ("current", "", 0, 0, ["payload"]),
            ("unit-python", "update", 0, 37, apt_calls[:1]),
            ("unit-python", "install", 0, 41, apt_calls),
            ("minimum", "", 43, 43, ["payload"]),
        ):
            with self.subTest(
                lane=lane, failure=failure, payload_status=payload_status
            ):
                args = commands[lane]
                command = args[args.index("bash") :]
                command[0] = str(BASH)
                command[2] = r"""
apt-get() {
  printf 'apt %s\n' "$*" >&2
  if [[ "$PROVISION_FAIL" == update && "$1" == update ]]; then return 37; fi
  if [[ "$PROVISION_FAIL" == install && "$1" == install ]]; then return 41; fi
  return 0
}
""" + command[2]
                command[-1] = 'printf "payload\\n" >&2; exit "$PAYLOAD_STATUS"'
                probe = subprocess.run(
                    command,
                    env={
                        **os.environ,
                        "PROVISION_FAIL": failure,
                        "PAYLOAD_STATUS": str(payload_status),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(probe.returncode, expected_status, probe.stderr)
                self.assertEqual(probe.stderr.splitlines(), expected_calls)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_failed_snapshot_does_not_run_validation(self) -> None:
        result = self.run_validation("release", "container", str(self.root / "missing"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_native_actionlint_provisions_shellcheck_and_cleans_failures(self) -> None:
        (self.bin / "shellcheck").unlink()
        for failure, expected in (
            ("python", ["python"]),
            ("pip", ["python", "pip"]),
            ("go", ["python", "pip", "go"]),
            ("actionlint", ["python", "pip", "go", "actionlint", "shellcheck"]),
            ("", ["python", "pip", "go", "actionlint", "shellcheck"]),
        ):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                self.env["VALIDATION_FAIL"] = failure
                result = self.run_validation("unit", "native")
                self.assertEqual(
                    result.returncode,
                    1 if failure else 0,
                    result.stdout + result.stderr,
                )
                events = self.events()
                kinds = [event["kind"] for event in events]
                self.assertEqual(kinds if failure else kinds[:5], expected)
                self.assertEqual(list(self.scratch.iterdir()), [])
                if not failure or failure == "actionlint":
                    self.assertEqual(events[3]["cwd"], str(self.repo))
                    self.assertEqual(
                        Path(str(events[4]["path"])).parent,
                        Path(str(events[3]["path"])).parent,
                    )
                if not failure:
                    self.assertEqual(sum("environment" in event for event in events), 2)

    def test_native_lanes_have_distinct_temporary_environments(self) -> None:
        lanes = ("minimum", "current")
        for lane in lanes:
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
        for lane, environment in zip(lanes, environments, strict=True):
            with self.subTest(lane=lane):
                calls = [
                    event["args"]
                    for event in self.events()
                    if Path(str(event["path"])) == Path(str(environment)) / "bin/python"
                ]
                harness = next(
                    index
                    for index, call in enumerate(calls)
                    if call[:3] == ["-m", "pip", "install"]
                    and any(
                        arg.startswith("pytest-homeassistant-custom-component==")
                        for arg in call
                    )
                )
                requirements = calls.index(
                    [
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        "-r",
                        "requirements-ha-test.txt"
                        if lane == "minimum"
                        else "requirements-ha-current.txt",
                    ]
                )
                dependency_check = calls.index(["-m", "pip", "check"])
                tests = calls.index(
                    ["scripts/run_dependency_light_tests.py", "--home-assistant"]
                )
                self.assertLess(harness, requirements)
                self.assertLess(requirements, dependency_check)
                self.assertTrue(
                    all(
                        index < dependency_check
                        for index, call in enumerate(calls)
                        if call[:3] == ["-m", "pip", "install"]
                    )
                )
                self.assertLess(dependency_check, tests)

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
    switch ($args[-1]) {
        '--show-toplevel' {
            Split-Path (Split-Path $env:VALIDATION_SCRIPT -Parent) -Parent
        }
        '--git-dir' {
            if ($env:VALIDATION_FAIL -eq "git") {
                $global:LASTEXITCODE = 23
                return
            }
            "C:" + "\source with spaces\.git"
        }
        default { throw "Unexpected Git query: $args" }
    }
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

"""Validate the bounded same-month Home Assistant patch-forward test lane."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

HARNESS_DISTRIBUTION = "pytest-homeassistant-custom-component"
CORE_DISTRIBUTION = "homeassistant"
VERSION_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d+)$")
CORE_REQUIREMENT_RE = re.compile(
    r"^\s*homeassistant\s*==\s*(\d{4}\.\d{1,2}\.\d+)\s*(?:;.*)?$",
    re.IGNORECASE,
)


class CompatibilityError(ValueError):
    """Raised when the environment is outside the bounded patch contract."""


def exact_core_pin(path: Path) -> str:
    """Read exactly one stable Home Assistant pin from a requirements file."""
    pins = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        content = line.split("#", 1)[0].strip()
        if not content:
            continue
        match = CORE_REQUIREMENT_RE.fullmatch(content)
        if match is not None:
            pins.append(match.group(1))
    if len(pins) != 1:
        raise CompatibilityError(
            f"{path.name} must contain exactly one Home Assistant pin"
        )
    stable_version(pins[0])
    return pins[0]


def stable_version(value: str) -> tuple[int, int, int]:
    """Return a validated stable Home Assistant CalVer tuple."""
    match = VERSION_RE.fullmatch(value)
    if match is None:
        raise CompatibilityError(
            f"Home Assistant version is not a stable exact pin: {value}"
        )
    version = tuple(int(part) for part in match.groups())
    if not 1 <= version[1] <= 12:
        raise CompatibilityError(f"Home Assistant month is invalid: {value}")
    return version


def validate_patch_window(minimum: str, current: str) -> None:
    """Require a forward patch within one stable Home Assistant month."""
    minimum_version = stable_version(minimum)
    current_version = stable_version(current)
    if minimum_version[:2] != current_version[:2] or current_version <= minimum_version:
        raise CompatibilityError(
            "current Core must be a later patch in the minimum Core year/month"
        )


def harness_core_pin(requirements: list[str] | None) -> str:
    """Read the harness's one exact Home Assistant requirement from metadata."""
    matches = []
    for requirement in requirements or []:
        match = CORE_REQUIREMENT_RE.fullmatch(requirement)
        if match is not None:
            matches.append(match.group(1))
    if len(matches) != 1:
        raise CompatibilityError(
            "the installed harness must declare exactly one exact Home Assistant requirement"
        )
    stable_version(matches[0])
    return matches[0]


def validate_pip_check(
    returncode: int,
    output: str,
    *,
    harness_version: str,
    harness_core: str,
    current_core: str,
) -> str:
    """Accept closure or the single metadata-proven harness/Core mismatch."""
    if returncode == 0:
        return "dependency-closed"
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    expected = re.compile(
        rf"^{re.escape(HARNESS_DISTRIBUTION)}\s+{re.escape(harness_version)}\s+"
        rf"has requirement homeassistant=={re.escape(harness_core)}, but you have "
        rf"homeassistant {re.escape(current_core)}\.$",
        re.IGNORECASE,
    )
    if len(lines) == 1 and expected.fullmatch(lines[0]):
        return "single-known-harness-core-mismatch"
    raise CompatibilityError("pip check reported an unexpected dependency conflict")


def run(minimum_path: Path, current_path: Path) -> str:
    """Validate installed metadata and the actual environment."""
    minimum_core = exact_core_pin(minimum_path)
    current_core = exact_core_pin(current_path)
    validate_patch_window(minimum_core, current_core)

    installed_core = metadata.version(CORE_DISTRIBUTION)
    if installed_core != current_core:
        raise CompatibilityError(
            f"installed Home Assistant {installed_core} does not match {current_path.name}"
        )

    harness = metadata.distribution(HARNESS_DISTRIBUTION)
    harness_version = harness.version
    required_core = harness_core_pin(harness.requires)
    if required_core != minimum_core:
        raise CompatibilityError(
            "the harness Home Assistant pin does not match the supported minimum lane"
        )

    result = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        check=False,
        capture_output=True,
        text=True,
    )
    return validate_pip_check(
        result.returncode,
        f"{result.stdout}\n{result.stderr}",
        harness_version=harness_version,
        harness_core=required_core,
        current_core=current_core,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimum", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.minimum, args.current)
    except (
        CompatibilityError,
        FileNotFoundError,
        metadata.PackageNotFoundError,
    ) as exc:
        print(f"Home Assistant patch compatibility failed: {exc}", file=sys.stderr)
        return 1
    print(f"Home Assistant patch compatibility passed: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

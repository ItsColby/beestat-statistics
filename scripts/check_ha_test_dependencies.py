"""Verify the exact Home Assistant test lane's dependency closure."""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys

CORE_VERSION = "2026.8.1"
HARNESS_CORE_VERSION = "2026.8.0"
HARNESS_VERSION = "0.13.354"
EXPECTED_PATCH_SKEW = (
    f"pytest-homeassistant-custom-component {HARNESS_VERSION} has requirement "
    f"homeassistant=={HARNESS_CORE_VERSION}, but you have homeassistant "
    f"{CORE_VERSION}."
)


def main() -> int:
    """Fail on every dependency conflict except the verified harness patch skew."""

    installed = {
        "homeassistant": importlib.metadata.version("homeassistant"),
        "pytest-homeassistant-custom-component": importlib.metadata.version(
            "pytest-homeassistant-custom-component"
        ),
    }
    expected = {
        "homeassistant": CORE_VERSION,
        "pytest-homeassistant-custom-component": HARNESS_VERSION,
    }
    if installed != expected:
        print(
            f"Unexpected Home Assistant test lane versions: {installed!r}",
            file=sys.stderr,
        )
        return 1

    result = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        check=False,
        capture_output=True,
        text=True,
    )
    conflicts = tuple(
        line.strip()
        for line in (*result.stdout.splitlines(), *result.stderr.splitlines())
        if line.strip()
    )
    if result.returncode == 0 and not conflicts:
        return 0
    if conflicts == (EXPECTED_PATCH_SKEW,):
        print(
            "Dependency closure verified with only the known upstream harness "
            "patch-version metadata skew."
        )
        return 0

    print("Unexpected Home Assistant test dependency conflicts:", file=sys.stderr)
    for conflict in conflicts:
        print(conflict, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

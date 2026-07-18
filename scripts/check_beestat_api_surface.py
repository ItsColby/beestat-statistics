"""Check the upstream Beestat API files this integration depends on."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO = "beestat/app"
BRANCH = "master"
GITHUB_API_ROOT = f"https://api.github.com/repos/{REPO}"
RAW_ROOT = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
DEFAULT_SNAPSHOT = (
    Path(__file__).resolve().parents[1] / "docs" / "beestat-api-surface.json"
)

WATCH_PATHS = (
    "api/cora/api.php",
    "api/cora/crud.php",
    "api/index.php",
    "api/runtime.php",
    "api/runtime_sensor.php",
    "api/runtime_thermostat.php",
    "api/runtime_thermostat_summary.php",
    "api/sensor.php",
    "api/thermostat.php",
)

INTEGRATION_DECISIONS = (
    {
        "surface": "runtime.sync, thermostat.sync, sensor.sync",
        "decision": "used",
        "reason": (
            "The integration needs Beestat cloud/history data refreshed before "
            "reading native entities or importing Recorder statistics."
        ),
    },
    {
        "surface": "thermostat.read_id, sensor.read_id",
        "decision": "used",
        "reason": (
            "These are the narrow metadata reads that support Home Assistant "
            "device matching, status sensors, and options-flow discovery."
        ),
    },
    {
        "surface": "runtime_thermostat.read, runtime_sensor.read",
        "decision": "used",
        "reason": (
            "Point-history reads are windowed and feed daily Home Assistant "
            "external statistics."
        ),
    },
    {
        "surface": "runtime_thermostat_summary.read_id with date attributes",
        "decision": "used",
        "reason": (
            "Summary rows are windowed for normal imports once Home Assistant "
            "Recorder has a prior cumulative seed; the importer keeps a "
            "full-baseline fallback for new installs, missing seeds, and "
            "rebuilds."
        ),
    },
    {
        "surface": "thermostat.get_metrics, thermostat.generate_profile",
        "decision": "not_used",
        "reason": (
            "These Beestat comparison/profile features are not local HA state, "
            "are cached app analysis paths, and would broaden the integration "
            "beyond history import/status enrichment."
        ),
    },
    {
        "surface": "thermostat.dismiss_alert",
        "decision": "used",
        "reason": (
            "When Home Assistant records a filter change, the integration can "
            "dismiss matching active Beestat filter alerts so Beestat's alert "
            "state follows the local acknowledgement."
        ),
    },
    {
        "surface": "thermostat.restore_alert, thermostat.update",
        "decision": "not_used",
        "reason": (
            "Restoring alerts and broad thermostat updates would make this "
            "statistics integration a general Beestat control surface. Direct "
            "filter metadata updates are also avoided because Beestat sync owns "
            "that field and can overwrite local writes."
        ),
    },
)


def main(argv: list[str] | None = None) -> int:
    """Run the API surface check."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help="Path to the checked-in API surface snapshot.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Refresh the snapshot instead of checking it.",
    )
    args = parser.parse_args(argv)

    try:
        current = fetch_surface()
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as err:
        print(f"Failed to fetch Beestat API surface: {err}", file=sys.stderr)
        return 2

    if args.update:
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Updated {args.snapshot}")
        return 0

    if not args.snapshot.is_file():
        print(
            f"Missing snapshot {args.snapshot}. Run with --update first.",
            file=sys.stderr,
        )
        return 2

    expected = json.loads(args.snapshot.read_text(encoding="utf-8"))
    differences = diff_surface(expected, current)
    if differences:
        print("Beestat API surface drift detected:", file=sys.stderr)
        for item in differences:
            print(f"- {item}", file=sys.stderr)
        print(
            "Review upstream changes, update integration decisions if needed, "
            "then run scripts/check_beestat_api_surface.py --update.",
            file=sys.stderr,
        )
        return 1

    commit = current["snapshot"]["commit_sha"]
    print(f"Beestat API surface matches watched snapshot at {commit}.")
    return 0


def fetch_surface() -> dict[str, Any]:
    """Fetch the current upstream surface from the official Beestat app repo."""

    commit = _request_json(f"{GITHUB_API_ROOT}/commits/{BRANCH}")
    tree = _request_json(f"{GITHUB_API_ROOT}/git/trees/{BRANCH}?recursive=1")["tree"]
    blob_sha_by_path = {
        item["path"]: item["sha"]
        for item in tree
        if item.get("type") == "blob" and isinstance(item.get("path"), str)
    }

    watched_files: dict[str, dict[str, Any]] = {}
    for path in WATCH_PATHS:
        text = _request_text(f"{RAW_ROOT}/{path}")
        watched_files[path] = {
            "blob_sha": blob_sha_by_path.get(path),
            "exposed": extract_exposed_methods(text),
            "checks": behavior_checks(path, text),
        }

    return {
        "schema_version": 1,
        "source": {
            "repo": REPO,
            "branch": BRANCH,
            "app_repo_url": f"https://github.com/{REPO}",
            "api_docs_url": "https://api.beestat.io/doc",
        },
        "snapshot": {
            "commit_sha": commit["sha"],
            "commit_date": commit["commit"]["committer"]["date"],
            "commit_message": commit["commit"]["message"],
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
        "watch_paths": list(WATCH_PATHS),
        "watched_files": watched_files,
        "integration_decisions": list(INTEGRATION_DECISIONS),
    }


def diff_surface(expected: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Return human-readable differences in watched API files."""

    differences: list[str] = []
    expected_files = expected.get("watched_files", {})
    current_files = current.get("watched_files", {})
    for path in sorted(set(expected_files) | set(current_files)):
        if path not in expected_files:
            differences.append(f"{path} is newly watched upstream")
            continue
        if path not in current_files:
            differences.append(f"{path} is missing upstream")
            continue
        expected_file = comparable_file(expected_files[path])
        current_file = comparable_file(current_files[path])
        if expected_file != current_file:
            differences.append(
                f"{path} changed from {expected_file} to {current_file}"
            )
    return differences


def comparable_file(value: dict[str, Any]) -> dict[str, Any]:
    """Return only fields that should fail the drift check."""

    return {
        "blob_sha": value.get("blob_sha"),
        "exposed": value.get("exposed"),
        "checks": value.get("checks"),
    }


def extract_exposed_methods(text: str) -> dict[str, list[str]] | None:
    """Extract public/private method names from a PHP $exposed declaration."""

    match = re.search(r"public\s+static\s+\$exposed\s*=\s*\[(.*?)\];", text, re.S)
    if match is None:
        return None

    exposed: dict[str, list[str]] = {}
    for scope in ("private", "public"):
        scope_match = re.search(rf"'{scope}'\s*=>\s*\[(.*?)\]", match.group(1), re.S)
        if scope_match is None:
            exposed[scope] = []
            continue
        exposed[scope] = re.findall(r"'([^']+)'", scope_match.group(1))
    return exposed


def behavior_checks(path: str, text: str) -> dict[str, bool]:
    """Return behavior checks that matter beyond exposed method names."""

    checks: dict[str, bool] = {}
    if path == "api/cora/crud.php":
        checks["read_id_forwards_attributes"] = (
            "$rows = $this->read($attributes, $columns);" in text
        )
    if path == "api/runtime_thermostat_summary.php":
        checks["date_range_adjusts_lower_bound"] = (
            "$attributes['date']['value'][0]" in text
        )
        checks["date_range_adjusts_upper_bound"] = (
            "$attributes['date']['value'][1]" in text
        )
        checks["summary_divides_temperature_tenths"] = (
            "$runtime_thermostat_summary['avg_outdoor_temperature'] /= 10;" in text
        )
    if path == "api/runtime_thermostat.php":
        checks["runtime_window_rejects_over_31_days"] = "2678000" in text
        checks["runtime_divides_temperature_tenths"] = (
            "$runtime_thermostat[$key] /= 10;" in text
            and "'outdoor_temperature'" in text
            and "'setpoint_heat'" in text
        )
    if path == "api/runtime_sensor.php":
        checks["sensor_window_rejects_over_31_days"] = "2678000" in text
        checks["sensor_divides_temperature_tenths"] = (
            "$runtime_sensor['temperature'] /= 10;" in text
        )
        checks["sensor_normalizes_air_quality"] = (
            "$runtime_sensor['air_quality'] = round(" in text
        )
    return checks


def _request_json(url: str) -> Any:
    return json.loads(_request_text(url))


def _request_text(url: str) -> str:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "beestat-statistics-api-surface-check",
    }
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

"""Check the upstream Beestat API files this integration depends on."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPO = "beestat/app"
BRANCH = "master"
GITHUB_API_ROOT = f"https://api.github.com/repos/{REPO}"
RAW_ROOT = f"https://raw.githubusercontent.com/{REPO}"
ALLOWED_REQUEST_HOSTS = frozenset({"api.github.com", "raw.githubusercontent.com"})
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SHA_RE = re.compile(r"[0-9a-f]{40}")
DEFAULT_SNAPSHOT = (
    Path(__file__).resolve().parents[1] / "docs" / "beestat-api-surface.json"
)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects so an optional GitHub token stays on approved hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Refuse every redirect instead of forwarding request headers."""

        return


_URL_OPENER = build_opener(_NoRedirectHandler)

WATCH_PATHS = (
    "api/cora/api.php",
    "api/cora/crud.php",
    "api/ecobee_thermostat.php",
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
        "surface": "ecobee_thermostat.read_id",
        "decision": "used",
        "reason": (
            "The integration projects a strict privacy allowlist of cached "
            "Ecobee settings for local configuration readback and optional "
            "diagnostic entities; raw account, location, billing, device, and "
            "access-control data is never retained."
        ),
    },
    {
        "surface": "runtime_thermostat.read, runtime_sensor.read",
        "decision": "used",
        "reason": (
            "Point-history reads are windowed and feed daily Home Assistant "
            "external statistics; thermostat reads also reconcile saved filter "
            "clicks to the nearest five-minute runtime boundary."
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

    expected = None
    if not args.update:
        try:
            expected = json.loads(args.snapshot.read_text(encoding="utf-8"))
            validate_surface(expected)
        except OSError, ValueError, TypeError, KeyError:
            print("Missing or invalid API surface snapshot.", file=sys.stderr)
            return 2

    try:
        current = fetch_surface()
        validate_surface(current)
    except (OSError, HTTPException, ValueError, TypeError, KeyError) as err:
        print(
            f"Failed to fetch a complete Beestat API surface ({type(err).__name__}).",
            file=sys.stderr,
        )
        return 2

    if args.update:
        try:
            _write_snapshot(args.snapshot, current)
        except OSError:
            print("Unable to write the API surface snapshot.", file=sys.stderr)
            return 2
        print(f"Updated {args.snapshot}")
        return 0

    assert expected is not None
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


def _write_snapshot(path: Path, surface: dict[str, Any]) -> None:
    """Replace the snapshot only after the complete new file has been written."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(surface, indent=2, sort_keys=True) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fetch_surface() -> dict[str, Any]:
    """Fetch the current upstream surface from the official Beestat app repo."""

    commit = _request_json(f"{GITHUB_API_ROOT}/commits/{BRANCH}")
    commit_sha = _checked_sha(commit["sha"])
    tree_sha = _checked_sha(commit["commit"]["tree"]["sha"])
    tree_result = _request_json(f"{GITHUB_API_ROOT}/git/trees/{tree_sha}?recursive=1")
    if tree_result.get("truncated") is not False or not isinstance(
        tree_result.get("tree"), list
    ):
        raise ValueError("Incomplete upstream tree")
    tree = tree_result["tree"]
    blob_sha_by_path = {
        item["path"]: _checked_sha(item["sha"])
        for item in tree
        if isinstance(item, dict)
        and item.get("type") == "blob"
        and isinstance(item.get("path"), str)
    }

    watched_files: dict[str, dict[str, Any]] = {}
    for path in WATCH_PATHS:
        blob_sha = blob_sha_by_path[path]
        text = _request_text(f"{RAW_ROOT}/{commit_sha}/{path}")
        raw = text.encode("utf-8")
        actual_sha = hashlib.sha1(
            f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False
        ).hexdigest()
        if actual_sha != blob_sha:
            raise ValueError("Upstream blob content does not match its tree")
        watched_files[path] = {
            "blob_sha": blob_sha,
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
            "commit_sha": commit_sha,
            "commit_date": commit["commit"]["committer"]["date"],
            "commit_message": commit["commit"]["message"],
            "captured_at": datetime.now(UTC).isoformat(),
        },
        "watch_paths": list(WATCH_PATHS),
        "watched_files": watched_files,
        "integration_decisions": list(INTEGRATION_DECISIONS),
    }


def _checked_sha(value: Any) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise ValueError("Invalid upstream object ID")
    return value


def validate_surface(surface: Any) -> None:
    """Reject incomplete snapshots rather than comparing an empty file set."""

    if not isinstance(surface, dict) or surface.get("schema_version") != 1:
        raise ValueError("Invalid surface schema")
    _checked_sha(surface["snapshot"]["commit_sha"])
    if not isinstance(surface.get("source"), dict) or not isinstance(
        surface.get("integration_decisions"), list
    ):
        raise TypeError("Missing surface metadata")
    paths = surface.get("watch_paths")
    files = surface.get("watched_files")
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(path, str) for path in paths)
        or len(set(paths)) != len(paths)
        or not isinstance(files, dict)
        or set(paths) != set(files)
    ):
        raise ValueError("Invalid watched file inventory")
    for value in files.values():
        _validate_file(value)


def _validate_file(value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError("Invalid watched file")
    _checked_sha(value["blob_sha"])
    checks = value.get("checks")
    if not isinstance(checks, dict) or not all(
        isinstance(key, str) and isinstance(check, bool)
        for key, check in checks.items()
    ):
        raise ValueError("Invalid behavior checks")
    exposed = value["exposed"]
    if exposed is not None and (
        not isinstance(exposed, dict)
        or set(exposed) != {"public", "private"}
        or not all(
            isinstance(methods, list)
            and all(isinstance(method, str) for method in methods)
            for methods in exposed.values()
        )
    ):
        raise ValueError("Invalid exposed methods")


def diff_surface(expected: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Return human-readable differences in watched API files."""

    differences = [
        f"{key} changed"
        for key in ("schema_version", "source", "watch_paths", "integration_decisions")
        if expected.get(key) != current.get(key)
    ]
    expected_files = expected.get("watched_files", {})
    current_files = current.get("watched_files", {})
    for path in sorted(set(expected_files) | set(current_files)):
        if path not in expected_files:
            differences.append("A new watched file was added")
            continue
        if path not in current_files:
            differences.append("A watched file is missing upstream")
            continue
        expected_file = comparable_file(expected_files[path])
        current_file = comparable_file(current_files[path])
        if expected_file != current_file:
            label = path if path in WATCH_PATHS else "Unknown watched file"
            differences.append(f"{label} changed")
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

    match = re.search(r"public\s+static\s+\$exposed\s*=\s*\[(.*?)\];", text, re.DOTALL)
    if match is None:
        return None

    exposed: dict[str, list[str]] = {}
    for scope in ("private", "public"):
        scope_match = re.search(
            rf"'{scope}'\s*=>\s*\[(.*?)\]", match.group(1), re.DOTALL
        )
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
    value = json.loads(_request_text(url))
    if not isinstance(value, dict):
        raise TypeError("Expected an upstream JSON object")
    return value


def _validated_request_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_REQUEST_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
        or any(ord(char) < 32 or ord(char) == 127 for char in url)
    ):
        raise ValueError("Unsupported API surface URL")
    return url


def _request_text(url: str) -> str:
    validated_url = _validated_request_url(url)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "beestat-statistics-api-surface-check",
    }
    if urlsplit(validated_url).hostname == "api.github.com" and (
        token := os.environ.get("GITHUB_TOKEN")
    ):
        headers["Authorization"] = f"Bearer {token}"
    request = Request(validated_url, headers=headers)  # noqa: S310 - validated HTTPS host
    with _URL_OPENER.open(request, timeout=30) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Upstream response exceeds the size limit")
        return raw.decode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

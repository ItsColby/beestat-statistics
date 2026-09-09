"""Tests for the Beestat upstream API surface checker."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_beestat_api_surface.py"


def _load_checker_module():
    spec = importlib.util.spec_from_file_location("check_beestat_api_surface", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load API surface checker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApiSurfaceCheckerTest(unittest.TestCase):
    """Validate parser and diff behavior without network access."""

    def setUp(self) -> None:
        self.checker = _load_checker_module()

    def test_extract_exposed_methods_handles_multiline_php_arrays(self) -> None:
        self.assertEqual(
            self.checker.extract_exposed_methods(
                """
                class thermostat extends cora\\crud {
                  public static $exposed = [
                    'private' => [
                      'read_id',
                      'sync',
                      'get_metrics',
                    ],
                    'public' => []
                  ];
                }
                """
            ),
            {
                "private": ["read_id", "sync", "get_metrics"],
                "public": [],
            },
        )

    def test_behavior_checks_capture_summary_date_range_support(self) -> None:
        checks = self.checker.behavior_checks(
            "api/runtime_thermostat_summary.php",
            """
            $attributes['date']['value'][0] = date('Y-m-d', strtotime('x'));
            $attributes['date']['value'][1] = date('Y-m-d', strtotime('x'));
            $runtime_thermostat_summary['avg_outdoor_temperature'] /= 10;
            """,
        )

        self.assertEqual(
            checks,
            {
                "date_range_adjusts_lower_bound": True,
                "date_range_adjusts_upper_bound": True,
                "summary_divides_temperature_tenths": True,
            },
        )

    def test_diff_ignores_commit_metadata_but_flags_watched_file_changes(self) -> None:
        expected = {
            "snapshot": {"commit_sha": "old"},
            "watched_files": {
                "api/runtime_sensor.php": {
                    "blob_sha": "abc",
                    "exposed": {"private": ["read"], "public": []},
                    "checks": {"sensor_window_rejects_over_31_days": True},
                }
            },
        }
        current_same = {
            "snapshot": {"commit_sha": "new"},
            "watched_files": {
                "api/runtime_sensor.php": {
                    "blob_sha": "abc",
                    "exposed": {"private": ["read"], "public": []},
                    "checks": {"sensor_window_rejects_over_31_days": True},
                }
            },
        }
        current_changed = {
            "snapshot": {"commit_sha": "new"},
            "watched_files": {
                "api/runtime_sensor.php": {
                    "blob_sha": "def",
                    "exposed": {"private": ["read"], "public": []},
                    "checks": {"sensor_window_rejects_over_31_days": True},
                }
            },
        }

        self.assertEqual(self.checker.diff_surface(expected, current_same), [])
        self.assertEqual(len(self.checker.diff_surface(expected, current_changed)), 1)

    def test_request_url_allows_only_expected_https_hosts(self) -> None:
        credentialed_url = f"https://user{chr(64)}api.github.com/repos/beestat/app"
        for url in (
            "https://api.github.com/repos/beestat/app",
            "https://raw.githubusercontent.com/beestat/app/master/api/index.php",
        ):
            with self.subTest(url=url):
                self.assertEqual(url, self.checker._validated_request_url(url))

        for url in (
            "http://api.github.com/repos/beestat/app",
            "https://example.com/beestat/app",
            credentialed_url,
            "https://api.github.com:444/repos/beestat/app",
            "file:///tmp/beestat-api.json",
        ):
            with (
                self.subTest(url=url),
                self.assertRaisesRegex(ValueError, "Unsupported API surface URL"),
            ):
                self.checker._validated_request_url(url)

    def test_surface_checker_refuses_redirects(self) -> None:
        handler = self.checker._NoRedirectHandler()

        self.assertIsNone(
            handler.redirect_request(
                object(),
                object(),
                302,
                "Found",
                {},
                "https://example.com/credential-target",
            )
        )

    def _upstream_fixture(self):
        content = "<?php\n"
        raw = content.encode()
        blob_sha = hashlib.sha1(
            f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False
        ).hexdigest()
        commit = {
            "sha": "a" * 40,
            "commit": {
                "tree": {"sha": "b" * 40},
                "committer": {"date": "2026-01-01T00:00:00Z"},
                "message": "Example change",
            },
        }
        tree = {
            "truncated": False,
            "tree": [
                {"type": "blob", "path": path, "sha": blob_sha}
                for path in self.checker.WATCH_PATHS
            ],
        }
        return commit, tree, content

    def test_fetch_pins_tree_and_content_to_one_commit(self) -> None:
        commit, tree, content = self._upstream_fixture()
        with (
            patch.object(
                self.checker, "_request_json", side_effect=[commit, tree]
            ) as req,
            patch.object(self.checker, "_request_text", return_value=content) as raw,
        ):
            surface = self.checker.fetch_surface()
        self.checker.validate_surface(surface)
        self.assertEqual(
            req.call_args.args[0],
            f"{self.checker.GITHUB_API_ROOT}/git/trees/{'b' * 40}?recursive=1",
        )
        self.assertEqual(len(raw.call_args_list), len(self.checker.WATCH_PATHS))
        for call in raw.call_args_list:
            self.assertIn(f"/{'a' * 40}/api/", call.args[0])

    def test_fetch_rejects_truncated_missing_or_mismatched_blobs(self) -> None:
        for scenario in ("truncated", "missing", "mismatch"):
            with self.subTest(scenario=scenario):
                commit, tree, content = self._upstream_fixture()
                if scenario == "truncated":
                    tree["truncated"] = True
                elif scenario == "missing":
                    tree["tree"] = []
                else:
                    content = "unexpected"
                with (
                    patch.object(
                        self.checker, "_request_json", side_effect=[commit, tree]
                    ),
                    patch.object(self.checker, "_request_text", return_value=content),
                    self.assertRaises((ValueError, KeyError)),
                ):
                    self.checker.fetch_surface()

    def test_snapshot_inventory_and_schema_fail_closed(self) -> None:
        baseline = json.loads(self.checker.DEFAULT_SNAPSHOT.read_text(encoding="utf-8"))
        self.checker.validate_surface(baseline)
        invalid = [
            [],
            {},
            {**baseline, "watched_files": {}},
            {**baseline, "watch_paths": []},
        ]
        for snapshot in invalid:
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                self.checker.validate_surface(snapshot)

    def test_invalid_local_snapshot_does_not_make_network_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json"
            snapshot.write_text("not json", encoding="utf-8")
            with (
                patch.object(self.checker, "fetch_surface") as fetch,
                redirect_stderr(io.StringIO()) as output,
            ):
                self.assertEqual(2, self.checker.main(["--snapshot", str(snapshot)]))
            fetch.assert_not_called()
            self.assertIn("invalid", output.getvalue())

    def test_network_failure_does_not_echo_exception_payload(self) -> None:
        with (
            patch.object(
                self.checker, "fetch_surface", side_effect=URLError("private")
            ),
            redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(2, self.checker.main([]))
        self.assertNotIn("private", output.getvalue())
        self.assertIn("URLError", output.getvalue())

    def test_atomic_write_preserves_original_if_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json"
            snapshot.write_text("original", encoding="utf-8")
            with (
                patch.object(Path, "replace", side_effect=OSError),
                self.assertRaises(OSError),
            ):
                self.checker._write_snapshot(snapshot, {})
            self.assertEqual("original", snapshot.read_text(encoding="utf-8"))
            self.assertEqual([snapshot], list(Path(directory).iterdir()))

    def test_request_limits_size_and_token_destination(self) -> None:
        for host in ("api.github.com", "raw.githubusercontent.com"):
            with (
                self.subTest(host=host),
                patch.dict(self.checker.os.environ, {"GITHUB_TOKEN": "example-token"}),
                patch.object(
                    self.checker._URL_OPENER, "open", return_value=io.BytesIO(b"ok")
                ) as opener,
            ):
                self.assertEqual(
                    "ok", self.checker._request_text(f"https://{host}/example")
                )
                request = opener.call_args.args[0]
                self.assertEqual(
                    host == "api.github.com", request.has_header("Authorization")
                )
                self.assertEqual(30, opener.call_args.kwargs["timeout"])
        with (
            patch.object(self.checker, "MAX_RESPONSE_BYTES", 2),
            patch.object(
                self.checker._URL_OPENER, "open", return_value=io.BytesIO(b"long")
            ),
            self.assertRaisesRegex(ValueError, "size limit"),
        ):
            self.checker._request_text("https://api.github.com/example")

    def test_diff_reports_policy_changes_without_upstream_values(self) -> None:
        expected = {"integration_decisions": [{"reason": "before"}]}
        current = {"integration_decisions": [{"reason": "private"}]}
        self.assertEqual(
            ["integration_decisions changed"],
            self.checker.diff_surface(expected, current),
        )

    def test_cli_reports_match_drift_and_update(self) -> None:
        baseline = json.loads(self.checker.DEFAULT_SNAPSHOT.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json"
            self.checker._write_snapshot(snapshot, baseline)
            with (
                patch.object(self.checker, "fetch_surface", return_value=baseline),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(0, self.checker.main(["--snapshot", str(snapshot)]))
                baseline["watched_files"]["api/index.php"]["blob_sha"] = "c" * 40
                self.assertEqual(1, self.checker.main(["--snapshot", str(snapshot)]))
                self.assertEqual(
                    0, self.checker.main(["--snapshot", str(snapshot), "--update"])
                )
                self.assertEqual(
                    baseline, json.loads(snapshot.read_text(encoding="utf-8"))
                )


if __name__ == "__main__":
    unittest.main()

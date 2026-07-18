"""Tests for the Beestat upstream API surface checker."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


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


if __name__ == "__main__":
    unittest.main()

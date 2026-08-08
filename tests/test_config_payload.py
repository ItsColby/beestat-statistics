"""Tests for config-entry payload helpers."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_config_payload_test"


def _load_module(name: str):
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{name}", ROOT / f"{name}.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_module("const")
config_payload = _load_module("config_payload")


class ConfigPayloadTest(unittest.TestCase):
    """Validate dependency-free config-entry payload shaping."""

    def test_split_entry_payload_preserves_mapping_overrides(self) -> None:
        data, options = config_payload.split_entry_payload(
            {
                "api_key": "key",
                "api_base": "https://example.test/",
                "point_lookback_days": 45,
                "scan_interval_seconds": 30,
                "thermostats": [{"id": 1, "climate_entity_id": "climate.main"}],
                "sensors": [{"id": 2, "temperature_entity_id": "sensor.room"}],
            }
        )

        self.assertEqual(
            data,
            {
                "api_key": "key",
                "api_base": "https://example.test/",
                "thermostats": [{"id": 1, "climate_entity_id": "climate.main"}],
                "sensors": [{"id": 2, "temperature_entity_id": "sensor.room"}],
            },
        )
        self.assertEqual(options["point_lookback_days"], 45)
        self.assertEqual(options["scan_interval_seconds"], 300)

    def test_effective_thermostat_override_prefers_current_options(self) -> None:
        self.assertEqual(
            config_payload.effective_thermostat_override(
                {"thermostats": [{"id": 1, "filter_changed_at": "old"}]},
                {
                    "thermostats": [
                        {"id": "invalid", "filter_changed_at": "ignore"},
                        {"id": 1, "filter_changed_at": "new"},
                    ]
                },
                1,
            ),
            {"id": 1, "filter_changed_at": "new"},
        )

    def test_split_entry_payload_normalizes_filter_changed_date(self) -> None:
        data, _options = config_payload.split_entry_payload(
            {
                "api_key": "key",
                "thermostats": [
                    {
                        "id": 1,
                        "filter_changed_date": date(2026, 7, 5),
                    }
                ],
            }
        )

        self.assertEqual(
            data["thermostats"],
            [{"id": 1, "filter_changed_date": "2026-07-05"}],
        )

    def test_connection_data_keeps_existing_key_when_blank(self) -> None:
        self.assertEqual(
            config_payload.connection_data_from_user_input(
                {
                    "api_key": "old-key",
                    "api_base": "https://old.example/",
                },
                {
                    "api_key": "",
                    "api_base": "https://new.example/",
                },
            ),
            {
                "api_key": "old-key",
                "api_base": "https://new.example/",
            },
        )

    def test_connection_data_normalizes_copy_paste_whitespace(self) -> None:
        data, _options = config_payload.split_entry_payload(
            {
                "api_key": "  pasted-key \n",
                "api_base": " https://example.test/ ",
            }
        )

        self.assertEqual(
            data,
            {
                "api_key": "pasted-key",
                "api_base": "https://example.test/",
            },
        )

    def test_connection_payloads_reject_insecure_api_base(self) -> None:
        with self.assertRaises(ValueError):
            config_payload.split_entry_payload(
                {
                    "api_key": "key",
                    "api_base": "http://api.example.test/",
                }
            )
        with self.assertRaises(ValueError):
            config_payload.connection_data_from_user_input(
                {
                    "api_key": "old-key",
                    "api_base": "https://api.example.test/",
                },
                {
                    "api_key": "replacement-key",
                    "api_base": "https://user@example.test/",
                },
            )
        self.assertEqual(
            config_payload.connection_data_from_user_input(
                {
                    "api_key": "old-key",
                    "api_base": "https://old.example/",
                },
                {
                    "api_key": " replacement-key ",
                    "api_base": " https://new.example/ ",
                },
            ),
            {
                "api_key": "replacement-key",
                "api_base": "https://new.example/",
            },
        )

    def test_options_from_user_input_normalizes_selector_floats(self) -> None:
        self.assertEqual(
            config_payload.options_from_user_input(
                {
                    "point_lookback_days": 30.0,
                    "scan_interval_seconds": 120.0,
                }
            ),
            {
                "point_lookback_days": 30,
                "scan_interval_seconds": 300,
            },
        )

    def test_yaml_options_use_scan_interval_seconds_floor(self) -> None:
        self.assertEqual(
            config_payload.entry_options_from_yaml(
                {
                    "point_lookback_days": 45,
                    "scan_interval": timedelta(seconds=120),
                }
            ),
            {
                "point_lookback_days": 45,
                "scan_interval_seconds": 300,
            },
        )

    def test_merge_import_options_preserves_ui_mapping_options(self) -> None:
        self.assertEqual(
            config_payload.merge_import_options(
                {
                    "point_lookback_days": 30,
                    "scan_interval_seconds": 900,
                    "thermostats": [{"id": 1, "filter_changed_date": "2026-07-05"}],
                    "sensors": [{"id": 2, "temperature_entity_id": "sensor.room"}],
                },
                {"api_key": "key", "api_base": "https://api.beestat.io/"},
                {
                    "point_lookback_days": 90,
                    "scan_interval_seconds": 1800,
                },
            ),
            {
                "point_lookback_days": 90,
                "scan_interval_seconds": 1800,
                "thermostats": [{"id": 1, "filter_changed_date": "2026-07-05"}],
                "sensors": [{"id": 2, "temperature_entity_id": "sensor.room"}],
            },
        )

    def test_merge_import_options_preserves_filter_boundary_with_yaml_mapping(
        self,
    ) -> None:
        self.assertEqual(
            config_payload.merge_import_options(
                {
                    "point_lookback_days": 30,
                    "scan_interval_seconds": 900,
                    "thermostats": [
                        {
                            "id": 1,
                            "climate_entity_id": "climate.old",
                            "filter_changed_date": "2026-07-05",
                            "filter_changed_at": "2026-07-05T21:48:00+00:00",
                            "filter_change_day_runtime_baseline_seconds": 28800,
                            "filter_change_boundary_reconciled_at": (
                                "2026-07-05T22:05:00+00:00"
                            ),
                            "filter_change_boundary_source_data_end": (
                                "2026-07-05T21:50:00+00:00"
                            ),
                        },
                        {"id": 2, "filter_changed_date": "2026-06-18"},
                    ],
                },
                {
                    "api_key": "key",
                    "api_base": "https://api.beestat.io/",
                    "thermostats": [
                        {"id": 1, "climate_entity_id": "climate.main"},
                        {"id": 2, "climate_entity_id": "climate.second"},
                    ],
                },
                {
                    "point_lookback_days": 90,
                    "scan_interval_seconds": 1800,
                },
            ),
            {
                "point_lookback_days": 90,
                "scan_interval_seconds": 1800,
                "thermostats": [
                    {
                        "id": 1,
                        "climate_entity_id": "climate.main",
                        "filter_changed_date": "2026-07-05",
                        "filter_changed_at": "2026-07-05T21:48:00+00:00",
                        "filter_change_day_runtime_baseline_seconds": 28800,
                        "filter_change_boundary_reconciled_at": (
                            "2026-07-05T22:05:00+00:00"
                        ),
                        "filter_change_boundary_source_data_end": (
                            "2026-07-05T21:50:00+00:00"
                        ),
                    },
                    {
                        "id": 2,
                        "climate_entity_id": "climate.second",
                        "filter_changed_date": "2026-06-18",
                    },
                ],
            },
        )

    def test_merge_import_options_yaml_filter_date_clears_click_boundary(self) -> None:
        self.assertEqual(
            config_payload.merge_import_options(
                {
                    "thermostats": [
                        {
                            "id": 1,
                            "filter_changed_date": "2026-07-05",
                            "filter_changed_at": "2026-07-05T21:48:00+00:00",
                            "filter_change_day_runtime_baseline_seconds": 28800,
                            "filter_change_boundary_reconciled_at": (
                                "2026-07-05T22:05:00+00:00"
                            ),
                        }
                    ],
                },
                {
                    "thermostats": [
                        {
                            "id": 1,
                            "filter_changed_date": "2026-07-06",
                        }
                    ],
                },
                {},
            ),
            {},
        )

    def test_merge_import_options_clears_yaml_owned_mapping_options(self) -> None:
        self.assertEqual(
            config_payload.merge_import_options(
                {
                    "thermostats": [{"id": 1, "climate_entity_id": "climate.old"}],
                },
                {
                    "thermostats": [{"id": 1, "climate_entity_id": "climate.main"}],
                },
                {},
            ),
            {},
        )

    def test_migrate_entry_payload_moves_legacy_options_from_data(self) -> None:
        data, options = config_payload.migrate_entry_payload(
            {
                "api_key": "key",
                "point_lookback_days": "60",
                "scan_interval_seconds": 120,
                "thermostats": [{"id": 1, "enabled": False}],
            },
            {},
        )

        self.assertEqual(
            data,
            {
                "api_key": "key",
                "api_base": "https://api.beestat.io/",
                "thermostats": [{"id": 1, "enabled": False}],
            },
        )
        self.assertEqual(
            options,
            {
                "point_lookback_days": 60,
                "scan_interval_seconds": 300,
            },
        )

    def test_migrate_entry_payload_preserves_existing_options(self) -> None:
        data, options = config_payload.migrate_entry_payload(
            {
                "api_key": "key",
                "api_base": "https://example.test/",
                "point_lookback_days": 60,
                "scan_interval_seconds": 120,
                "scan_interval": timedelta(seconds=180),
            },
            {
                "point_lookback_days": 30,
                "scan_interval_seconds": 600,
            },
        )

        self.assertEqual(
            data,
            {
                "api_key": "key",
                "api_base": "https://example.test/",
            },
        )
        self.assertEqual(
            options,
            {
                "point_lookback_days": 30,
                "scan_interval_seconds": 600,
            },
        )

    def test_runtime_config_data_prefers_ui_mapping_options(self) -> None:
        entry = types.SimpleNamespace(
            data={
                "api_key": "key",
                "thermostats": [{"id": 1, "climate_entity_id": "climate.old"}],
            },
            options={
                "thermostats": [{"id": 1, "climate_entity_id": "climate.new"}],
                "point_lookback_days": 45,
            },
        )

        self.assertEqual(
            config_payload.entry_runtime_config_data(entry),
            {
                "api_key": "key",
                "thermostats": [{"id": 1, "climate_entity_id": "climate.new"}],
            },
        )

    def test_update_source_scope_preserves_overrides_and_discovery_drift(self) -> None:
        options = config_payload.update_source_scope_options(
            {
                "thermostats": [
                    {"id": 1, "slug": "zone_a"},
                    {"id": 3, "enabled": False},
                ],
            },
            {
                "point_lookback_days": 45,
                "sensors": [
                    {
                        "id": 10,
                        "temperature_entity_id": "sensor.room_sensor_a",
                        "include_voc": False,
                    },
                    {"id": 12, "enabled": False},
                ],
            },
            known_thermostat_ids=(1, 2, 3),
            enabled_thermostat_ids=(2, 3),
            explicitly_enabled_thermostat_ids=(2,),
            known_sensor_ids=(10, 11, 12),
            enabled_sensor_ids=(10, 12),
        )

        self.assertEqual(options["point_lookback_days"], 45)
        self.assertEqual(
            options["thermostats"],
            [
                {"id": 1, "slug": "zone_a", "enabled": False},
                {"id": 2, "enabled": True},
            ],
        )
        self.assertEqual(
            options["sensors"],
            [
                {
                    "id": 10,
                    "temperature_entity_id": "sensor.room_sensor_a",
                    "include_voc": False,
                },
                {"id": 11, "enabled": False},
            ],
        )

    def test_update_source_scope_keeps_unknown_saved_items_unchanged(self) -> None:
        options = config_payload.update_source_scope_options(
            {},
            {
                "thermostats": [
                    {"id": 99, "name": "saved", "enabled": False},
                ],
            },
            known_thermostat_ids=(1,),
            enabled_thermostat_ids=(1,),
            known_sensor_ids=(),
            enabled_sensor_ids=(),
        )

        self.assertEqual(
            options["thermostats"],
            [{"id": 99, "name": "saved", "enabled": False}],
        )

    def test_update_thermostat_override_options_merges_one_item(self) -> None:
        options = config_payload.update_thermostat_override_options(
            {
                "thermostats": [
                    {
                        "id": 1,
                        "slug": "main",
                        "climate_entity_id": "climate.old",
                    }
                ]
            },
            {"point_lookback_days": 45},
            1,
            {
                "climate_entity_id": "climate.new",
                "temperature_entity_id": "",
                "filter_changed_date": "2026-07-05",
                "filter_lifetime_runtime_hours": 300,
                "filter_max_age_days": 120,
                "filter_notice_days": 14,
            },
        )

        self.assertEqual(options["point_lookback_days"], 45)
        self.assertEqual(
            options["thermostats"],
            [
                {
                    "id": 1,
                    "slug": "main",
                    "climate_entity_id": "climate.new",
                    "filter_changed_date": "2026-07-05",
                    "filter_lifetime_runtime_hours": 300,
                    "filter_max_age_days": 120,
                    "filter_notice_days": 14,
                }
            ],
        )

    def test_update_sensor_override_options_adds_one_item(self) -> None:
        options = config_payload.update_sensor_override_options(
            {},
            {},
            2,
            {
                "temperature_entity_id": "sensor.room",
                "include_temperature": True,
                "include_air_quality": False,
            },
        )

        self.assertEqual(
            options["sensors"],
            [
                {
                    "id": 2,
                    "temperature_entity_id": "sensor.room",
                    "include_temperature": True,
                    "include_air_quality": False,
                }
            ],
        )

    def test_update_sensor_override_preserves_missing_mapping_fields(self) -> None:
        reference = {
            "registry_entry_id": "registry-entry-a",
            "domain": "sensor",
            "platform": "homekit_controller",
            "unique_id": "room-sensor-a-temperature",
        }

        options = config_payload.update_sensor_override_options(
            {},
            {
                "sensors": [
                    {
                        "id": 2,
                        "temperature_entity_id": "sensor.room_sensor_a",
                        "temperature_entity_ref": reference,
                    }
                ]
            },
            2,
            {"include_temperature": False},
        )

        self.assertEqual(
            options["sensors"],
            [
                {
                    "id": 2,
                    "temperature_entity_id": "sensor.room_sensor_a",
                    "temperature_entity_ref": reference,
                    "include_temperature": False,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()

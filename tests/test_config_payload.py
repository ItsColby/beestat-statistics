"""Tests for config-entry payload helpers."""

from __future__ import annotations

import types
import unittest
from datetime import date

from custom_components.beestat_statistics import config_payload, config_rows


class ConfigPayloadTest(unittest.TestCase):
    """Validate dependency-free config-entry payload shaping."""

    def test_resource_id_normalization_preserves_exact_positive_identity(self) -> None:
        for value in (1, 1.0, "001", " 1 "):
            with self.subTest(value=value):
                self.assertEqual(config_rows.positive_resource_id(value), 1)
        for value in (
            True,
            False,
            0,
            -1,
            1.5,
            float("inf"),
            float("nan"),
            None,
            "1.5",
            [],
            {},
        ):
            with self.subTest(value=value):
                self.assertIsNone(config_rows.positive_resource_id(value))

    def test_row_resource_id_skips_malformed_fields(self) -> None:
        self.assertEqual(config_rows.row_resource_id({"id": 1.0}, "id"), 1)
        self.assertEqual(
            config_rows.row_resource_id(
                {"thermostat_id": True, "id": "2"}, "thermostat_id", "id"
            ),
            2,
        )

    def test_malformed_resource_ids_cannot_shadow_valid_override(self) -> None:
        valid = {"id": 1, "filter_notice_days": 7}
        rows = [valid] + [
            {"id": value, "future": {"preserve": True}}
            for value in (True, 1.5, 0, -1, float("inf"), None, "invalid")
        ]
        options = {"thermostats": rows}
        updated = config_payload.update_thermostat_override_options(
            {}, options, 1, {"filter_notice_days": 14}
        )
        self.assertEqual(updated["thermostats"][1:], rows[1:])
        self.assertEqual(
            config_payload.effective_thermostat_override({}, updated, 1),
            {"id": 1, "filter_notice_days": 14},
        )
        self.assertEqual(valid["filter_notice_days"], 7)

    def test_override_identity_cannot_fall_back_to_sensor_parent(self) -> None:
        for row in (
            {"id": True, "sensor_id": 2, "thermostat_id": 1},
            {"id": 1.5, "thermostat_id": 1},
            {"sensor_id": None, "thermostat_id": 1},
        ):
            with self.subTest(row=row):
                self.assertIsNone(config_rows.override_id(row))
        self.assertEqual(
            config_rows.override_id({"sensor_id": 2, "thermostat_id": 1}), 2
        )
        self.assertEqual(config_rows.override_id({"thermostat_id": 1}), 1)

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

    def test_repeated_override_id_uses_last_row_consistently(self) -> None:
        options = {
            "thermostats": [
                {"id": 1, "filter_changed_at": "shadowed", "future": "keep"},
                {"id": 1, "filter_changed_at": "effective"},
            ]
        }

        self.assertEqual(
            config_payload.effective_thermostat_override({}, options, 1),
            {"id": 1, "filter_changed_at": "effective"},
        )

        updated = config_payload.update_thermostat_override_options(
            {},
            options,
            1,
            {"filter_changed_date": "2026-08-08"},
        )

        self.assertEqual(updated["thermostats"][0], options["thermostats"][0])
        self.assertEqual(
            updated["thermostats"][1],
            {
                "id": 1,
                "filter_changed_at": "effective",
                "filter_changed_date": "2026-08-08",
            },
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

    def test_targeted_edits_preserve_unowned_rows_and_effective_defaults(self) -> None:
        rows = [
            {"id": 1, "enabled": False},
            {"id": 1},
            {"id": 99},
            {"id": "invalid"},
            None,
            "future row",
            ["future", "shape"],
        ]
        options = {"thermostats": rows, "sensors": rows}
        for update, key in (
            (config_payload.update_thermostat_override_options, "thermostats"),
            (config_payload.update_sensor_override_options, "sensors"),
        ):
            with self.subTest(key=key):
                updated = update(
                    {}, options, 1, {"temperature_entity_id": "sensor.room"}
                )
                self.assertEqual(updated[key][0], rows[0])
                self.assertEqual(updated[key][2:], rows[2:])
                self.assertEqual(
                    updated[key][1]["temperature_entity_id"], "sensor.room"
                )

        scoped = config_payload.update_source_scope_options(
            {},
            options,
            known_thermostat_ids=(1,),
            enabled_thermostat_ids=(1,),
            known_sensor_ids=(1,),
            enabled_sensor_ids=(1,),
        )
        self.assertEqual(scoped, options)
        self.assertEqual(
            config_payload.effective_thermostat_override({}, scoped, 1), {"id": 1}
        )
        self.assertEqual(rows[1], {"id": 1})

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

    def test_normalizers_bound_malformed_timing_options(self) -> None:
        """Persisted timing corruption degrades to supported bounded values."""

        self.assertEqual(config_payload.normalize_point_lookback_days(1000000), 366)
        self.assertEqual(
            config_payload.normalize_scan_interval_seconds("invalid"), 21600
        )
        self.assertEqual(config_payload.normalize_point_lookback_days("invalid"), 45)
        self.assertEqual(config_payload.normalize_scan_interval_seconds(120), 300)
        self.assertEqual(
            config_payload.normalize_scan_interval_seconds(10**100),
            31_536_000,
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

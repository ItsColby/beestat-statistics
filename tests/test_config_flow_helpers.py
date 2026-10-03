"""Tests for dependency-light config-flow helpers."""

from __future__ import annotations

import types
import unittest

from custom_components.beestat_statistics import config_flow


class ConfigFlowHelpersTest(unittest.TestCase):
    """Validate config-flow helpers without a Home Assistant test harness."""

    def setUp(self) -> None:
        self.config_flow = config_flow

    def test_account_fingerprint_uses_hashed_thermostat_anchors(self) -> None:
        fingerprint = self.config_flow._account_fingerprint(
            [
                {"id": 1001},
                {"thermostat_id": "2002"},
                {"id": None},
            ]
        )

        self.assertIsNotNone(fingerprint)
        text = repr(fingerprint)
        self.assertNotIn("1001", text)
        self.assertNotIn("2002", text)
        self.assertEqual(len(fingerprint["thermostat_id_hashes"]), 2)
        self.assertTrue(
            all(len(value) == 64 for value in fingerprint["thermostat_id_hashes"])
        )
        self.assertEqual(len(fingerprint["signature"]), 64)

    def test_account_anchors_normalize_exact_ids_and_reject_malformed_ids(self) -> None:
        expected = self.config_flow._account_fingerprint([{"id": 1}])
        self.assertEqual(
            self.config_flow._account_fingerprint(
                [{"id": "001"}, {"id": 1.0}, {"id": True}, {"id": 1.5}, {"id": 0}]
            ),
            expected,
        )
        self.assertIsNone(
            self.config_flow._account_fingerprint([{"id": True}, {"id": 1.5}])
        )

    def test_wrong_account_supports_legacy_signature_values(self) -> None:
        self.assertFalse(
            self.config_flow._wrong_account(
                {"account_fingerprint": "same-signature"},
                "same-signature",
            )
        )
        self.assertTrue(
            self.config_flow._wrong_account(
                {"account_fingerprint": "old-signature"},
                "new-signature",
            )
        )

    def test_same_connection_data_defaults_api_base(self) -> None:
        self.assertTrue(
            self.config_flow._same_connection_data(
                {"api_key": "key"},
                {
                    "api_key": "key",
                    "api_base": "https://api.beestat.io/",
                },
            )
        )
        self.assertFalse(
            self.config_flow._same_connection_data(
                {
                    "api_key": "key",
                    "api_base": "https://api.beestat.io/",
                },
                {
                    "api_key": "other-key",
                    "api_base": "https://api.beestat.io/",
                },
            )
        )

    def test_validated_connection_change_requires_known_same_account(self) -> None:
        current = {
            "account_fingerprint": {
                "thermostat_id_hashes": ["shared", "old"],
                "signature": "old-signature",
            }
        }

        self.assertTrue(
            self.config_flow._validated_connection_change_is_safe(
                current,
                {
                    "thermostat_id_hashes": ["shared", "new"],
                    "signature": "new-signature",
                },
            )
        )
        self.assertFalse(
            self.config_flow._validated_connection_change_is_safe(
                current,
                {
                    "thermostat_id_hashes": ["other"],
                    "signature": "other-signature",
                },
            )
        )
        self.assertFalse(
            self.config_flow._validated_connection_change_is_safe({}, current)
        )
        self.assertFalse(
            self.config_flow._validated_connection_change_is_safe(current, None)
        )

    def test_source_scope_signature_includes_labels_and_activity_context(self) -> None:
        """Source drift evidence covers more than the discovered IDs."""

        self.assertEqual(
            self.config_flow._source_scope_signature(
                [{"value": "1", "label": "Zone A (1, inactive)"}],
                [{"value": "2", "label": "Room B (2)"}],
            ),
            (
                ("thermostat", "1", "Zone A (1, inactive)"),
                ("sensor", "2", "Room B (2)"),
            ),
        )

    def test_sensor_source_identity_excludes_parent_thermostat_id(self) -> None:
        entry = types.SimpleNamespace(
            data={},
            options={"sensors": [{"sensor_id": 2003, "thermostat_id": 1001}]},
            runtime_data=types.SimpleNamespace(
                coordinator=types.SimpleNamespace(
                    data=types.SimpleNamespace(
                        config=types.SimpleNamespace(sensors=()),
                        sensor_rows=(
                            {
                                "sensor_id": 2001,
                                "thermostat_id": 1001,
                                "name": "Room A",
                                "inactive": True,
                            },
                            {"id": 2002, "thermostat_id": 1001, "name": "Room B"},
                        ),
                    )
                )
            ),
        )
        choices = self.config_flow._sensor_options(entry)
        self.assertEqual({item["value"] for item in choices}, {"2001", "2002", "2003"})
        self.assertEqual(
            self.config_flow._inactive_resource_ids(
                entry, rows_attribute="sensor_rows", id_field="sensor_id"
            ),
            {2001},
        )

    def test_saved_sensor_mapping_identity_uses_override_id_not_parent(self) -> None:
        override = {
            "id": 2001,
            "thermostat_id": 1001,
            "temperature_entity_id": "sensor.selected",
        }
        self.assertEqual(
            self.config_flow._effective_override(
                {"sensors": [override]}, "sensors", 2001
            ),
            override,
        )

    def test_automatic_mapping_rechecks_current_registry_device_conflicts(self) -> None:
        first = types.SimpleNamespace(
            id="first-registry-id",
            entity_id="climate.first",
            domain="climate",
            platform="homekit_controller",
            unique_id="first-source",
            device_id="first-device",
        )
        second = types.SimpleNamespace(
            id="second-registry-id",
            entity_id="climate.second",
            domain="climate",
            platform="homekit_controller",
            unique_id="second-source",
            device_id="second-device",
        )
        entries = (first, second)
        registry = types.SimpleNamespace(
            async_get=lambda value: next(
                (item for item in entries if value in (item.id, item.entity_id)), None
            ),
            async_get_entity_id=lambda domain, platform, unique_id: next(
                (
                    item.entity_id
                    for item in entries
                    if (item.domain, item.platform, item.unique_id)
                    == (domain, platform, unique_id)
                ),
                None,
            ),
        )
        entry = types.SimpleNamespace(
            data={},
            options={
                "thermostats": [{"id": 1001, "climate_entity_id": first.entity_id}]
            },
            runtime_data=types.SimpleNamespace(
                coordinator=types.SimpleNamespace(
                    data=types.SimpleNamespace(
                        config=types.SimpleNamespace(
                            thermostats=(
                                types.SimpleNamespace(
                                    thermostat_id=1001,
                                    name="First",
                                    climate_entity_id=first.entity_id,
                                ),
                                types.SimpleNamespace(
                                    thermostat_id=1002,
                                    name="Second",
                                    climate_entity_id=second.entity_id,
                                ),
                            ),
                            sensors=(),
                        )
                    )
                )
            ),
        )
        self.assertIsNotNone(
            self.config_flow._automatic_mapping_options(entry, registry)
        )
        second.device_id = first.device_id
        self.assertIsNone(self.config_flow._automatic_mapping_options(entry, registry))
        self.assertEqual(
            entry.options,
            {"thermostats": [{"id": 1001, "climate_entity_id": first.entity_id}]},
        )


if __name__ == "__main__":
    unittest.main()

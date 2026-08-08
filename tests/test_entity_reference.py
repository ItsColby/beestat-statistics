"""Tests for stable Home Assistant entity-registry references."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "beestat_statistics"
PACKAGE = "beestat_statistics_entity_reference_test"


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
entity_reference = _load_module("entity_reference")


@dataclass
class FakeEntityEntry:
    id: str
    entity_id: str
    domain: str
    platform: str
    unique_id: str


class FakeEntityRegistry:
    def __init__(self, entries: list[FakeEntityEntry]) -> None:
        self.entries = entries

    def async_get(self, entity_id_or_uuid: str) -> FakeEntityEntry | None:
        return next(
            (
                entry
                for entry in self.entries
                if entity_id_or_uuid in (entry.id, entry.entity_id)
            ),
            None,
        )

    def async_get_entity_id(
        self,
        domain: str,
        platform: str,
        unique_id: str,
    ) -> str | None:
        return next(
            (
                entry.entity_id
                for entry in self.entries
                if (entry.domain, entry.platform, entry.unique_id)
                == (domain, platform, unique_id)
            ),
            None,
        )


class EntityReferenceTest(unittest.TestCase):
    """Prove rename, recreation, and option migration behavior."""

    def test_reference_resolves_rename_and_recreation(self) -> None:
        original = FakeEntityEntry(
            "registry-a",
            "climate.zone_a",
            "climate",
            "homekit_controller",
            "source-climate",
        )
        registry = FakeEntityRegistry([original])
        reference = entity_reference.entity_reference_from_registry(
            registry,
            original.entity_id,
        )

        self.assertEqual(
            entity_reference.resolve_entity_reference(registry, reference),
            "climate.zone_a",
        )
        original.entity_id = "climate.zone_a_renamed"
        self.assertEqual(
            entity_reference.resolve_entity_reference(registry, reference),
            "climate.zone_a_renamed",
        )

        registry.entries = [
            FakeEntityEntry(
                "registry-b",
                "climate.zone_a_restored",
                "climate",
                "homekit_controller",
                "source-climate",
            )
        ]
        self.assertEqual(
            entity_reference.resolve_entity_reference(registry, reference),
            "climate.zone_a_restored",
        )

    def test_uuid_mismatch_fails_over_to_stable_source_tuple(self) -> None:
        registry = FakeEntityRegistry(
            [
                FakeEntityEntry(
                    "registry-a",
                    "climate.unrelated",
                    "climate",
                    "homekit_controller",
                    "other-source",
                ),
                FakeEntityEntry(
                    "registry-b",
                    "climate.zone_a",
                    "climate",
                    "homekit_controller",
                    "source-climate",
                ),
            ]
        )
        reference = {
            "registry_entry_id": "registry-a",
            "domain": "climate",
            "platform": "homekit_controller",
            "unique_id": "source-climate",
        }

        self.assertEqual(
            entity_reference.resolve_entity_reference(registry, reference),
            "climate.zone_a",
        )

    def test_mapping_form_defaults_resolve_without_rewriting_storage(self) -> None:
        source = FakeEntityEntry(
            "registry-a",
            "climate.zone_a_renamed",
            "climate",
            "homekit_controller",
            "source-climate",
        )
        registry = FakeEntityRegistry([source])
        reference = entity_reference.entity_reference_from_registry(
            registry,
            source.entity_id,
        )
        stored = {
            "id": 1001,
            "climate_entity_id": "climate.zone_a",
            "climate_entity_ref": reference,
            "filter_notice_days": 14,
        }

        defaults = entity_reference.mapping_form_defaults(
            registry,
            stored,
            entity_reference.THERMOSTAT_STABLE_ENTITY_FIELDS,
        )

        self.assertEqual(defaults["climate_entity_id"], source.entity_id)
        self.assertEqual(defaults["filter_notice_days"], 14)
        self.assertEqual(stored["climate_entity_id"], "climate.zone_a")

    def test_mapping_form_defaults_hide_missing_source_and_recover(self) -> None:
        reference = {
            "registry_entry_id": "registry-a",
            "domain": "sensor",
            "platform": "homekit_controller",
            "unique_id": "room-sensor-a-temperature",
        }
        stored = {
            "id": 2001,
            "temperature_entity_id": "sensor.room_sensor_a_temperature",
            "temperature_entity_ref": reference,
        }
        registry = FakeEntityRegistry([])

        missing_defaults = entity_reference.mapping_form_defaults(
            registry,
            stored,
            entity_reference.SENSOR_STABLE_ENTITY_FIELDS,
        )

        self.assertNotIn("temperature_entity_id", missing_defaults)
        registry.entries = [
            FakeEntityEntry(
                "registry-b",
                "sensor.room_sensor_a_temperature_restored",
                "sensor",
                "homekit_controller",
                "room-sensor-a-temperature",
            )
        ]
        restored_defaults = entity_reference.mapping_form_defaults(
            registry,
            stored,
            entity_reference.SENSOR_STABLE_ENTITY_FIELDS,
        )
        self.assertEqual(
            restored_defaults["temperature_entity_id"],
            "sensor.room_sensor_a_temperature_restored",
        )

    def test_migration_backfills_only_options_owned_mappings(self) -> None:
        registry = FakeEntityRegistry(
            [
                FakeEntityEntry(
                    "registry-a",
                    "climate.zone_a",
                    "climate",
                    "homekit_controller",
                    "source-climate",
                )
            ]
        )
        options = {"thermostats": [{"id": 1001, "climate_entity_id": "climate.zone_a"}]}

        migrated = entity_reference.migrate_option_entity_references(
            registry,
            options,
        )

        self.assertEqual(
            migrated["thermostats"][0]["climate_entity_ref"],
            {
                "registry_entry_id": "registry-a",
                "domain": "climate",
                "platform": "homekit_controller",
                "unique_id": "source-climate",
            },
        )
        self.assertEqual(
            migrated["thermostats"][0]["climate_entity_id"],
            "climate.zone_a",
        )


if __name__ == "__main__":
    unittest.main()

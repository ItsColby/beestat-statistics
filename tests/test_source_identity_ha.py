"""Native-registry proof for physical thermostat temperature mappings."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beestat_statistics import _async_track_source_device_relinks
from custom_components.beestat_statistics.config_model import (
    BeestatConfig,
    ConfiguredThermostat,
    build_beestat_config,
    configured_mapping_device_conflicts,
)
from custom_components.beestat_statistics.const import DOMAIN

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def _skip_dependency_setup():
    """Use the native flow harness without starting the unrelated Recorder."""

    with patch(
        "homeassistant.config_entries.async_process_deps_reqs", new_callable=AsyncMock
    ):
        yield


@pytest.fixture
def thermostat_pair(hass: HomeAssistant):
    """Create separate native registrations with a synthetic hardware serial."""

    homekit = MockConfigEntry(domain="homekit_controller")
    cloud = MockConfigEntry(domain="ecobee")
    homekit.add_to_hass(hass)
    cloud.add_to_hass(hass)
    devices = dr.async_get(hass)
    homekit_device = devices.async_get_or_create(
        config_entry_id=homekit.entry_id,
        identifiers={("homekit_controller:accessory-id", "synthetic:aid:1")},
        manufacturer="ecobee Inc.",
        serial_number="123456789012",
        name="Local thermostat",
    )
    cloud_device = devices.async_get_or_create(
        config_entry_id=cloud.entry_id,
        identifiers={("ecobee", "123456789012")},
        manufacturer="ecobee",
        name="Different cloud label",
    )
    registry = er.async_get(hass)
    climate = registry.async_get_or_create(
        "climate",
        "homekit_controller",
        "synthetic_1_10",
        config_entry=homekit,
        device_id=homekit_device.id,
    )
    motion = registry.async_get_or_create(
        "binary_sensor",
        "homekit_controller",
        "synthetic_1_20",
        config_entry=homekit,
        device_id=homekit_device.id,
    )
    temperature = registry.async_get_or_create(
        "sensor",
        "ecobee",
        "123456789012-ei:0-temperature",
        config_entry=cloud,
        device_id=cloud_device.id,
        original_device_class="temperature",
        unit_of_measurement="°F",
    )
    row = {
        "id": 1001,
        "climate_entity_id": climate.entity_id,
        "temperature_entity_id": temperature.entity_id,
        "motion_entity_id": motion.entity_id,
    }
    return types.SimpleNamespace(
        homekit=homekit,
        cloud=cloud,
        homekit_device=homekit_device,
        cloud_device=cloud_device,
        climate=climate,
        motion=motion,
        temperature=temperature,
        row=row,
    )


def test_native_identity_is_required_for_mixed_probe_mapping(hass, thermostat_pair):
    pair = thermostat_pair
    config = {"thermostats": [pair.row]}
    registry = er.async_get(hass)
    # The previous registry-device-ID-only guard rejects this physical device.
    assert (
        configured_mapping_device_conflicts(config, registry)[0].reason
        == "cross_device"
    )
    assert (
        configured_mapping_device_conflicts(config, registry, dr.async_get(hass)) == ()
    )


@pytest.mark.parametrize(
    "case",
    [
        "missing_serial",
        "wrong_serial",
        "wrong_manufacturer",
        "remote_probe",
        "wrong_identifier",
        "ambiguous_homekit",
    ],
)
def test_mixed_mapping_rejects_unproven_identity(hass, thermostat_pair, case):
    pair = thermostat_pair
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    if case == "missing_serial":
        devices.async_update_device(pair.homekit_device.id, serial_number=None)
    elif case == "wrong_serial":
        devices.async_update_device(
            pair.homekit_device.id, serial_number="999999999999"
        )
    elif case == "wrong_manufacturer":
        devices.async_update_device(pair.homekit_device.id, manufacturer="Other")
    elif case == "remote_probe":
        registry.async_update_entity(
            pair.temperature.entity_id, new_unique_id="ROOM123-temperature"
        )
    elif case == "wrong_identifier":
        devices.async_update_device(
            pair.cloud_device.id, new_identifiers={("ecobee", "999999999999")}
        )
    else:
        duplicate = devices.async_get_or_create(
            config_entry_id=pair.homekit.entry_id,
            identifiers={("homekit_controller:accessory-id", "duplicate:aid:1")},
            manufacturer="ecobee",
            serial_number="123456789012",
        )
        registry.async_get_or_create(
            "climate",
            "homekit_controller",
            "duplicate_1_10",
            config_entry=pair.homekit,
            device_id=duplicate.id,
        )
    conflicts = configured_mapping_device_conflicts(
        {"thermostats": [pair.row]}, registry, devices
    )
    assert [conflict.reason for conflict in conflicts] == ["cross_device"]


def test_two_rows_cannot_claim_the_two_registrations_separately(hass, thermostat_pair):
    pair = thermostat_pair
    rows = [
        {"id": 1001, "climate_entity_id": pair.climate.entity_id},
        {"id": 1002, "temperature_entity_id": pair.temperature.entity_id},
    ]
    conflicts = configured_mapping_device_conflicts(
        {"thermostats": rows}, er.async_get(hass), dr.async_get(hass)
    )
    assert len(conflicts) == 1
    assert conflicts[0].reason == "duplicate_device"
    assert conflicts[0].resource_ids == (1001, 1002)


def test_existing_cloud_only_mapping_remains_valid(hass, thermostat_pair):
    pair = thermostat_pair
    registry = er.async_get(hass)
    cloud_climate = registry.async_get_or_create(
        "climate",
        "ecobee",
        "123456789012",
        config_entry=pair.cloud,
        device_id=pair.cloud_device.id,
    )
    row = {
        "id": 1001,
        "climate_entity_id": cloud_climate.entity_id,
        "temperature_entity_id": pair.temperature.entity_id,
    }
    assert (
        configured_mapping_device_conflicts(
            {"thermostats": [row]}, registry, dr.async_get(hass)
        )
        == ()
    )


@pytest.mark.parametrize("source", ["same_device", "matched_cloud", "wrong_device"])
def test_builtin_sensor_override_must_agree_with_inherited_parent(
    hass, thermostat_pair, source
):
    pair = thermostat_pair
    registry = er.async_get(hass)
    temperature = pair.temperature
    if source != "matched_cloud":
        device_id = pair.homekit_device.id
        if source == "wrong_device":
            device_id = (
                dr.async_get(hass)
                .async_get_or_create(
                    config_entry_id=pair.homekit.entry_id,
                    identifiers={("homekit_controller:accessory-id", "other:aid:2")},
                    manufacturer="ecobee",
                    name="Other room",
                )
                .id
            )
        temperature = registry.async_get_or_create(
            "sensor",
            "homekit_controller",
            "synthetic_temperature",
            config_entry=pair.homekit,
            device_id=device_id,
            original_device_class="temperature",
        )
    config = build_beestat_config(
        hass,
        ({"id": 1001, "name": "Zone"},),
        ({"id": 2001, "thermostat_id": 1001, "type": "thermostat"},),
        {
            "thermostats": [{"id": 1001, "climate_entity_id": pair.climate.entity_id}],
            "sensors": [{"id": 2001, "temperature_entity_id": temperature.entity_id}],
        },
    )
    sensor = config.sensors[0]
    if source == "wrong_device":
        assert sensor.device_id is None
        assert sensor.temperature_entity_id is None
        assert [
            (item.resource_type, item.resource_ids, item.reason)
            for item in config.mapping_device_conflicts
        ] == [("sensor", (2001,), "cross_device")]
    else:
        assert sensor.device_id == pair.homekit_device.id
        assert sensor.temperature_entity_id == temperature.entity_id
        assert config.mapping_device_conflicts == ()


async def test_builtin_sensor_mapping_flow_rejects_foreign_device(
    hass, thermostat_pair
):
    pair = thermostat_pair
    rows = ({"id": 1001, "name": "Zone"},)
    sensors = ({"id": 2001, "thermostat_id": 1001, "type": "thermostat"},)
    options = {
        "thermostats": [{"id": 1001, "climate_entity_id": pair.climate.entity_id}]
    }
    entry = MockConfigEntry(
        domain=DOMAIN, data={"api_key": "synthetic-key"}, options=options
    )
    entry.add_to_hass(hass)
    entry.runtime_data = types.SimpleNamespace(
        coordinator=types.SimpleNamespace(
            data=types.SimpleNamespace(
                thermostat_rows=rows,
                sensor_rows=sensors,
                config=build_beestat_config(hass, rows, sensors, options),
            )
        )
    )
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=pair.homekit.entry_id,
        identifiers={("homekit_controller:accessory-id", "other:aid:2")},
        manufacturer="ecobee",
    )
    temperature = er.async_get(hass).async_get_or_create(
        "sensor",
        "homekit_controller",
        "other_temperature",
        config_entry=pair.homekit,
        device_id=other.id,
        original_device_class="temperature",
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "sensor_mapping"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "2001"}
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"temperature_entity_id": temperature.entity_id}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "mapping_device_conflict"}
    assert entry.options == options
    reload.assert_not_called()


async def test_options_save_mixed_mapping_with_stable_references(hass, thermostat_pair):
    pair = thermostat_pair
    entry = MockConfigEntry(domain=DOMAIN, data={"api_key": "synthetic-key"})
    entry.add_to_hass(hass)
    entry.runtime_data = types.SimpleNamespace(
        coordinator=types.SimpleNamespace(
            data=types.SimpleNamespace(
                config=BeestatConfig(
                    thermostats=(
                        ConfiguredThermostat(
                            thermostat_id=1001, name="Zone", slug="zone"
                        ),
                    ),
                    sensors=(),
                )
            )
        )
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "thermostat_mapping"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "1001"}
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {key: value for key, value in pair.row.items() if key != "id"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    saved = entry.options["thermostats"][0]
    assert saved["temperature_entity_ref"]["unique_id"] == pair.temperature.unique_id
    assert saved["climate_entity_ref"]["unique_id"] == pair.climate.unique_id
    reload.assert_called_once()


@pytest.mark.parametrize("mapping_owner", ["thermostat", "builtin_sensor"])
@pytest.mark.parametrize(
    "change",
    [
        "identifier",
        "cloud_detach",
        "homekit_detach",
        "duplicate_create",
        "duplicate_serial",
    ],
)
async def test_identity_drift_rebuilds_both_sources_and_recovers(
    hass, thermostat_pair, change, mapping_owner
):
    pair = thermostat_pair
    options = {
        "thermostat": {"thermostats": [pair.row]},
        "builtin_sensor": {
            "thermostats": [{"id": 1001, "climate_entity_id": pair.climate.entity_id}],
            "sensors": [
                {"id": 2001, "temperature_entity_id": pair.temperature.entity_id}
            ],
        },
    }[mapping_owner]
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.add_to_hass(hass)
    rows = ({"id": 1001, "name": "Zone"},)
    sensors = ({"id": 2001, "thermostat_id": 1001, "type": "thermostat"},)

    def rebuild():
        return build_beestat_config(hass, rows, sensors, options)

    def mapped_source(config):
        return (
            config.thermostats[0]
            if mapping_owner == "thermostat"
            else config.sensors[0]
        )

    initial = rebuild()
    assert initial.thermostats[0].device_id == pair.homekit_device.id
    assert mapped_source(initial).temperature_entity_id == pair.temperature.entity_id
    # Cloud availability never changes the explicit source to the display value.
    hass.states.async_set(
        pair.temperature.entity_id, "unavailable", {"unit_of_measurement": "°F"}
    )
    assert mapped_source(rebuild()).temperature_entity_id == pair.temperature.entity_id
    listeners = []

    def add_listener(listener):
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    def update():
        coordinator.data = types.SimpleNamespace(
            config=rebuild(), thermostat_rows=rows, sensor_rows=sensors
        )
        for listener in tuple(listeners):
            listener()

    coordinator = types.SimpleNamespace(
        data=types.SimpleNamespace(
            config=initial, thermostat_rows=rows, sensor_rows=sensors
        ),
        async_add_listener=add_listener,
        async_rebuild_runtime_from_cached_rows=Mock(side_effect=update),
    )
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    duplicate = None
    if change == "duplicate_serial":
        duplicate = devices.async_get_or_create(
            config_entry_id=pair.homekit.entry_id,
            identifiers={("homekit_controller:accessory-id", "other:aid:1")},
            manufacturer="ecobee",
            serial_number="999999999999",
        )
        registry.async_get_or_create(
            "climate",
            "homekit_controller",
            "other_1_10",
            config_entry=pair.homekit,
            device_id=duplicate.id,
        )
    _async_track_source_device_relinks(hass, entry)
    if change == "identifier":
        devices.async_update_device(
            pair.cloud_device.id, new_identifiers={("ecobee", "999999999999")}
        )
    elif change == "cloud_detach":
        registry.async_update_entity(pair.temperature.entity_id, device_id=None)
    elif change == "homekit_detach":
        registry.async_update_entity(pair.climate.entity_id, device_id=None)
    elif change == "duplicate_create":
        duplicate = devices.async_get_or_create(
            config_entry_id=pair.homekit.entry_id,
            identifiers={("homekit_controller:accessory-id", "duplicate:aid:1")},
            manufacturer="ecobee",
            serial_number="123456789012",
        )
        registry.async_get_or_create(
            "climate",
            "homekit_controller",
            "duplicate_1_10",
            config_entry=pair.homekit,
            device_id=duplicate.id,
        )
    else:
        devices.async_update_device(duplicate.id, serial_number="123456789012")
    await hass.async_block_till_done()
    coordinator.async_rebuild_runtime_from_cached_rows.assert_called()
    rejected = mapped_source(rebuild())
    assert rejected.device_id is None
    assert rejected.temperature_entity_id is None
    assert rebuild().thermostats[0].climate_entity_id == pair.climate.entity_id
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "mapping_device_conflicts")
        is not None
    )
    _restore_identity(change, pair, duplicate, devices, registry)
    await hass.async_block_till_done()
    assert mapped_source(rebuild()).temperature_entity_id == pair.temperature.entity_id
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "mapping_device_conflicts") is None
    )
    await entry._async_process_on_unload(hass)
    coordinator.async_rebuild_runtime_from_cached_rows.reset_mock()
    devices.async_update_device(pair.cloud_device.id, name="After unload")
    await hass.async_block_till_done()
    coordinator.async_rebuild_runtime_from_cached_rows.assert_not_called()


def _restore_identity(change, pair, duplicate, devices, registry):
    if change == "identifier":
        devices.async_update_device(
            pair.cloud_device.id, new_identifiers={("ecobee", "123456789012")}
        )
    elif change == "cloud_detach":
        registry.async_update_entity(
            pair.temperature.entity_id, device_id=pair.cloud_device.id
        )
    elif change == "homekit_detach":
        registry.async_update_entity(
            pair.climate.entity_id, device_id=pair.homekit_device.id
        )
    else:
        devices.async_remove_device(duplicate.id)

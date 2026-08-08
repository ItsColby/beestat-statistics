"""Home Assistant harness tests for Beestat Statistics.

These tests exercise the real Home Assistant flow manager and registry helpers.
They intentionally fail collection when the declared HA harness is unavailable.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_IMPORT,
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity import Entity
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beestat_statistics import (
    CONFIG_SCHEMA,
    _async_track_override_issue_updates,
    _async_track_source_device_relinks,
    _async_update_override_issues,
    async_migrate_entry,
    async_setup,
)
from custom_components.beestat_statistics import (
    async_setup_entry as async_setup_entry_impl,
)
from custom_components.beestat_statistics.api import (
    BeestatApiError,
    BeestatAuthError,
)
from custom_components.beestat_statistics.button import BeestatFilterChangedButton
from custom_components.beestat_statistics.config_model import (
    BeestatConfig,
    ConfiguredSensor,
    ConfiguredThermostat,
)
from custom_components.beestat_statistics.const import (
    API_BASE,
    ATTR_CHANGED_AT,
    ATTR_CONFIG_ENTRY_ID,
    CONF_ACCOUNT_FINGERPRINT,
    CONF_API_BASE,
    CONF_CLIMATE_ENTITY_ID,
    CONF_CLIMATE_ENTITY_REF,
    CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS,
    CONF_FILTER_CHANGED_DATE,
    CONF_ID,
    CONF_POINT_LOOKBACK_DAYS,
    CONF_SCAN_INTERVAL_SECONDS,
    CONF_SENSORS,
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_TEMPERATURE_ENTITY_REF,
    CONF_THERMOSTATS,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_UNIQUE_ID,
    CONFIG_ENTRY_VERSION,
    CONFIG_TITLE,
    DEFAULT_POINT_LOOKBACK_DAYS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    SERVICE_GET_CONFIGURATION,
    SERVICE_REPAIR_FILTER_CHANGE_BOUNDARY,
    thermostat_entity_unique_id,
)
from custom_components.beestat_statistics.date import BeestatFilterChangedDate
from custom_components.beestat_statistics.entity import (
    async_remove_cross_integration_device_ownership,
    link_entity_to_device,
)
from custom_components.beestat_statistics.entity_reference import (
    resolve_entity_reference,
)
from custom_components.beestat_statistics.issues import (
    YAML_CONNECTION_CHANGE_ISSUE_ID,
    async_set_yaml_connection_change_issue,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations"),
]


USER_INPUT = {
    CONF_API_KEY: "test-api-key",
    CONF_API_BASE: API_BASE,
}
ACCOUNT_A = {
    "thermostat_id_hashes": ["account-a"],
    "signature": "account-a",
}
ACCOUNT_B = {
    "thermostat_id_hashes": ["account-b"],
    "signature": "account-b",
}


async def test_mapped_entities_link_without_shared_device_ownership(
    hass: HomeAssistant,
) -> None:
    """Test the supported helper-device migration against real HA registries."""

    source_entry = MockConfigEntry(domain="homekit_controller")
    source_entry.add_to_hass(hass)
    helper_entry = _add_mock_entry(hass)
    device_registry = dr.async_get(hass)
    source_device = device_registry.async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("homekit_controller", "source-device")},
    )
    shared_device = device_registry.async_get_or_create(
        config_entry_id=helper_entry.entry_id,
        identifiers=set(source_device.identifiers),
    )
    entity_registry = er.async_get(hass)
    helper_entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "mapped-helper",
        config_entry=helper_entry,
        device_id=shared_device.id,
        suggested_object_id="mapped_helper",
    )
    entity = Entity()

    link_entity_to_device(entity, hass, source_device.id)
    async_remove_cross_integration_device_ownership(
        hass,
        helper_entry.entry_id,
        (source_device.id,),
    )

    current_device = device_registry.async_get(source_device.id)
    current_entity = entity_registry.async_get(helper_entity.entity_id)
    assert entity.device_entry is not None
    assert entity.device_entry.id == source_device.id
    assert current_device is not None
    if hasattr(current_device, "config_entry_id"):
        assert current_device.config_entry_id == source_entry.entry_id
    else:
        assert set(current_device.config_entries) == {source_entry.entry_id}
    assert current_entity is not None
    assert current_entity.device_id == source_device.id


async def test_legacy_http_entry_fails_before_transport_and_creates_repair(
    hass: HomeAssistant,
) -> None:
    """Test an existing HTTP entry is blocked before its API key can be sent."""

    entry = _add_mock_entry(
        hass,
        data={
            CONF_API_KEY: "synthetic-key",
            CONF_API_BASE: "http://api.example.test/",
        },
    )

    with pytest.raises(ConfigEntryError) as raised:
        await async_setup_entry_impl(hass, entry)

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "invalid_api_base"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "insecure_api_base")


async def test_legacy_invalid_https_entry_fails_before_transport_and_creates_repair(
    hass: HomeAssistant,
) -> None:
    """Test an invalid HTTPS entry is blocked with an accurate setup error."""

    entry = _add_mock_entry(
        hass,
        data={
            CONF_API_KEY: "synthetic-key",
            CONF_API_BASE: "https://api.example.test/?mode=test",
        },
    )

    with pytest.raises(ConfigEntryError) as raised:
        await async_setup_entry_impl(hass, entry)

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "invalid_api_base"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "insecure_api_base")


async def test_mapped_entities_relink_across_source_registry_lifecycle(
    hass: HomeAssistant,
) -> None:
    """Test existing helpers follow source moves without config-entry recreation."""

    source_entry = MockConfigEntry(domain="homekit_controller")
    source_entry.add_to_hass(hass)
    helper_entry = _add_mock_entry(hass)
    helper_entry_id = helper_entry.entry_id
    device_registry = dr.async_get(hass)
    source_device_a = device_registry.async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("homekit_controller", "source-device-a")},
    )
    source_device_b = device_registry.async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("homekit_controller", "source-device-b")},
    )
    entity_registry = er.async_get(hass)
    source_entity = entity_registry.async_get_or_create(
        "climate",
        "homekit_controller",
        "source-climate",
        config_entry=source_entry,
        device_id=source_device_a.id,
        suggested_object_id="zone_a",
    )
    source_reference = {
        "registry_entry_id": source_entity.id,
        "domain": "climate",
        "platform": "homekit_controller",
        "unique_id": "source-climate",
    }
    hass.config_entries.async_update_entry(
        helper_entry,
        options={
            CONF_THERMOSTATS: [
                {
                    CONF_ID: 1001,
                    CONF_CLIMATE_ENTITY_ID: source_entity.entity_id,
                    CONF_CLIMATE_ENTITY_REF: source_reference,
                }
            ]
        },
    )
    helper_entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        thermostat_entity_unique_id(1001, "current_comfort_profile"),
        config_entry=helper_entry,
        device_id=None,
        suggested_object_id="zone_a_current_comfort_profile",
    )

    class CachedMappingCoordinator:
        def __init__(self) -> None:
            self.data: Any = types.SimpleNamespace(
                config=BeestatConfig(thermostats=(), sensors=())
            )
            self.source_enabled = False
            self.listeners: list[Callable[[], None]] = []

        def async_add_listener(
            self, listener: Callable[[], None]
        ) -> Callable[[], None]:
            self.listeners.append(listener)

            def remove_listener() -> None:
                self.listeners.remove(listener)

            return remove_listener

        def async_rebuild_runtime_from_cached_rows(self) -> None:
            current_entity_id = resolve_entity_reference(
                entity_registry,
                source_reference,
            )
            current_source = (
                entity_registry.async_get(current_entity_id)
                if current_entity_id is not None
                else None
            )
            self.data = types.SimpleNamespace(
                config=BeestatConfig(
                    thermostats=(
                        ConfiguredThermostat(
                            thermostat_id=1001,
                            slug="zone_a",
                            name="Zone A",
                            climate_entity_id=current_entity_id,
                            device_id=(
                                current_source.device_id
                                if current_source is not None
                                else None
                            ),
                        ),
                    )
                    if self.source_enabled
                    else (),
                    sensors=(),
                )
            )
            for listener in tuple(self.listeners):
                listener()

    coordinator = CachedMappingCoordinator()
    helper_entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    removers = _async_track_source_device_relinks(hass, helper_entry)

    coordinator.source_enabled = True
    coordinator.async_rebuild_runtime_from_cached_rows()
    assert (
        entity_registry.async_get(helper_entity.entity_id).device_id
        == source_device_a.id
    )

    original_registry_id = source_entity.id
    original_entity_id = source_entity.entity_id
    renamed_entity_id = "climate.zone_a_renamed"
    entity_registry.async_update_entity(
        original_entity_id,
        new_entity_id=renamed_entity_id,
    )
    await hass.async_block_till_done()
    assert (
        resolve_entity_reference(entity_registry, source_reference) == renamed_entity_id
    )
    assert helper_entry.options[CONF_THERMOSTATS][0][CONF_CLIMATE_ENTITY_ID] == (
        original_entity_id
    )
    assert (
        entity_registry.async_get(helper_entity.entity_id).device_id
        == source_device_a.id
    )

    entity_registry.async_update_entity(
        renamed_entity_id,
        device_id=source_device_b.id,
    )
    await hass.async_block_till_done()
    assert (
        entity_registry.async_get(helper_entity.entity_id).device_id
        == source_device_b.id
    )

    entity_registry.async_update_entity(renamed_entity_id, device_id=None)
    await hass.async_block_till_done()
    assert entity_registry.async_get(helper_entity.entity_id).device_id is None

    entity_registry.async_remove(renamed_entity_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get(helper_entity.entity_id).device_id is None

    entity_registry.async_get_or_create(
        "climate",
        "homekit_controller",
        "unrelated-source",
        config_entry=source_entry,
        device_id=source_device_a.id,
        suggested_object_id="zone_a_renamed",
    )
    restored_source = entity_registry.async_get_or_create(
        "climate",
        "homekit_controller",
        "source-climate",
        config_entry=source_entry,
        device_id=source_device_b.id,
        suggested_object_id="zone_a_renamed",
    )
    await hass.async_block_till_done()
    # Home Assistant preserves the registry entry UUID when the same source
    # identity is restored through the supported registry API.
    assert restored_source.id == original_registry_id
    assert restored_source.entity_id != original_entity_id
    assert resolve_entity_reference(entity_registry, source_reference) == (
        restored_source.entity_id
    )
    assert (
        entity_registry.async_get(helper_entity.entity_id).device_id
        == source_device_b.id
    )
    assert helper_entry.entry_id == helper_entry_id

    for remove_listener in removers:
        remove_listener()
    entity_registry.async_update_entity(
        restored_source.entity_id,
        device_id=source_device_a.id,
    )
    await hass.async_block_till_done()
    assert (
        entity_registry.async_get(helper_entity.entity_id).device_id
        == source_device_b.id
    )


async def test_mapping_repairs_follow_referenced_entity_registry_lifecycle(
    hass: HomeAssistant,
) -> None:
    """Test a removed mapping raises a Repair and recovery clears it promptly."""

    source_entry = MockConfigEntry(domain="homekit_controller")
    source_entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    source_entity = entity_registry.async_get_or_create(
        "sensor",
        "homekit_controller",
        "room-sensor-a-temperature",
        config_entry=source_entry,
        suggested_object_id="room_sensor_a_temperature",
    )
    source_reference = {
        "registry_entry_id": source_entity.id,
        "domain": "sensor",
        "platform": "homekit_controller",
        "unique_id": "room-sensor-a-temperature",
    }
    helper_entry = _add_mock_entry(
        hass,
        options={
            CONF_POINT_LOOKBACK_DAYS: 30,
            CONF_SCAN_INTERVAL_SECONDS: 900,
            CONF_SENSORS: [
                {
                    CONF_ID: 2002,
                    CONF_TEMPERATURE_ENTITY_ID: source_entity.entity_id,
                    CONF_TEMPERATURE_ENTITY_REF: source_reference,
                }
            ],
        },
    )
    issue_registry = ir.async_get(hass)

    _async_update_override_issues(hass, helper_entry)
    _async_track_override_issue_updates(hass, helper_entry)
    assert issue_registry.async_get_issue(DOMAIN, "missing_override_entities") is None

    original_entity_id = source_entity.entity_id
    renamed_entity_id = "sensor.room_sensor_a_temperature_renamed"
    entity_registry.async_update_entity(
        original_entity_id,
        new_entity_id=renamed_entity_id,
    )
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(DOMAIN, "missing_override_entities") is None
    assert helper_entry.options[CONF_SENSORS][0][CONF_TEMPERATURE_ENTITY_ID] == (
        original_entity_id
    )

    entity_registry.async_remove(renamed_entity_id)
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(DOMAIN, "missing_override_entities")

    entity_registry.async_get_or_create(
        "sensor",
        "homekit_controller",
        "unrelated-room-temperature",
        config_entry=source_entry,
        suggested_object_id="room_sensor_a_temperature",
    )
    restored_entity = entity_registry.async_get_or_create(
        "sensor",
        "homekit_controller",
        "room-sensor-a-temperature",
        config_entry=source_entry,
        suggested_object_id="room_sensor_a_temperature",
    )
    await hass.async_block_till_done()
    # Removal and restoration retain the stable registry UUID for this source.
    assert restored_entity.id == source_entity.id
    assert restored_entity.entity_id != source_entity.entity_id
    assert issue_registry.async_get_issue(DOMAIN, "missing_override_entities") is None


@pytest.fixture(autouse=True)
def _skip_dependency_setup_for_config_flow_tests():
    """Keep config-flow tests focused on flow behavior, not integration setup."""

    with (
        patch(
            "homeassistant.config_entries.async_process_deps_reqs",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.beestat_statistics.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        yield


async def test_user_flow_creates_config_entry(hass: HomeAssistant) -> None:
    """Test the successful user setup flow."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert {key.schema for key in result["data_schema"].schema} == {
        CONF_API_KEY,
        CONF_API_BASE,
    }

    with _mock_validate_input():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == CONFIG_TITLE
    assert result["data"] == {
        CONF_API_KEY: "test-api-key",
        CONF_API_BASE: API_BASE,
        CONF_ACCOUNT_FINGERPRINT: ACCOUNT_A,
    }
    assert result["options"] == {
        CONF_POINT_LOOKBACK_DAYS: DEFAULT_POINT_LOOKBACK_DAYS,
        CONF_SCAN_INTERVAL_SECONDS: DEFAULT_SCAN_INTERVAL_SECONDS,
    }


async def test_migrate_entry_preserves_legacy_scope_and_moves_timing(
    hass: HomeAssistant,
) -> None:
    """Test legacy entries retain source scope through versioned migration."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=CONFIG_TITLE,
        unique_id=CONFIG_ENTRY_UNIQUE_ID,
        version=1,
        minor_version=1,
        data={
            CONF_API_KEY: "synthetic-key",
            CONF_API_BASE: API_BASE,
            CONF_POINT_LOOKBACK_DAYS: 45,
            CONF_SCAN_INTERVAL_SECONDS: 600,
            CONF_THERMOSTATS: [
                {CONF_ID: 1001, "enabled": False, "slug": "zone_a"},
            ],
        },
        options={},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == CONFIG_ENTRY_VERSION
    assert entry.minor_version == CONFIG_ENTRY_MINOR_VERSION
    assert entry.data[CONF_THERMOSTATS] == [
        {CONF_ID: 1001, "enabled": False, "slug": "zone_a"},
    ]
    assert CONF_POINT_LOOKBACK_DAYS not in entry.data
    assert CONF_SCAN_INTERVAL_SECONDS not in entry.data
    assert entry.options[CONF_POINT_LOOKBACK_DAYS] == 45
    assert entry.options[CONF_SCAN_INTERVAL_SECONDS] == 600


async def test_migrate_entry_backfills_stable_refs_for_options_only(
    hass: HomeAssistant,
) -> None:
    """Test UI mappings gain stable refs without rewriting YAML-owned data."""

    source_entry = MockConfigEntry(domain="homekit_controller")
    source_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    source = registry.async_get_or_create(
        "climate",
        "homekit_controller",
        "source-climate",
        config_entry=source_entry,
        suggested_object_id="zone_a",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=CONFIG_TITLE,
        unique_id=CONFIG_ENTRY_UNIQUE_ID,
        version=CONFIG_ENTRY_VERSION,
        minor_version=4,
        data={
            CONF_API_KEY: "synthetic-key",
            CONF_API_BASE: API_BASE,
            CONF_THERMOSTATS: [
                {CONF_ID: 1002, CONF_CLIMATE_ENTITY_ID: "climate.yaml_zone"}
            ],
        },
        options={
            CONF_THERMOSTATS: [
                {CONF_ID: 1001, CONF_CLIMATE_ENTITY_ID: source.entity_id}
            ]
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert CONF_CLIMATE_ENTITY_REF not in entry.data[CONF_THERMOSTATS][0]
    assert entry.options[CONF_THERMOSTATS][0][CONF_CLIMATE_ENTITY_REF] == {
        "registry_entry_id": source.id,
        "domain": "climate",
        "platform": "homekit_controller",
        "unique_id": "source-climate",
    }


async def test_user_flow_normalizes_copy_paste_whitespace(
    hass: HomeAssistant,
) -> None:
    """Test copied Beestat connection fields are stripped before validation/storage."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    user_input = USER_INPUT | {
        CONF_API_KEY: " test-api-key \n",
        CONF_API_BASE: f" {API_BASE} ",
    }
    with _mock_validate_input() as validate:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input,
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    validate.assert_awaited_once()
    validated_input = validate.await_args.args[1]
    assert validated_input[CONF_API_KEY] == "test-api-key"
    assert validated_input[CONF_API_BASE] == API_BASE
    assert "\n" not in validated_input[CONF_API_KEY]
    assert result["data"][CONF_API_KEY] == "test-api-key"
    assert result["data"][CONF_API_BASE] == API_BASE


async def test_user_flow_requires_identifiable_account_anchor(
    hass: HomeAssistant,
) -> None:
    """Test setup cannot create an entry when account identity is unproven."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    with _mock_validate_input(return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "account_identity_unavailable"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_user_flow_rejects_insecure_api_base_before_validation(
    hass: HomeAssistant,
) -> None:
    """Test an API key cannot be validated against a plaintext endpoint."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    with _mock_validate_input() as validate:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "test-api-key",
                CONF_API_BASE: "http://api.example.test/",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_API_BASE: "invalid_api_base"}
    validate.assert_not_awaited()


async def test_user_flow_recovers_from_auth_error(hass: HomeAssistant) -> None:
    """Test the user can recover after Beestat rejects a key."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    with _mock_validate_input(side_effect=BeestatAuthError("invalid key")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    with _mock_validate_input():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT | {CONF_API_KEY: "fixed-key"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] == "fixed-key"


async def test_user_flow_recovers_from_unexpected_error(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test the user can recover after an unexpected validation exception."""

    secret = "private-validation-detail"

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    with _mock_validate_input(side_effect=RuntimeError(secret)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text

    with _mock_validate_input():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT | {CONF_API_KEY: "fixed-key"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] == "fixed-key"


async def test_user_flow_rejects_blank_api_key(hass: HomeAssistant) -> None:
    """Test blank credentials fail before network validation."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    with _mock_validate_input() as validate:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT | {CONF_API_KEY: ""},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_API_KEY: "api_key_required"}
    validate.assert_not_awaited()


async def test_reauth_preserves_entry_when_account_identity_is_unavailable(
    hass: HomeAssistant,
) -> None:
    """Test reauth cannot carry a stale fingerprint onto an unproven account."""

    entry = _add_mock_entry(hass)
    original_data = dict(entry.data)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    with _mock_validate_input(return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "replacement-key",
                CONF_API_BASE: API_BASE,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "account_identity_unavailable"}
    assert dict(entry.data) == original_data


async def test_user_flow_rejects_duplicate_entry(hass: HomeAssistant) -> None:
    """Test the integration remains single-entry."""

    _add_mock_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        USER_INPUT,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_import_flow_creates_config_entry(hass: HomeAssistant) -> None:
    """Test YAML import creates a config entry with options split out."""

    with _mock_validate_input():
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_API_KEY: "yaml-key",
                CONF_API_BASE: API_BASE,
                CONF_POINT_LOOKBACK_DAYS: 75,
                CONF_SCAN_INTERVAL_SECONDS: 3600,
                CONF_THERMOSTATS: [
                    {
                        "id": 1001,
                        CONF_CLIMATE_ENTITY_ID: "climate.zone_a",
                    }
                ],
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == CONFIG_TITLE
    assert result["data"] == {
        CONF_API_KEY: "yaml-key",
        CONF_API_BASE: API_BASE,
        CONF_ACCOUNT_FINGERPRINT: ACCOUNT_A,
        CONF_THERMOSTATS: [
            {
                "id": 1001,
                CONF_CLIMATE_ENTITY_ID: "climate.zone_a",
            }
        ],
    }
    assert result["options"] == {
        CONF_POINT_LOOKBACK_DAYS: 75,
        CONF_SCAN_INTERVAL_SECONDS: 3600,
    }


async def test_yaml_schema_rejects_whitespace_only_api_key() -> None:
    """Test YAML cannot normalize a present API key into an empty secret."""

    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA({DOMAIN: {CONF_API_KEY: "   "}})


async def test_yaml_schema_rejects_insecure_api_base() -> None:
    """Test YAML cannot configure credential transport over plaintext HTTP."""

    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(
            {
                DOMAIN: {
                    CONF_API_KEY: "synthetic-key",
                    CONF_API_BASE: "http://api.example.test/",
                }
            }
        )


async def test_initial_import_requires_identifiable_account_anchor(
    hass: HomeAssistant,
) -> None:
    """Test YAML import cannot create an account with no stable thermostat anchor."""

    with _mock_validate_input(return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_API_KEY: "yaml-key",
                CONF_API_BASE: API_BASE,
                CONF_POINT_LOOKBACK_DAYS: 75,
                CONF_SCAN_INTERVAL_SECONDS: 3600,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "account_identity_unavailable"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_import_flow_updates_existing_entry(hass: HomeAssistant) -> None:
    """Test YAML import updates the single existing config entry."""

    entry = _add_mock_entry(hass)
    async_set_yaml_connection_change_issue(hass, active=True)
    with _mock_validate_input() as validate:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_API_KEY: "yaml-key",
                CONF_API_BASE: "https://api.example.test/",
                CONF_POINT_LOOKBACK_DAYS: 90,
                CONF_SCAN_INTERVAL_SECONDS: 1800,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    validate.assert_awaited_once()
    assert dict(entry.data) == {
        CONF_API_KEY: "yaml-key",
        CONF_API_BASE: "https://api.example.test/",
        CONF_ACCOUNT_FINGERPRINT: ACCOUNT_A,
    }
    assert dict(entry.options) == {
        CONF_POINT_LOOKBACK_DAYS: 90,
        CONF_SCAN_INTERVAL_SECONDS: 1800,
    }
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN,
            YAML_CONNECTION_CHANGE_ISSUE_ID,
        )
        is None
    )


async def test_same_connection_yaml_import_backfills_missing_fingerprint(
    hass: HomeAssistant,
) -> None:
    """Test a legacy entry proves continuity before gaining a fingerprint."""

    entry = _add_mock_entry(
        hass,
        data={
            CONF_API_KEY: "yaml-key",
            CONF_API_BASE: API_BASE,
        },
    )
    with _mock_validate_input() as validate:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_API_KEY: "yaml-key",
                CONF_API_BASE: API_BASE,
                CONF_POINT_LOOKBACK_DAYS: 75,
                CONF_SCAN_INTERVAL_SECONDS: 3600,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    validate.assert_awaited_once()
    assert entry.data[CONF_ACCOUNT_FINGERPRINT] == ACCOUNT_A


async def test_import_flow_preserves_ui_mapping_options(
    hass: HomeAssistant,
) -> None:
    """Test YAML import preserves UI-owned mappings when YAML omits them."""

    entry = _add_mock_entry(
        hass,
        options={
            CONF_POINT_LOOKBACK_DAYS: 30,
            CONF_SCAN_INTERVAL_SECONDS: 900,
            CONF_THERMOSTATS: [{CONF_ID: 1001, CONF_FILTER_CHANGED_DATE: "2026-07-05"}],
        },
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_API_KEY: "old-key",
            CONF_API_BASE: API_BASE,
            CONF_POINT_LOOKBACK_DAYS: 90,
            CONF_SCAN_INTERVAL_SECONDS: 1800,
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert dict(entry.data) == {
        CONF_API_KEY: "old-key",
        CONF_API_BASE: API_BASE,
        CONF_ACCOUNT_FINGERPRINT: ACCOUNT_A,
    }
    assert dict(entry.options) == {
        CONF_POINT_LOOKBACK_DAYS: 90,
        CONF_SCAN_INTERVAL_SECONDS: 1800,
        CONF_THERMOSTATS: [{CONF_ID: 1001, CONF_FILTER_CHANGED_DATE: "2026-07-05"}],
    }


async def test_import_flow_preserves_button_boundary_with_yaml_mapping(
    hass: HomeAssistant,
) -> None:
    """Test YAML mappings retain the native filter click boundary."""

    entry = _add_mock_entry(
        hass,
        options={
            CONF_THERMOSTATS: [
                {
                    CONF_ID: 1001,
                    CONF_FILTER_CHANGED_DATE: "2026-07-05",
                    CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS: 28800,
                }
            ],
        },
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_API_KEY: "old-key",
            CONF_API_BASE: API_BASE,
            CONF_POINT_LOOKBACK_DAYS: 90,
            CONF_SCAN_INTERVAL_SECONDS: 1800,
            CONF_THERMOSTATS: [
                {CONF_ID: 1001, CONF_CLIMATE_ENTITY_ID: "climate.zone_a"}
            ],
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert dict(entry.options) == {
        CONF_POINT_LOOKBACK_DAYS: 90,
        CONF_SCAN_INTERVAL_SECONDS: 1800,
        CONF_THERMOSTATS: [
            {
                CONF_ID: 1001,
                CONF_CLIMATE_ENTITY_ID: "climate.zone_a",
                CONF_FILTER_CHANGED_DATE: "2026-07-05",
                CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS: 28800,
            }
        ],
    }


async def test_import_flow_blocks_yaml_account_change(hass: HomeAssistant) -> None:
    """Test YAML cannot silently reinterpret saved mappings for another account."""

    entry = _add_mock_entry(
        hass,
        options={
            CONF_POINT_LOOKBACK_DAYS: 30,
            CONF_SCAN_INTERVAL_SECONDS: 900,
            CONF_THERMOSTATS: [
                {CONF_ID: 1001, CONF_CLIMATE_ENTITY_ID: "climate.zone_a"}
            ],
        },
    )
    original_data = dict(entry.data)
    original_options = dict(entry.options)

    with _mock_validate_input(return_value=ACCOUNT_B):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_API_KEY: "other-account-key",
                CONF_API_BASE: API_BASE,
                CONF_POINT_LOOKBACK_DAYS: 90,
                CONF_SCAN_INTERVAL_SECONDS: 1800,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "yaml_connection_change_requires_reconfigure"
    assert dict(entry.data) == original_data
    assert dict(entry.options) == original_options
    assert ir.async_get(hass).async_get_issue(
        DOMAIN,
        YAML_CONNECTION_CHANGE_ISSUE_ID,
    )


async def test_import_flow_blocks_unvalidated_yaml_connection_change(
    hass: HomeAssistant,
) -> None:
    """Test unavailable YAML credentials do not replace a working connection."""

    entry = _add_mock_entry(hass)
    original_data = dict(entry.data)

    with _mock_validate_input(side_effect=BeestatApiError("synthetic failure")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_API_KEY: "unvalidated-key",
                CONF_API_BASE: API_BASE,
                CONF_POINT_LOOKBACK_DAYS: 90,
                CONF_SCAN_INTERVAL_SECONDS: 1800,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "yaml_connection_change_requires_reconfigure"
    assert dict(entry.data) == original_data
    assert ir.async_get(hass).async_get_issue(
        DOMAIN,
        YAML_CONNECTION_CHANGE_ISSUE_ID,
    )


async def test_setup_clears_stale_yaml_connection_issue_without_yaml(
    hass: HomeAssistant,
) -> None:
    """Test removing YAML clears its no-longer-actionable Repair."""

    async_set_yaml_connection_change_issue(hass, active=True)

    assert await async_setup(hass, {})
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN,
            YAML_CONNECTION_CHANGE_ISSUE_ID,
        )
        is None
    )


async def test_reauth_flow_updates_api_key(hass: HomeAssistant) -> None:
    """Test reauth updates the existing entry without creating another."""

    entry = _add_mock_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with _mock_validate_input(side_effect=BeestatAuthError("invalid key")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "bad-key",
                CONF_API_BASE: API_BASE,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    with _mock_validate_input():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "replacement-key",
                CONF_API_BASE: API_BASE,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "replacement-key"
    assert entry.data[CONF_ACCOUNT_FINGERPRINT] == ACCOUNT_A


async def test_reauth_flow_confirms_different_account(
    hass: HomeAssistant,
) -> None:
    """Test reauth requires explicit confirmation before switching accounts."""

    entry = _add_mock_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )

    with _mock_validate_input(return_value=ACCOUNT_B):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "different-account-key",
                CONF_API_BASE: API_BASE,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account_change_confirm"
    assert entry.data[CONF_API_KEY] == "old-key"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "different-account-key"
    assert entry.data[CONF_ACCOUNT_FINGERPRINT] == ACCOUNT_B


async def test_reauth_flow_recovers_from_unexpected_error(
    hass: HomeAssistant,
) -> None:
    """Test reauth can recover after an unexpected validation exception."""

    entry = _add_mock_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )

    with _mock_validate_input(side_effect=RuntimeError("boom")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "replacement-key",
                CONF_API_BASE: API_BASE,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}

    with _mock_validate_input():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "replacement-key",
                CONF_API_BASE: API_BASE,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "replacement-key"


async def test_reauth_flow_rejects_blank_api_key(hass: HomeAssistant) -> None:
    """Test reauth requires a replacement key."""

    entry = _add_mock_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )

    with _mock_validate_input() as validate:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "",
                CONF_API_BASE: API_BASE,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_API_KEY: "api_key_required"}
    validate.assert_not_awaited()


async def test_reconfigure_flow_allows_blank_key_to_keep_current(
    hass: HomeAssistant,
) -> None:
    """Test reconfigure can update connection data without retyping the key."""

    entry = _add_mock_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with _mock_validate_input(side_effect=BeestatApiError("offline")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "",
                CONF_API_BASE: "https://offline.example.test/",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    with _mock_validate_input():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "",
                CONF_API_BASE: "https://api.example.test/",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_API_KEY] == "old-key"
    assert entry.data[CONF_API_BASE] == "https://api.example.test/"
    assert entry.data[CONF_ACCOUNT_FINGERPRINT] == ACCOUNT_A


async def test_reconfigure_preserves_entry_when_account_identity_is_unavailable(
    hass: HomeAssistant,
) -> None:
    """Test reconfigure fails closed when continuity cannot be proven."""

    entry = _add_mock_entry(hass)
    original_data = dict(entry.data)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    with _mock_validate_input(return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "different-key",
                CONF_API_BASE: "https://api.example.test/",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "account_identity_unavailable"}
    assert dict(entry.data) == original_data


async def test_reconfigure_flow_confirms_different_account(
    hass: HomeAssistant,
) -> None:
    """Test reconfigure requires explicit confirmation before account replacement."""

    entry = _add_mock_entry(
        hass,
        data={
            CONF_API_KEY: "old-key",
            CONF_API_BASE: API_BASE,
            CONF_ACCOUNT_FINGERPRINT: ACCOUNT_A,
            CONF_THERMOSTATS: [{CONF_ID: 1001, "slug": "zone_a"}],
        },
        options={
            CONF_POINT_LOOKBACK_DAYS: 30,
            CONF_SCAN_INTERVAL_SECONDS: 900,
            CONF_SENSORS: [
                {
                    CONF_ID: 2001,
                    CONF_TEMPERATURE_ENTITY_ID: ("sensor.room_sensor_a_temperature"),
                }
            ],
        },
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )

    with _mock_validate_input(return_value=ACCOUNT_B):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "different-account-key",
                CONF_API_BASE: API_BASE,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account_change_confirm"
    assert entry.data[CONF_API_KEY] == "old-key"
    assert CONF_THERMOSTATS in entry.data
    assert CONF_SENSORS in entry.options

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_API_KEY] == "different-account-key"
    assert entry.data[CONF_ACCOUNT_FINGERPRINT] == ACCOUNT_B
    assert CONF_THERMOSTATS not in entry.data
    assert CONF_SENSORS not in entry.options
    assert entry.options == {
        CONF_POINT_LOOKBACK_DAYS: 30,
        CONF_SCAN_INTERVAL_SECONDS: 900,
    }


async def test_reconfigure_flow_recovers_from_unexpected_error(
    hass: HomeAssistant,
) -> None:
    """Test reconfigure can recover after an unexpected validation exception."""

    entry = _add_mock_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )

    with _mock_validate_input(side_effect=RuntimeError("boom")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "",
                CONF_API_BASE: "https://api.example.test/",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}

    with _mock_validate_input():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_API_KEY: "",
                CONF_API_BASE: "https://api.example.test/",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_API_BASE] == "https://api.example.test/"


async def test_options_flow_updates_import_options(hass: HomeAssistant) -> None:
    """Test the options flow stores user-tunable import settings."""

    entry = _add_mock_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == {
        "timing": "Import timing",
        "source_scope": "Choose Beestat sources",
        "thermostat_mapping": "Map a thermostat",
        "sensor_mapping": "Map a room sensor",
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "timing"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "timing"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POINT_LOOKBACK_DAYS: 60,
            CONF_SCAN_INTERVAL_SECONDS: 1200,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_POINT_LOOKBACK_DAYS: 60,
        CONF_SCAN_INTERVAL_SECONDS: 1200,
    }


async def test_options_flow_confirms_scope_removal_and_preserves_other_options(
    hass: HomeAssistant,
) -> None:
    """Test source selection uses raw discovery and preserves unrelated options."""

    entry = _add_mock_entry(
        hass,
        options={
            CONF_POINT_LOOKBACK_DAYS: 30,
            CONF_SCAN_INTERVAL_SECONDS: 900,
            CONF_THERMOSTATS: [
                {CONF_ID: 1001, CONF_CLIMATE_ENTITY_ID: "climate.zone_a"},
                {CONF_ID: 1002, "enabled": False},
            ],
            CONF_SENSORS: [
                {
                    CONF_ID: 2001,
                    CONF_TEMPERATURE_ENTITY_ID: "sensor.room_sensor_a_temperature",
                }
            ],
        },
    )
    entry.runtime_data = _runtime_data(
        thermostats=[
            _configured_thermostat(
                thermostat_id=1001,
                name="Zone A",
                slug="zone_a",
            )
        ],
        sensors=[
            _configured_sensor(
                sensor_id=2001,
                name="Room Sensor A",
                slug="room_sensor_a",
            )
        ],
        thermostat_rows=[
            {"id": 1001, "name": "Zone A"},
            {"id": 1002, "name": "Zone B", "inactive": True},
        ],
        sensor_rows=[
            {"id": 2001, "name": "Room Sensor A"},
            {"id": 2002, "name": "Room Sensor B"},
        ],
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "source_scope"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_scope"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "included_thermostat_ids": ["1002"],
            "included_sensor_ids": ["2001", "2002"],
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_scope_confirm"
    assert result["description_placeholders"] == {
        "thermostat_count": "1",
        "sensor_count": "0",
    }
    assert entry.options[CONF_THERMOSTATS] == [
        {CONF_ID: 1001, CONF_CLIMATE_ENTITY_ID: "climate.zone_a"},
        {CONF_ID: 1002, "enabled": False},
    ]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POINT_LOOKBACK_DAYS] == 30
    assert result["data"][CONF_SCAN_INTERVAL_SECONDS] == 900
    assert result["data"][CONF_THERMOSTATS] == [
        {
            CONF_ID: 1001,
            CONF_CLIMATE_ENTITY_ID: "climate.zone_a",
            "enabled": False,
        },
        {CONF_ID: 1002, "enabled": True},
    ]
    assert result["data"][CONF_SENSORS] == [
        {
            CONF_ID: 2001,
            CONF_TEMPERATURE_ENTITY_ID: "sensor.room_sensor_a_temperature",
        }
    ]
    assert entry.options == result["data"]


async def test_get_configuration_service_returns_exact_non_secret_response(
    hass: HomeAssistant,
) -> None:
    """Test the response-only service exposes effective local configuration."""

    entry = _add_mock_entry(
        hass,
        options={
            CONF_POINT_LOOKBACK_DAYS: 30,
            CONF_SCAN_INTERVAL_SECONDS: 900,
            CONF_THERMOSTATS: [
                {CONF_ID: 1001, CONF_CLIMATE_ENTITY_ID: "climate.zone_a"}
            ],
        },
    )
    entry.runtime_data = _runtime_data(
        thermostats=[
            _configured_thermostat(
                thermostat_id=1001,
                name="Zone A",
                slug="zone_a",
                climate_entity_id="climate.zone_a",
            )
        ],
        sensors=[],
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup(hass, {})

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_CONFIGURATION,
        {ATTR_CONFIG_ENTRY_ID: entry.entry_id},
        blocking=True,
        return_response=True,
    )

    assert response["timing"] == {
        "point_lookback_days": 30,
        "scan_interval_seconds": 900,
    }
    assert response["saved_overrides"]["thermostats"] == {
        "source": "options",
        "items": [{CONF_ID: 1001, CONF_CLIMATE_ENTITY_ID: "climate.zone_a"}],
    }
    assert (
        response["effective_configuration"]["thermostats"][0][CONF_CLIMATE_ENTITY_ID]
        == "climate.zone_a"
    )
    assert (
        response["effective_configuration"]["thermostats"][0][
            "filter_change_day_runtime_baseline_seconds"
        ]
        is None
    )
    assert "api_key" not in repr(response)


async def test_repair_filter_change_boundary_service_uses_verified_timestamp(
    hass: HomeAssistant,
) -> None:
    entry = _add_mock_entry(hass)
    local_tz = ZoneInfo("America/New_York")
    repair_at = (datetime.now(UTC) - timedelta(days=1)).astimezone(local_tz)
    repair_at = repair_at.replace(microsecond=0)
    thermostat = _configured_thermostat(
        thermostat_id=1001,
        name="Zone A",
        slug="zone_a",
        filter_changed_date=repair_at.date(),
    )
    coordinator = types.SimpleNamespace(
        data=types.SimpleNamespace(
            config=types.SimpleNamespace(thermostats=(thermostat,))
        ),
        local_tz=local_tz,
    )
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup(hass, {})

    changed_at = repair_at.replace(tzinfo=None).isoformat()
    with patch(
        "custom_components.beestat_statistics.async_mark_filter_changed",
        new_callable=AsyncMock,
    ) as mark_changed:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REPAIR_FILTER_CHANGE_BOUNDARY,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                "thermostat_id": 1001,
                ATTR_CHANGED_AT: changed_at,
            },
            blocking=True,
        )

    mark_changed.assert_awaited_once()
    assert mark_changed.await_args.args[1] == 1001
    assert mark_changed.await_args.args[2] == repair_at.astimezone(UTC)
    assert mark_changed.await_args.kwargs == {"dismiss_alerts": False}


@pytest.mark.parametrize(
    ("changed_at", "evaluated_at"),
    [
        ("2026-03-08T02:30:00", datetime(2026, 3, 15, tzinfo=UTC)),
        ("2026-11-01T01:30:00", datetime(2026, 11, 8, tzinfo=UTC)),
    ],
)
async def test_repair_filter_change_boundary_rejects_inexact_local_wall_time(
    hass: HomeAssistant,
    changed_at: str,
    evaluated_at: datetime,
) -> None:
    entry = _add_mock_entry(hass)
    local_tz = ZoneInfo("America/New_York")
    thermostat = _configured_thermostat(
        thermostat_id=1001,
        name="Zone A",
        slug="zone_a",
        filter_changed_date=datetime.fromisoformat(changed_at).date(),
    )
    coordinator = types.SimpleNamespace(
        data=types.SimpleNamespace(
            config=types.SimpleNamespace(thermostats=(thermostat,))
        ),
        local_tz=local_tz,
    )
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup(hass, {})

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return evaluated_at.replace(tzinfo=None)
            return evaluated_at.astimezone(tz)

    with (
        patch("custom_components.beestat_statistics.datetime", FrozenDateTime),
        patch(
            "custom_components.beestat_statistics.async_mark_filter_changed",
            new_callable=AsyncMock,
        ) as mark_changed,
        pytest.raises(ServiceValidationError) as raised,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REPAIR_FILTER_CHANGE_BOUNDARY,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                "thermostat_id": 1001,
                ATTR_CHANGED_AT: changed_at,
            },
            blocking=True,
        )

    assert raised.value.translation_key == "filter_change_boundary_local_time_invalid"
    mark_changed.assert_not_awaited()


async def test_native_filter_button_forwards_exact_aware_click_time(
    hass: HomeAssistant,
) -> None:
    thermostat = _configured_thermostat(
        thermostat_id=1001,
        name="Zone A",
        slug="zone_a",
    )
    coordinator = types.SimpleNamespace(
        hass=hass,
        data=types.SimpleNamespace(
            config=types.SimpleNamespace(thermostats=(thermostat,))
        ),
    )
    entity = BeestatFilterChangedButton(coordinator, thermostat)
    changed_at = datetime.fromisoformat("2026-07-05T21:48:00+00:00")

    with (
        patch(
            "custom_components.beestat_statistics.button.dt_util.now",
            return_value=changed_at,
        ),
        patch(
            "custom_components.beestat_statistics.button.async_mark_filter_changed",
            new_callable=AsyncMock,
        ) as mark_changed,
    ):
        await entity.async_press()

    mark_changed.assert_awaited_once_with(coordinator, 1001, changed_at)


async def test_native_filter_date_exposes_and_updates_click_boundary(
    hass: HomeAssistant,
) -> None:
    changed_at = datetime.fromisoformat("2026-07-05T21:48:00+00:00")
    reconciled_at = datetime.fromisoformat("2026-07-06T06:05:00+00:00")
    source_data_end = datetime.fromisoformat("2026-07-06T04:00:00+00:00")
    thermostat = _configured_thermostat(
        thermostat_id=1001,
        name="Zone A",
        slug="zone_a",
        filter_changed_date=changed_at.date(),
        filter_changed_at=changed_at,
        filter_change_day_runtime_baseline_seconds=7200.0,
        filter_change_boundary_reconciled_at=reconciled_at,
        filter_change_boundary_source_data_end=source_data_end,
    )
    summary = types.SimpleNamespace(
        filter_changed_date=changed_at.date(),
        filter_changed_source="home_assistant_override",
    )
    coordinator = types.SimpleNamespace(
        hass=hass,
        last_update_success=True,
        data=types.SimpleNamespace(
            config=types.SimpleNamespace(thermostats=(thermostat,)),
            thermostats={1001: summary},
        ),
    )
    entity = BeestatFilterChangedDate(coordinator, thermostat)

    assert entity.available
    assert entity.native_value == changed_at.date()
    assert entity.extra_state_attributes == {
        "source": "home_assistant_override",
        "home_assistant_override_date": "2026-07-05",
        "filter_changed_at": "2026-07-05T21:48:00+00:00",
        "boundary_status": "finalized",
        "change_day_runtime_baseline_seconds": 7200.0,
        "boundary_reconciled_at": "2026-07-06T06:05:00+00:00",
        "boundary_source_data_end": "2026-07-06T04:00:00+00:00",
        "boundary_precision_minutes": 5,
        "legacy_helper_entity_id": None,
    }

    new_date = date(2026, 7, 7)
    with patch(
        "custom_components.beestat_statistics.date.async_set_filter_changed_date",
        new_callable=AsyncMock,
    ) as set_date:
        await entity.async_set_value(new_date)

    set_date.assert_awaited_once_with(coordinator, 1001, new_date)

    coordinator.data.config = types.SimpleNamespace(thermostats=())
    assert not entity.available
    assert entity.native_value is None
    assert entity.extra_state_attributes is None


async def test_options_flow_updates_thermostat_mapping(hass: HomeAssistant) -> None:
    """Test the options flow stores native thermostat mapping overrides."""

    source_entry = MockConfigEntry(domain="homekit_controller")
    source_entry.add_to_hass(hass)
    source = er.async_get(hass).async_get_or_create(
        "climate",
        "homekit_controller",
        "source-climate",
        config_entry=source_entry,
        suggested_object_id="zone_a",
    )
    entry = _add_mock_entry(hass)
    entry.runtime_data = _runtime_data(
        thermostats=[
            _configured_thermostat(
                thermostat_id=1001,
                name="Zone A",
                slug="zone_a",
            )
        ],
        sensors=[],
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "thermostat_mapping"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "thermostat_mapping"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ID: "1001"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "thermostat_mapping_detail"
    assert result["description_placeholders"] == {
        "item": "Zone A (1001)",
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CLIMATE_ENTITY_ID: source.entity_id},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_THERMOSTATS] == [
        {
            CONF_ID: 1001,
            CONF_CLIMATE_ENTITY_ID: source.entity_id,
            CONF_CLIMATE_ENTITY_REF: {
                "registry_entry_id": source.id,
                "domain": "climate",
                "platform": "homekit_controller",
                "unique_id": "source-climate",
            },
        }
    ]


async def test_options_flow_updates_room_sensor_mapping(hass: HomeAssistant) -> None:
    """Test the options flow identifies the selected room-sensor override."""

    source_entry = MockConfigEntry(domain="homekit_controller")
    source_entry.add_to_hass(hass)
    source = er.async_get(hass).async_get_or_create(
        "sensor",
        "homekit_controller",
        "room-sensor-b-temperature",
        config_entry=source_entry,
        suggested_object_id="room_sensor_b_temperature",
    )
    entry = _add_mock_entry(hass)
    entry.runtime_data = _runtime_data(
        thermostats=[],
        sensors=[
            _configured_sensor(
                sensor_id=2002,
                name="Room Sensor B",
                slug="room_sensor_b",
            )
        ],
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "sensor_mapping"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sensor_mapping"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ID: "2002"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sensor_mapping_detail"
    assert result["description_placeholders"] == {
        "item": "Room Sensor B (2002)",
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_TEMPERATURE_ENTITY_ID: source.entity_id},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SENSORS] == [
        {
            CONF_ID: 2002,
            CONF_TEMPERATURE_ENTITY_ID: source.entity_id,
            CONF_TEMPERATURE_ENTITY_REF: {
                "registry_entry_id": source.id,
                "domain": "sensor",
                "platform": "homekit_controller",
                "unique_id": "room-sensor-b-temperature",
            },
        }
    ]


async def test_options_flow_resolves_renamed_thermostat_mapping_default(
    hass: HomeAssistant,
) -> None:
    """Test a stable thermostat reference supplies the current entity ID."""

    source_entry = MockConfigEntry(domain="homekit_controller")
    source_entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    source = entity_registry.async_get_or_create(
        "climate",
        "homekit_controller",
        "source-climate",
        config_entry=source_entry,
        suggested_object_id="zone_a",
    )
    original_entity_id = source.entity_id
    renamed_entity_id = "climate.zone_a_renamed"
    source_reference = {
        "registry_entry_id": source.id,
        "domain": "climate",
        "platform": "homekit_controller",
        "unique_id": "source-climate",
    }
    entry = _add_mock_entry(
        hass,
        options={
            CONF_POINT_LOOKBACK_DAYS: 30,
            CONF_SCAN_INTERVAL_SECONDS: 900,
            CONF_THERMOSTATS: [
                {
                    CONF_ID: 1001,
                    CONF_CLIMATE_ENTITY_ID: original_entity_id,
                    CONF_CLIMATE_ENTITY_REF: source_reference,
                }
            ],
        },
    )
    entry.runtime_data = _runtime_data(
        thermostats=[
            _configured_thermostat(
                thermostat_id=1001,
                name="Zone A",
                slug="zone_a",
            )
        ],
        sensors=[],
    )
    entity_registry.async_update_entity(
        original_entity_id,
        new_entity_id=renamed_entity_id,
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "thermostat_mapping"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ID: "1001"},
    )

    assert result["type"] is FlowResultType.FORM
    assert _suggested_values(result)[CONF_CLIMATE_ENTITY_ID] == renamed_entity_id
    assert entry.options[CONF_THERMOSTATS][0][CONF_CLIMATE_ENTITY_ID] == (
        original_entity_id
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CLIMATE_ENTITY_ID: renamed_entity_id},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_THERMOSTATS][0][CONF_CLIMATE_ENTITY_ID] == (
        renamed_entity_id
    )
    assert result["data"][CONF_THERMOSTATS][0][CONF_CLIMATE_ENTITY_REF] == (
        source_reference
    )


async def test_options_flow_recovers_temporarily_missing_room_sensor_default(
    hass: HomeAssistant,
) -> None:
    """Test a missing stable source is blank and resolves after restoration."""

    source_entry = MockConfigEntry(domain="homekit_controller")
    source_entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    source = entity_registry.async_get_or_create(
        "sensor",
        "homekit_controller",
        "room-sensor-b-temperature",
        config_entry=source_entry,
        suggested_object_id="room_sensor_b_temperature",
    )
    original_entity_id = source.entity_id
    source_reference = {
        "registry_entry_id": source.id,
        "domain": "sensor",
        "platform": "homekit_controller",
        "unique_id": "room-sensor-b-temperature",
    }
    entry = _add_mock_entry(
        hass,
        options={
            CONF_POINT_LOOKBACK_DAYS: 30,
            CONF_SCAN_INTERVAL_SECONDS: 900,
            CONF_SENSORS: [
                {
                    CONF_ID: 2002,
                    CONF_TEMPERATURE_ENTITY_ID: original_entity_id,
                    CONF_TEMPERATURE_ENTITY_REF: source_reference,
                }
            ],
        },
    )
    entry.runtime_data = _runtime_data(
        thermostats=[],
        sensors=[
            _configured_sensor(
                sensor_id=2002,
                name="Room Sensor B",
                slug="room_sensor_b",
            )
        ],
    )
    entity_registry.async_remove(original_entity_id)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "sensor_mapping"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ID: "2002"},
    )

    assert result["type"] is FlowResultType.FORM
    assert CONF_TEMPERATURE_ENTITY_ID not in _suggested_values(result)
    assert entry.options[CONF_SENSORS][0][CONF_TEMPERATURE_ENTITY_ID] == (
        original_entity_id
    )
    assert entry.options[CONF_SENSORS][0][CONF_TEMPERATURE_ENTITY_REF] == (
        source_reference
    )

    restored = entity_registry.async_get_or_create(
        "sensor",
        "homekit_controller",
        "room-sensor-b-temperature",
        config_entry=source_entry,
        suggested_object_id="room_sensor_b_temperature_restored",
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "sensor_mapping"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ID: "2002"},
    )

    assert result["type"] is FlowResultType.FORM
    assert _suggested_values(result)[CONF_TEMPERATURE_ENTITY_ID] == restored.entity_id
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_TEMPERATURE_ENTITY_ID: restored.entity_id},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SENSORS][0][CONF_TEMPERATURE_ENTITY_ID] == (
        restored.entity_id
    )
    assert result["data"][CONF_SENSORS][0][CONF_TEMPERATURE_ENTITY_REF] == (
        {
            **source_reference,
            "registry_entry_id": restored.id,
        }
    )


def _suggested_values(result: dict[str, Any]) -> dict[str, Any]:
    """Return suggested values attached to one Home Assistant flow schema."""

    return {
        marker.schema: marker.description["suggested_value"]
        for marker in result["data_schema"].schema
        if marker.description is not None and "suggested_value" in marker.description
    }


def _add_mock_entry(
    hass: HomeAssistant,
    *,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=CONFIG_TITLE,
        unique_id=CONFIG_ENTRY_UNIQUE_ID,
        data=data
        if data is not None
        else {
            CONF_API_KEY: "old-key",
            CONF_API_BASE: API_BASE,
            CONF_ACCOUNT_FINGERPRINT: ACCOUNT_A,
        },
        options=options
        if options is not None
        else {
            CONF_POINT_LOOKBACK_DAYS: 30,
            CONF_SCAN_INTERVAL_SECONDS: 900,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _mock_validate_input(**kwargs: Any):
    kwargs.setdefault("return_value", ACCOUNT_A)
    return patch(
        "custom_components.beestat_statistics.config_flow._async_validate_input",
        new_callable=AsyncMock,
        **kwargs,
    )


def _runtime_data(
    *,
    thermostats: list[Any],
    sensors: list[Any],
    thermostat_rows: list[dict[str, Any]] | None = None,
    sensor_rows: list[dict[str, Any]] | None = None,
) -> Any:
    return types.SimpleNamespace(
        coordinator=types.SimpleNamespace(
            data=types.SimpleNamespace(
                config=types.SimpleNamespace(
                    thermostats=tuple(thermostats),
                    sensors=tuple(sensors),
                ),
                thermostat_rows=tuple(thermostat_rows or ()),
                sensor_rows=tuple(sensor_rows or ()),
            )
        )
    )


def _configured_thermostat(
    *,
    thermostat_id: int,
    name: str,
    slug: str,
    climate_entity_id: str | None = None,
    device_id: str | None = None,
    filter_change_day_runtime_baseline_seconds: float | None = None,
    filter_changed_date: date | None = None,
    filter_changed_at: datetime | None = None,
    filter_change_boundary_reconciled_at: datetime | None = None,
    filter_change_boundary_source_data_end: datetime | None = None,
) -> ConfiguredThermostat:
    return ConfiguredThermostat(
        thermostat_id=thermostat_id,
        name=name,
        slug=slug,
        climate_entity_id=climate_entity_id,
        device_id=device_id,
        filter_changed_date=filter_changed_date,
        filter_changed_at=filter_changed_at,
        filter_change_day_runtime_baseline_seconds=(
            filter_change_day_runtime_baseline_seconds
        ),
        filter_change_boundary_reconciled_at=(filter_change_boundary_reconciled_at),
        filter_change_boundary_source_data_end=(filter_change_boundary_source_data_end),
    )


def _configured_sensor(
    *,
    sensor_id: int,
    name: str,
    slug: str,
    device_id: str | None = None,
) -> ConfiguredSensor:
    return ConfiguredSensor(
        sensor_id=sensor_id,
        name=name,
        slug=slug,
        thermostat_id=None,
        thermostat_slug=None,
        include_temperature=True,
        include_air_quality=False,
        include_co2=False,
        include_voc=False,
        device_id=device_id,
    )

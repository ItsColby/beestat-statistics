"""Home Assistant config-flow tests for Beestat Statistics.

These tests exercise the real Home Assistant flow manager when the HA test
harness is installed. Local pure-unit validation skips this module when the
current Python environment cannot install Home Assistant.
"""

from __future__ import annotations

from pathlib import Path
import sys
import types
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import pytest

    from homeassistant.config_entries import (
        ConfigEntryState,
        SOURCE_IMPORT,
        SOURCE_REAUTH,
        SOURCE_RECONFIGURE,
        SOURCE_USER,
    )
    from homeassistant.const import CONF_API_KEY
    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowResultType
    from pytest_homeassistant_custom_component.common import MockConfigEntry
except ModuleNotFoundError as err:  # pragma: no cover - local non-HA test env
    raise unittest.SkipTest(f"Home Assistant test harness unavailable: {err}") from err

from custom_components.beestat_statistics.api import (  # noqa: E402
    BeestatApiError,
    BeestatAuthError,
)
from custom_components.beestat_statistics import async_setup  # noqa: E402
from custom_components.beestat_statistics.const import (  # noqa: E402
    API_BASE,
    ATTR_CONFIG_ENTRY_ID,
    CONF_ACCOUNT_FINGERPRINT,
    CONF_API_BASE,
    CONF_CLIMATE_ENTITY_ID,
    CONF_FILTER_CHANGED_DATE,
    CONF_ID,
    CONF_POINT_LOOKBACK_DAYS,
    CONF_SCAN_INTERVAL_SECONDS,
    CONF_SENSORS,
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_THERMOSTATS,
    CONFIG_ENTRY_UNIQUE_ID,
    CONFIG_TITLE,
    DOMAIN,
    SERVICE_GET_CONFIGURATION,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations"),
]


USER_INPUT = {
    CONF_API_KEY: "test-api-key",
    CONF_API_BASE: API_BASE,
    CONF_POINT_LOOKBACK_DAYS: 30,
    CONF_SCAN_INTERVAL_SECONDS: 900,
}
ACCOUNT_A = {
    "thermostat_id_hashes": ["account-a"],
    "signature": "account-a",
}
ACCOUNT_B = {
    "thermostat_id_hashes": ["account-b"],
    "signature": "account-b",
}


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
        CONF_POINT_LOOKBACK_DAYS: 30,
        CONF_SCAN_INTERVAL_SECONDS: 900,
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
) -> None:
    """Test the user can recover after an unexpected validation exception."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    with _mock_validate_input(side_effect=RuntimeError("boom")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}

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


async def test_import_flow_updates_existing_entry(hass: HomeAssistant) -> None:
    """Test YAML import updates the single existing config entry."""

    entry = _add_mock_entry(hass)
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
    assert dict(entry.data) == {
        CONF_API_KEY: "yaml-key",
        CONF_API_BASE: "https://api.example.test/",
    }
    assert dict(entry.options) == {
        CONF_POINT_LOOKBACK_DAYS: 90,
        CONF_SCAN_INTERVAL_SECONDS: 1800,
    }


async def test_import_flow_preserves_ui_mapping_options(
    hass: HomeAssistant,
) -> None:
    """Test YAML import preserves UI-owned mappings when YAML omits them."""

    entry = _add_mock_entry(
        hass,
        options={
            CONF_POINT_LOOKBACK_DAYS: 30,
            CONF_SCAN_INTERVAL_SECONDS: 900,
            CONF_THERMOSTATS: [
                {CONF_ID: 1001, CONF_FILTER_CHANGED_DATE: "2026-07-05"}
            ],
        },
    )
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
    assert dict(entry.data) == {
        CONF_API_KEY: "yaml-key",
        CONF_API_BASE: "https://api.example.test/",
    }
    assert dict(entry.options) == {
        CONF_POINT_LOOKBACK_DAYS: 90,
        CONF_SCAN_INTERVAL_SECONDS: 1800,
        CONF_THERMOSTATS: [
            {CONF_ID: 1001, CONF_FILTER_CHANGED_DATE: "2026-07-05"}
        ],
    }


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


async def test_reauth_flow_rejects_different_account(
    hass: HomeAssistant,
) -> None:
    """Test reauth does not silently switch to another Beestat account."""

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
    assert result["errors"] == {"base": "wrong_account"}
    assert entry.data[CONF_API_KEY] == "old-key"


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


async def test_reconfigure_flow_rejects_different_account(
    hass: HomeAssistant,
) -> None:
    """Test reconfigure keeps the existing account fingerprint."""

    entry = _add_mock_entry(hass)
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
    assert result["errors"] == {"base": "wrong_account"}
    assert entry.data[CONF_API_KEY] == "old-key"


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
    assert response["effective_configuration"]["thermostats"][0][
        CONF_CLIMATE_ENTITY_ID
    ] == "climate.zone_a"
    assert "api_key" not in repr(response)


async def test_options_flow_updates_thermostat_mapping(hass: HomeAssistant) -> None:
    """Test the options flow stores native thermostat mapping overrides."""

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
        {CONF_CLIMATE_ENTITY_ID: "climate.zone_a"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_THERMOSTATS] == [
        {CONF_ID: 1001, CONF_CLIMATE_ENTITY_ID: "climate.zone_a"}
    ]


async def test_options_flow_updates_room_sensor_mapping(hass: HomeAssistant) -> None:
    """Test the options flow identifies the selected room-sensor override."""

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
        {CONF_TEMPERATURE_ENTITY_ID: "sensor.room_sensor_b_temperature"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SENSORS] == [
        {CONF_ID: 2002, CONF_TEMPERATURE_ENTITY_ID: "sensor.room_sensor_b_temperature"}
    ]


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


def _runtime_data(*, thermostats: list[Any], sensors: list[Any]) -> Any:
    return types.SimpleNamespace(
        coordinator=types.SimpleNamespace(
            data=types.SimpleNamespace(
                config=types.SimpleNamespace(
                    thermostats=tuple(thermostats),
                    sensors=tuple(sensors),
                )
            )
        )
    )


def _configured_thermostat(
    *,
    thermostat_id: int,
    name: str,
    slug: str,
    climate_entity_id: str | None = None,
) -> Any:
    return types.SimpleNamespace(
        thermostat_id=thermostat_id,
        name=name,
        slug=slug,
        climate_entity_id=climate_entity_id,
        temperature_entity_id=None,
        occupancy_entity_id=None,
        motion_entity_id=None,
        filter_changed_entity_id=None,
        filter_changed_date=None,
        filter_lifetime_runtime_hours=250.0,
        filter_max_age_days=90,
        filter_notice_days=7,
    )


def _configured_sensor(
    *,
    sensor_id: int,
    name: str,
    slug: str,
) -> Any:
    return types.SimpleNamespace(
        sensor_id=sensor_id,
        name=name,
        slug=slug,
    )

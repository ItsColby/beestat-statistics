"""Import and rebuild actions plus the shipped blueprint, against a stub importer."""

from __future__ import annotations

import types
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant.components.blueprint import models as blueprint_models
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import yaml as yaml_util

from custom_components.beestat_statistics import async_setup
from custom_components.beestat_statistics.const import (
    DOMAIN,
    SERVICE_IMPORT_STATISTICS,
    SERVICE_REBUILD_STATISTICS,
)
from custom_components.beestat_statistics.import_support import UnknownThermostatError
from tests.runtime_support import _coordinator_data

BLUEPRINT = (
    Path(__file__).parents[1]
    / "blueprints/automation/beestat_statistics/stale_runtime_notification.yaml"
)


async def _loaded(hass: HomeAssistant, importer: AsyncMock | None = None) -> Any:
    now = datetime(2026, 7, 6, 18, tzinfo=UTC)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    importer = importer or AsyncMock()
    entry.runtime_data = types.SimpleNamespace(
        coordinator=coordinator,
        importer=types.SimpleNamespace(async_import_statistics=importer),
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup(hass, {})
    return entry, importer


@pytest.mark.usefixtures("unload_entries")
async def test_import_action_forwards_arguments(hass: HomeAssistant) -> None:
    _entry, importer = await _loaded(hass)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_IMPORT_STATISTICS,
        {"point_lookback_days": 3, "skip_sync": True},
        blocking=True,
    )
    importer.assert_awaited_once_with(point_lookback_days=3, skip_sync=True)


@pytest.mark.usefixtures("unload_entries")
async def test_rebuild_action_forwards_arguments(hass: HomeAssistant) -> None:
    _entry, importer = await _loaded(hass)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_REBUILD_STATISTICS,
        {"thermostat_id": 1, "start_date": "2026-07-01", "end_date": "2026-07-03"},
        blocking=True,
    )
    importer.assert_awaited_once_with(
        skip_sync=False,
        force_full_summary=True,
        rebuild_start=date(2026, 7, 1),
        rebuild_end=date(2026, 7, 3),
        thermostat_id=1,
    )


@pytest.mark.usefixtures("unload_entries")
async def test_rebuild_action_rejects_reversed_dates(hass: HomeAssistant) -> None:
    _entry, importer = await _loaded(hass)
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REBUILD_STATISTICS,
            {"start_date": "2026-07-04", "end_date": "2026-07-03"},
            blocking=True,
        )
    assert raised.value.translation_key == "invalid_rebuild_date_range"
    importer.assert_not_awaited()


@pytest.mark.usefixtures("unload_entries")
async def test_rebuild_action_maps_unknown_thermostat(hass: HomeAssistant) -> None:
    _entry, _importer = await _loaded(
        hass, AsyncMock(side_effect=UnknownThermostatError(99))
    )
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, SERVICE_REBUILD_STATISTICS, {"thermostat_id": 99}, blocking=True
        )
    assert raised.value.translation_key == "unknown_thermostat_id"
    assert raised.value.translation_placeholders == {"thermostat_id": "99"}


@pytest.mark.parametrize(
    "service", [SERVICE_IMPORT_STATISTICS, SERVICE_REBUILD_STATISTICS]
)
async def test_actions_require_a_loaded_entry(
    hass: HomeAssistant, service: str
) -> None:
    assert await async_setup(hass, {})
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(DOMAIN, service, {}, blocking=True)
    assert raised.value.translation_key == "no_loaded_entry"


@pytest.mark.usefixtures("unload_entries")
@pytest.mark.parametrize("value", [0, -1, 1.9, True, "1.5"])
async def test_rebuild_action_rejects_non_positive_integer_thermostat_id(
    hass: HomeAssistant, value: Any
) -> None:
    _entry, importer = await _loaded(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, SERVICE_REBUILD_STATISTICS, {"thermostat_id": value}, blocking=True
        )
    importer.assert_not_awaited()


def test_stale_runtime_blueprint_loads_with_expected_inputs() -> None:
    blueprint = blueprint_models.Blueprint(
        yaml_util.load_yaml_dict(str(BLUEPRINT)),
        expected_domain="automation",
        path=BLUEPRINT.name,
        schema=BLUEPRINT_SCHEMA,
    )
    assert set(blueprint.inputs) == {
        "lag_sensor",
        "threshold_days",
        "stale_for",
        "notify_action",
    }
    assert blueprint.metadata["source_url"].endswith(
        "blueprints/automation/beestat_statistics/stale_runtime_notification.yaml"
    )

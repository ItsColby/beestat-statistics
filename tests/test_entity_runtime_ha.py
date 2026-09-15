"""Exact-Core tests for Beestat entity state and action boundaries."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.const import (
    CONF_API_KEY,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.beestat_statistics import _async_track_room_temperature_sources
from custom_components.beestat_statistics.api import (
    BeestatApiError,
    BeestatAuthError,
    BeestatClient,
)
from custom_components.beestat_statistics.binary_sensor import (
    BeestatFilterDueProblemBinarySensor,
    BeestatFilterDueSoonProblemBinarySensor,
)
from custom_components.beestat_statistics.button import BeestatFilterChangedButton
from custom_components.beestat_statistics.const import API_BASE, CONF_API_BASE, DOMAIN
from custom_components.beestat_statistics.coordinator import (
    BeestatRuntimeDataCoordinator,
    RoomTemperatureSpread,
)
from custom_components.beestat_statistics.date import BeestatFilterChangedDate
from custom_components.beestat_statistics.runtime import BeestatStatisticsRuntime
from custom_components.beestat_statistics.sensor import (
    BeestatSensor,
    _thermostat_sensor_descriptions,
)

pytestmark = pytest.mark.asyncio
_LOGGER = logging.getLogger(__name__)


@pytest.fixture
async def coordinator(
    hass: HomeAssistant, freezer: Any
) -> AsyncIterator[BeestatRuntimeDataCoordinator]:
    """Keep source and projection clocks coherent and always cancel owned timers."""

    now = datetime(2026, 7, 5, 12, tzinfo=UTC)
    freezer.move_to(now)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "test-key", CONF_API_BASE: API_BASE},
    )
    entry.add_to_hass(hass)
    coordinator = BeestatRuntimeDataCoordinator(
        hass, entry, Mock(spec=BeestatClient), local_tz=ZoneInfo("UTC")
    )
    coordinator.data = coordinator._build_runtime_data(
        [],
        [{"id": 1, "name": "Zone A"}],
        [],
        now,
        now,
        True,
        None,
        None,
        evaluated_at=now,
        fetched_at=now,
    )
    try:
        yield coordinator
    finally:
        await entry._async_process_on_unload(hass)


@asynccontextmanager
async def _entity_platform(
    coordinator: BeestatRuntimeDataCoordinator, domain: str
) -> AsyncIterator[EntityPlatform]:
    """Register entities under their real config-entry owner and always remove them."""

    platform = EntityPlatform(
        hass=coordinator.hass,
        logger=_LOGGER,
        domain=domain,
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = coordinator.beestat_config_entry
    try:
        yield platform
    finally:
        await platform.async_reset()


async def test_spread_recovers_and_changes_native_unit_with_its_value(
    hass: HomeAssistant,
    coordinator: BeestatRuntimeDataCoordinator,
) -> None:
    """A late projection and later unit changes must retain valid numeric state."""

    thermostat = coordinator.data.config.thermostats[0]
    description = next(
        description
        for description in _thermostat_sensor_descriptions(thermostat=thermostat)
        if description.translation_key == "current_profile_room_temperature_spread"
    )
    entity = BeestatSensor(coordinator, description, None)
    entity.entity_id = "sensor.profile_spread"
    assert entity.native_unit_of_measurement is None
    assert not entity.available
    async with _entity_platform(coordinator, "sensor") as platform:
        await platform.async_add_entities([entity])
        assert hass.states.get(entity.entity_id).state == STATE_UNAVAILABLE

        for unit, value in (("°C", 2.0), ("°F", 3.6)):
            projection = RoomTemperatureSpread(
                value=value,
                unit=unit,
                participating_sensor_count=2,
                valid_sensor_count=2,
                participating_sensor_names=("Bedroom", "Office"),
                unavailable_sensor_names=(),
                hottest_sensor_name="Office",
                coldest_sensor_name="Bedroom",
            )
            coordinator.async_set_updated_data(
                replace(coordinator.data, room_temperature_spreads={1: projection})
            )
            await hass.async_block_till_done()
            state = hass.states.get(entity.entity_id)
            assert entity.native_unit_of_measurement == unit
            assert entity.native_value == value
            assert entity.available
            # HA may retain the display unit while converting the new native unit.
            assert state.attributes["unit_of_measurement"] in {"°C", "°F"}
            expected = 2.0 if state.attributes["unit_of_measurement"] == "°C" else 3.6
            assert float(state.state) == pytest.approx(expected)


async def test_filter_notice_remains_independent_of_due_uncertainty(
    hass: HomeAssistant,
    coordinator: BeestatRuntimeDataCoordinator,
) -> None:
    """Missing exposure preserves unknown due while the calendar notice is usable."""

    now = coordinator.data.projected_at
    entry = coordinator.beestat_config_entry

    def rebuild(*, maximum_days: int, notice_days: int):
        hass.config_entries.async_update_entry(
            entry,
            options={
                "thermostats": [
                    {
                        "id": 1,
                        "filter_changed_date": (
                            now.date() - timedelta(days=83)
                        ).isoformat(),
                        "filter_max_age_days": maximum_days,
                        "filter_notice_days": notice_days,
                    }
                ]
            },
        )
        return coordinator._build_runtime_data(
            [],
            [{"id": 1, "name": "Zone A"}],
            [],
            now,
            now,
            True,
            None,
            None,
            evaluated_at=now,
            fetched_at=now,
        )

    coordinator.async_set_updated_data(rebuild(maximum_days=90, notice_days=7))
    thermostat = coordinator.data.config.thermostats[0]
    due = BeestatFilterDueProblemBinarySensor(coordinator, thermostat)
    notice = BeestatFilterDueSoonProblemBinarySensor(coordinator, thermostat)
    due.entity_id = "binary_sensor.filter_due"
    notice.entity_id = "binary_sensor.filter_notice"
    async with _entity_platform(coordinator, "binary_sensor") as platform:
        await platform.async_add_entities([due, notice])
        assert hass.states.get(due.entity_id).state == STATE_UNKNOWN
        assert hass.states.get(notice.entity_id).state == STATE_ON

        coordinator.async_set_updated_data(rebuild(maximum_days=90, notice_days=0))
        await hass.async_block_till_done()
        assert hass.states.get(due.entity_id).state == STATE_UNKNOWN
        assert hass.states.get(notice.entity_id).state == STATE_OFF

        coordinator.async_set_updated_data(rebuild(maximum_days=83, notice_days=0))
        await hass.async_block_till_done()
        assert hass.states.get(due.entity_id).state == STATE_ON
        assert hass.states.get(notice.entity_id).state == STATE_ON

        good_data = coordinator.data
        coordinator.async_set_update_error(UpdateFailed("Synthetic source failure"))
        await hass.async_block_till_done()
        assert hass.states.get(due.entity_id).state == STATE_UNAVAILABLE
        assert hass.states.get(notice.entity_id).state == STATE_UNAVAILABLE

        coordinator.async_set_updated_data(
            replace(good_data, config=replace(good_data.config, thermostats=()))
        )
        await hass.async_block_till_done()
        assert hass.states.get(due.entity_id).state == STATE_UNAVAILABLE
        assert hass.states.get(notice.entity_id).state == STATE_UNAVAILABLE


async def test_temperature_listener_updates_filter_uncertainty_without_revision_churn(
    hass: HomeAssistant,
    coordinator: BeestatRuntimeDataCoordinator,
    freezer: Any,
) -> None:
    """An unrelated mapped-temperature report must not look like new filter evidence."""

    now = coordinator.data.projected_at
    changed = datetime(2026, 7, 4, tzinfo=UTC)
    source_end = datetime(2026, 7, 4, 23, 55, tzinfo=UTC)
    entry = coordinator.beestat_config_entry
    hass.states.async_set("sensor.zone_temperature", 20, {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    options = {
        "thermostats": [
            {
                "id": 1,
                "temperature_entity_id": "sensor.zone_temperature",
                "filter_changed_at": changed.isoformat(),
                "filter_changed_date": changed.date().isoformat(),
            }
        ]
    }
    hass.config_entries.async_update_entry(entry, options=options)
    coordinator._filter_day_cache[1] = (
        (changed, "UTC"),
        tuple(
            {
                "timestamp": (changed + timedelta(minutes=5 * index)).isoformat(),
                "fan": 0,
            }
            for index in range(288)
        ),
    )
    coordinator.data = coordinator._build_runtime_data(
        [],
        [{"id": 1, "name": "Zone A", "data_end": source_end.isoformat()}],
        [],
        now,
        now,
        True,
        None,
        None,
        evaluated_at=now,
        fetched_at=now,
    )
    entry.runtime_data = BeestatStatisticsRuntime(
        coordinator._client, coordinator, Mock(), timedelta(minutes=30)
    )
    _async_track_room_temperature_sources(hass, entry)
    thermostat = coordinator.data.config.thermostats[0]
    description = next(
        description
        for description in _thermostat_sensor_descriptions(thermostat=thermostat)
        if description.translation_key == "filter_due_date"
    )
    entity = BeestatSensor(coordinator, description, None)
    entity.entity_id = "sensor.filter_due_date"
    async with _entity_platform(coordinator, "sensor") as platform:
        await platform.async_add_entities([entity])
        before = hass.states.get(entity.entity_id)
        assert before.attributes["runtime_unknown_interval_minutes"] == 720
        assert before.attributes["runtime_threshold_reached"] is False

        for seconds in (15, 30, 60):
            freezer.move_to(now + timedelta(seconds=seconds))
            hass.states.async_set(
                "sensor.zone_temperature", 20 + seconds, {"unit_of_measurement": "°C"}
            )
            await hass.async_block_till_done()
            after = hass.states.get(entity.entity_id)
            assert coordinator.data.projected_at == now + timedelta(seconds=seconds)
            assert after.state == before.state
            assert (
                after.attributes["forecast_revision"]
                == before.attributes["forecast_revision"]
            )
            assert after.attributes["runtime_unknown_interval_minutes"] == (
                720 + seconds / 60
            )
            assert after.attributes["changed_at"] == changed.isoformat()
            assert after.attributes["runtime_source_data_end"] == source_end.isoformat()

        assert entry.options == options
        assert coordinator.data.fetched_at == now
        assert not coordinator._client.mock_calls


async def test_spread_context_tracks_cloud_membership_not_local_projection_time(
    hass: HomeAssistant,
    coordinator: BeestatRuntimeDataCoordinator,
    freezer: Any,
) -> None:
    """Temperature, coverage and schedule changes preserve cloud provenance."""

    names = ("Bedroom", "Office", "Hall")
    sensor_rows = [
        {"id": index, "thermostat_id": 1, "identifier": f"rs:{index}", "name": name}
        for index, name in enumerate(names, 10)
    ]
    hass.config_entries.async_update_entry(
        coordinator.beestat_config_entry,
        options={
            "sensors": [
                {"id": row["id"], "temperature_entity_id": f"sensor.{name.lower()}"}
                for row, name in zip(sensor_rows, names, strict=True)
            ]
        },
    )
    for name, value in zip(names, (20, 22, 24), strict=True):
        hass.states.async_set(
            f"sensor.{name.lower()}", value, {"unit_of_measurement": "°C"}
        )
    profiles = [
        {
            "climateRef": ref,
            "name": ref.title(),
            "sensors": [{"id": f"rs:{index}:1"} for index in members],
        }
        for ref, members in (("home", (10, 11, 12)), ("sleep", (10, 12)))
    ]
    schedule = [["home"] * 26 + ["sleep"] * 22 for _ in range(7)]

    def publish_profile(ref: str | None, synced_at: datetime) -> None:
        coordinator.async_set_updated_data(
            coordinator._build_runtime_data(
                [],
                [
                    {
                        "id": 1,
                        "name": "Zone A",
                        "timezone": "UTC",
                        "program": {
                            "currentClimateRef": ref,
                            "climates": profiles,
                            "schedule": schedule,
                        },
                    }
                ],
                sensor_rows,
                synced_at,
                synced_at,
                True,
                None,
                None,
                evaluated_at=synced_at,
            )
        )

    synced_at = coordinator.data.metadata_sync_success_at
    assert synced_at is not None
    publish_profile("home", synced_at)
    thermostat = coordinator.data.config.thermostats[0]
    description = next(
        description
        for description in _thermostat_sensor_descriptions(thermostat=thermostat)
        if description.translation_key == "current_profile_room_temperature_spread"
    )
    entity = BeestatSensor(coordinator, description, None)
    entity.entity_id = "sensor.profile_spread"
    async with _entity_platform(coordinator, "sensor") as platform:
        await platform.async_add_entities([entity])
        state = hass.states.get(entity.entity_id)
        assert float(state.state) == 4
        assert state.attributes["profile_name"] == "Home"
        assert state.attributes["profile_ref"] == "home"
        assert state.attributes["configured_sensor_count"] == 3
        assert state.attributes["valid_sensor_count"] == 3

        freezer.move_to(synced_at + timedelta(minutes=10))
        hass.states.async_set("sensor.office", STATE_UNAVAILABLE)
        hass.states.async_set("sensor.hall", 25, {"unit_of_measurement": "°C"})
        coordinator.async_rebuild_runtime_from_cached_rows()
        await hass.async_block_till_done()
        state = hass.states.get(entity.entity_id)
        assert float(state.state) == 5
        assert state.attributes["configured_sensor_count"] == 3
        assert state.attributes["valid_sensor_count"] == 2
        assert state.attributes["unavailable_sensor_names"] == ["Office"]
        assert state.attributes["metadata_synced_at"] == synced_at.isoformat()

        boundary = synced_at + timedelta(hours=1)
        freezer.move_to(boundary)
        async_fire_time_changed_exact(hass, boundary)
        await hass.async_block_till_done()
        state = hass.states.get(entity.entity_id)
        assert coordinator.data.thermostat_metadata[1].scheduled_climate_ref == "sleep"
        assert state.attributes["profile_ref"] == "home"
        assert state.attributes["metadata_synced_at"] == synced_at.isoformat()
        assert not coordinator._client.mock_calls

        refreshed_at = synced_at + timedelta(hours=2)
        freezer.move_to(refreshed_at)
        publish_profile("sleep", refreshed_at)
        await hass.async_block_till_done()
        state = hass.states.get(entity.entity_id)
        assert float(state.state) == 5
        assert state.attributes["profile_name"] == "Sleep"
        assert state.attributes["profile_ref"] == "sleep"
        assert state.attributes["configured_sensor_count"] == 2
        assert state.attributes["valid_sensor_count"] == 2
        assert state.attributes["unavailable_sensor_names"] == []
        assert state.attributes["metadata_synced_at"] == refreshed_at.isoformat()

        for ref in ("unknown-profile", None):
            publish_profile(ref, refreshed_at)
            await hass.async_block_till_done()
            state = hass.states.get(entity.entity_id)
            assert state.state == STATE_UNAVAILABLE
            assert "profile_ref" not in state.attributes
            assert "profile_name" not in state.attributes
            assert "metadata_synced_at" not in state.attributes
            assert "configured_sensor_count" not in state.attributes


@pytest.mark.parametrize(
    ("error_type", "translation_key", "reauth"),
    [
        (BeestatAuthError, "beestat_auth_failed", True),
        (BeestatApiError, "beestat_request_failed", False),
        (RuntimeError, "beestat_request_failed", False),
    ],
)
async def test_filter_date_errors_are_sanitized_and_auth_starts_reauth(
    coordinator: BeestatRuntimeDataCoordinator,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    translation_key: str,
    reauth: bool,
) -> None:
    entity = BeestatFilterChangedDate(
        coordinator, coordinator.data.config.thermostats[0]
    )
    private_detail = "private-unexpected-response-detail"
    with (
        patch(
            "custom_components.beestat_statistics.date.async_set_filter_changed_date",
            new=AsyncMock(side_effect=error_type(private_detail)),
        ),
        patch.object(
            coordinator.beestat_config_entry, "async_start_reauth_if_available"
        ) as start_reauth,
        pytest.raises(HomeAssistantError) as raised,
    ):
        await entity.async_set_value(date(2026, 7, 1))

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == translation_key
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert private_detail not in str(raised.value)
    assert private_detail not in caplog.text
    assert start_reauth.called is reauth


async def test_filter_button_tracks_source_removal_recovery_and_cloud_failure(
    hass: HomeAssistant,
    coordinator: BeestatRuntimeDataCoordinator,
) -> None:
    """The physical-change control updates with source scope and survives cloud loss."""

    initial_data = coordinator.data
    entity = BeestatFilterChangedButton(coordinator, initial_data.config.thermostats[0])
    entity.entity_id = "button.mark_filter_changed"
    async with _entity_platform(coordinator, "button") as platform:
        await platform.async_add_entities([entity])
        entry_id = coordinator.beestat_config_entry.entry_id
        assert entity.registry_entry.config_entry_id == entry_id
        assert entity.device_entry.config_entry_id == entry_id
        assert hass.states.get(entity.entity_id).state != STATE_UNAVAILABLE

        now = initial_data.projected_at
        removed_data = coordinator._build_runtime_data(
            [],
            [],
            [],
            now,
            now,
            True,
            None,
            None,
            evaluated_at=now,
            fetched_at=now,
        )
        assert not removed_data.config.thermostats
        assert not removed_data.thermostat_rows
        coordinator.async_set_updated_data(removed_data)
        await hass.async_block_till_done()
        assert not entity.available
        assert hass.states.get(entity.entity_id).state == STATE_UNAVAILABLE

        coordinator.async_set_updated_data(initial_data)
        coordinator.last_update_success = False
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert entity.available
        assert hass.states.get(entity.entity_id).state != STATE_UNAVAILABLE

    assert not coordinator._listeners

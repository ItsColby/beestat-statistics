"""Native service persistence and real Recorder correction acceptance tests."""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.beestat_statistics import BeestatStatisticsImporter, async_setup
from custom_components.beestat_statistics.const import (
    DOMAIN,
    SERVICE_RECORD_FILTER_CHANGE,
    SERVICE_REPAIR_FILTER_CHANGE_BOUNDARY,
)
from custom_components.beestat_statistics.entry_options import async_mark_filter_changed
from tests.test_runtime_ha import _coordinator_data

pytestmark = pytest.mark.asyncio


async def test_timestamped_action_persists_completion_time_and_safe_replay(
    hass: HomeAssistant, freezer: Any
) -> None:
    now = datetime(2026, 7, 6, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup(hass, {})
    changed_at = "2026-07-05T21:48:12.345678+00:00"
    request = {
        "config_entry_id": entry.entry_id,
        "thermostat_id": 1,
        "changed_at": changed_at,
        "expected_changed_at": None,
        "expected_changed_date": None,
        "expected_request_id": None,
        "request_id": "qualified-completion-1",
    }
    with (
        patch.object(
            coordinator,
            "async_refresh_runtime",
            new=AsyncMock(side_effect=RuntimeError("offline")),
        ),
        patch.object(
            coordinator, "async_dismiss_filter_alerts", new=AsyncMock(return_value=0)
        ) as dismiss,
        patch.object(
            coordinator, "async_schedule_filter_boundary_reconcile", new=Mock()
        ),
    ):
        result = await hass.services.async_call(
            DOMAIN,
            SERVICE_RECORD_FILTER_CHANGE,
            request,
            blocking=True,
            return_response=True,
        )
        assert result["status"] == "recorded"
        assert result["changed_at"] == changed_at
        assert result["changed_date"] == "2026-07-05"
        assert result["boundary_status"] == "pending_data"
        persisted = entry.options["thermostats"][0]
        assert persisted["filter_changed_at"] == changed_at
        assert persisted["filter_change_event"]["source"] == "service"
        assert persisted["filter_change_event"]["recorded_at"] == now.isoformat()
        replay = await hass.services.async_call(
            DOMAIN,
            SERVICE_RECORD_FILTER_CHANGE,
            request,
            blocking=True,
            return_response=True,
        )
        assert replay["status"] == "already_recorded"
        freezer.move_to(now + timedelta(days=40))
        old_replay = await hass.services.async_call(
            DOMAIN,
            SERVICE_RECORD_FILTER_CHANGE,
            request,
            blocking=True,
            return_response=True,
        )
        assert old_replay["status"] == "already_recorded"
        freezer.move_to(now)
        dismiss.assert_awaited_once()
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_RECORD_FILTER_CHANGE,
                {
                    **request,
                    "request_id": "stale-completion",
                    "changed_at": now.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
        assert raised.value.translation_key == "filter_change_boundary_conflict"
        assert entry.options["thermostats"][0] == persisted
    await entry._async_process_on_unload(hass)


@pytest.mark.parametrize(
    "changes",
    [
        {"thermostat_id": 999},
        {"changed_at": "2026-07-07T00:00:00+00:00"},
        {"changed_at": "2026-06-01T00:00:00+00:00"},
        {"expected_changed_date": "2026-07-01"},
    ],
)
async def test_timestamped_action_rejects_invalid_or_stale_request_without_write(
    hass: HomeAssistant, freezer: Any, changes: dict[str, Any]
) -> None:
    now = datetime(2026, 7, 6, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup(hass, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECORD_FILTER_CHANGE,
            {
                "config_entry_id": entry.entry_id,
                "thermostat_id": 1,
                "changed_at": "2026-07-05T18:00:00+00:00",
                "expected_changed_at": None,
                "expected_changed_date": None,
                "expected_request_id": None,
                "request_id": "completion",
                **changes,
            },
            blocking=True,
            return_response=True,
        )
    assert entry.options == {}
    await entry._async_process_on_unload(hass)


@pytest.mark.usefixtures("recorder_mock")
async def test_bounded_rebuild_updates_real_recorder_tail_and_next_window_seed(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An old corrected day updates subsequent sums without widening measurements."""

    now = datetime(2026, 7, 31, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, client = _coordinator_data(hass, evaluated_at=now)
    await hass.async_start()
    start_day = date(2026, 6, 21)
    rows = [
        {
            "thermostat_id": 1,
            "date": (start_day + timedelta(days=index)).isoformat(),
            "count": 288,
            "sum_fan": 3600,
            "avg_indoor_humidity": 40.0,
        }
        for index in range(40)
    ]

    def rebuild_data():
        coordinator.data = coordinator._build_runtime_data(
            rows,
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

    async def refresh(**_kwargs):
        rebuild_data()
        return coordinator.data

    async def summary_window(start, end):
        return [row for row in rows if start <= row["date"] <= end]

    rebuild_data()
    client.async_read_runtime_thermostat = AsyncMock(return_value=[])
    client.async_read_runtime_sensor = AsyncMock(return_value=[])
    client.async_read_runtime_thermostat_summary = AsyncMock(side_effect=summary_window)
    importer = BeestatStatisticsImporter(
        hass, client, coordinator, point_lookback_days=1
    )
    fan_id = "beestat:zone_a_fan_runtime_hours"
    humidity_id = "beestat:zone_a_indoor_humidity"

    async def read():
        await async_wait_recording_done(hass)
        return await get_instance(hass).async_add_executor_job(
            partial(
                statistics_during_period,
                hass,
                datetime(2026, 6, 20, tzinfo=UTC),
                None,
                {fan_id, humidity_id},
                "hour",
                None,
                {"sum", "state", "mean"},
            )
        )

    with patch.object(coordinator, "async_refresh_runtime", new=refresh):
        await importer.async_import_statistics(skip_sync=True, force_full_summary=True)
        initial = await read()
        assert initial[fan_id][-1]["sum"] == 40
        # Correct the second day's runtime and all humidity source values. Only
        # the selected humidity day may change, but every later sum must change.
        rows[1]["sum_fan"] = 10800
        for row in rows:
            row["avg_indoor_humidity"] = 45.0
        changed_day = start_day + timedelta(days=1)
        for _replay in range(2):
            await importer.async_import_statistics(
                skip_sync=True,
                force_full_summary=True,
                rebuild_start=changed_day,
                rebuild_end=changed_day,
            )
            corrected = await read()
            assert len(corrected[fan_id]) == 40
            assert corrected[fan_id][0]["sum"] == 1
            assert corrected[fan_id][1]["sum"] == 4
            assert corrected[fan_id][-1]["sum"] == 42
            assert corrected[humidity_id][1]["mean"] == 45
            assert corrected[humidity_id][-1]["mean"] == 40
        rows.append(
            {
                "thermostat_id": 1,
                "date": "2026-07-31",
                "count": 288,
                "sum_fan": 3600,
                "avg_indoor_humidity": 45.0,
            }
        )
        result = await importer.async_import_statistics(skip_sync=True)
        final = await read()
        assert result.summary_mode == "windowed"
        assert final[fan_id][-1]["sum"] == 43
    await entry._async_process_on_unload(hass)


async def test_repair_rejects_stale_projection_after_new_replacement_is_saved(
    hass: HomeAssistant, freezer: Any
) -> None:
    now = datetime(2026, 7, 6, 18, tzinfo=UTC)
    freezer.move_to(now)
    entry, coordinator, _client = _coordinator_data(hass, evaluated_at=now)
    hass.config_entries.async_update_entry(
        entry, options={"thermostats": [{"id": 1, "filter_changed_date": "2026-07-04"}]}
    )
    coordinator.async_rebuild_runtime_from_cached_rows()
    assert coordinator.data.config.thermostats[0].filter_changed_date == date(
        2026, 7, 4
    )
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_setup(hass, {})
    with (
        patch.object(
            coordinator,
            "async_rebuild_runtime_from_cached_rows",
            side_effect=RuntimeError("cached failure"),
        ),
        patch.object(
            coordinator,
            "async_refresh_runtime",
            new=AsyncMock(side_effect=RuntimeError("offline")),
        ),
        patch.object(
            coordinator, "async_dismiss_filter_alerts", new=AsyncMock(return_value=0)
        ),
        patch.object(
            coordinator, "async_schedule_filter_boundary_reconcile", new=Mock()
        ),
    ):
        await async_mark_filter_changed(
            coordinator, 1, datetime(2026, 7, 5, 18, tzinfo=UTC)
        )
        saved = entry.options
        assert coordinator.data.config.thermostats[0].filter_changed_date == date(
            2026, 7, 4
        )
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_REPAIR_FILTER_CHANGE_BOUNDARY,
                {
                    "config_entry_id": entry.entry_id,
                    "thermostat_id": 1,
                    "changed_at": "2026-07-04T18:00:00+00:00",
                },
                blocking=True,
            )
        assert raised.value.translation_key == "filter_change_boundary_date_mismatch"
        assert entry.options is saved
    await entry._async_process_on_unload(hass)

"""Import Beestat HVAC data into Home Assistant external statistics."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date as dt_date, datetime, time, timedelta, timezone
from functools import partial
import logging
from typing import Any
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant.components.recorder import get_instance as get_recorder_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, SOURCE_IMPORT
from homeassistant.const import (
    CONF_API_KEY,
    CONF_SCAN_INTERVAL,
    Platform,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .api import BeestatApiError, BeestatAuthError, BeestatClient
from .config_model import (
    ConfiguredThermostat,
    build_sensor_statistics as build_sensor_specs,
    configured_override_entity_domain_errors,
    configured_override_entity_ids,
)
from .configuration import configuration_response
from .config_payload import (
    entry_data_from_yaml,
    entry_options_from_yaml,
    entry_runtime_config_data,
    migrate_entry_payload,
)
from .const import (
    API_BASE,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_END_DATE,
    ATTR_SKIP_SYNC,
    ATTR_START_DATE,
    CONF_API_BASE,
    CONF_CLIMATE_ENTITY_ID,
    CONF_FILTER_CHANGED_ENTITY_ID,
    CONF_FILTER_CHANGED_DATE,
    CONF_FILTER_LIFETIME_RUNTIME_HOURS,
    CONF_FILTER_MAX_AGE_DAYS,
    CONF_FILTER_NOTICE_DAYS,
    CONF_ID,
    CONF_INCLUDE_AIR_QUALITY,
    CONF_INCLUDE_CO2,
    CONF_INCLUDE_TEMPERATURE,
    CONF_INCLUDE_VOC,
    CONF_MOTION_ENTITY_ID,
    CONF_OCCUPANCY_ENTITY_ID,
    CONF_OVERRIDE_NAME,
    CONF_POINT_LOOKBACK_DAYS,
    CONF_SCAN_INTERVAL_SECONDS,
    CONF_SENSORS,
    CONF_SLUG,
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_THERMOSTAT_ID,
    CONF_THERMOSTATS,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_FILTER_LIFETIME_RUNTIME_HOURS,
    DEFAULT_FILTER_MAX_AGE_DAYS,
    DEFAULT_FILTER_NOTICE_DAYS,
    DEFAULT_POINT_LOOKBACK_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_SUMMARY_OVERLAP_DAYS,
    DOMAIN,
    MAX_FILTER_LIFETIME_RUNTIME_HOURS,
    MAX_FILTER_MAX_AGE_DAYS,
    MAX_FILTER_NOTICE_DAYS,
    MAX_POINT_LOOKBACK_DAYS,
    MAX_WINDOW_DAYS,
    MIN_SCAN_INTERVAL_SECONDS,
    SERVICE_GET_CONFIGURATION,
    SERVICE_IMPORT_STATISTICS,
    SERVICE_REBUILD_STATISTICS,
    sensor_entity_unique_id,
    thermostat_entity_unique_id,
)
from .coordinator import BeestatRuntimeData, BeestatRuntimeDataCoordinator
from .entity import async_register_service_device
from .runtime import BeestatStatisticsConfigEntry, BeestatStatisticsRuntime
from .statistics_builder import (
    CumulativeStatisticSeed,
    StatisticsSeries,
    apply_cumulative_seeds,
    build_statistics,
    cumulative_statistic_ids,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: tuple[Platform, ...] = (
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.DATE,
    Platform.SENSOR,
)

_THERMOSTAT_ENTITY_SUFFIXES: tuple[str, ...] = (
    "runtime_summary_latest_date",
    "runtime_summary_lag_days",
    "current_comfort_profile",
    "scheduled_comfort_profile",
    "next_scheduled_comfort_profile_time",
    "active_sensor_count",
    "cloud_data_end",
    "cloud_data_lag_minutes",
    "active_alert_count",
    "active_alert_category",
    "filter_runtime_hours",
    "filter_recent_runtime_hours_per_day",
    "filter_remaining_runtime_hours",
    "filter_runtime_due_date",
    "filter_max_age_due_date",
    "filter_due_date",
    "filter_days_remaining",
    "filter_changed_date",
    "mark_filter_changed",
    "equipment_alert",
    "filter_due",
    "filter_due_soon",
    "runtime_summary_stale",
    "cloud_data_stale",
)
_DEFAULT_ENABLED_PROBLEM_ENTITY_SUFFIXES: frozenset[str] = frozenset(
    {
        "runtime_summary_stale",
        "cloud_data_stale",
    }
)
_MISSING_OVERRIDE_ENTITIES_ISSUE_ID = "missing_override_entities"
_INVALID_OVERRIDE_ENTITY_DOMAINS_ISSUE_ID = "invalid_override_entity_domains"
_GLOBAL_UNIQUE_ID_MIGRATION = {
    "beestat_statistics_status": "status",
    "beestat_runtime_sync_last_success": "runtime_sync_last_success",
    "beestat_metadata_sync_last_success": "metadata_sync_last_success",
    "beestat_runtime_summary_row_count": "runtime_summary_row_count",
    "beestat_statistics_last_import_success": "statistics_last_import_success",
    "beestat_statistics_imported_series": "statistics_imported_series",
    "beestat_statistics_imported_rows": "statistics_imported_rows",
    "beestat_statistics_source_rows": "statistics_source_rows",
    "beestat_refresh_runtime": "refresh_runtime",
    "beestat_import_statistics": "import_statistics",
}

_CLIMATE_ENTITY_ID_SCHEMA = vol.All(cv.entity_id, cv.entity_domain("climate"))
_FILTER_CHANGED_ENTITY_ID_SCHEMA = vol.All(
    cv.entity_id,
    cv.entity_domain("input_datetime"),
)
_MOTION_ENTITY_ID_SCHEMA = vol.All(cv.entity_id, cv.entity_domain("binary_sensor"))
_OCCUPANCY_ENTITY_ID_SCHEMA = vol.All(cv.entity_id, cv.entity_domain("binary_sensor"))
_TEMPERATURE_ENTITY_ID_SCHEMA = vol.All(cv.entity_id, cv.entity_domain("sensor"))

THERMOSTAT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ID): vol.Coerce(int),
        vol.Optional(CONF_SLUG): cv.slug,
        vol.Optional(CONF_OVERRIDE_NAME): cv.string,
        vol.Optional(CONF_CLIMATE_ENTITY_ID): _CLIMATE_ENTITY_ID_SCHEMA,
        vol.Optional(CONF_TEMPERATURE_ENTITY_ID): _TEMPERATURE_ENTITY_ID_SCHEMA,
        vol.Optional(CONF_OCCUPANCY_ENTITY_ID): _OCCUPANCY_ENTITY_ID_SCHEMA,
        vol.Optional(CONF_MOTION_ENTITY_ID): _MOTION_ENTITY_ID_SCHEMA,
        vol.Optional(CONF_FILTER_CHANGED_ENTITY_ID): _FILTER_CHANGED_ENTITY_ID_SCHEMA,
        vol.Optional(CONF_FILTER_CHANGED_DATE): cv.date,
        vol.Optional(
            CONF_FILTER_LIFETIME_RUNTIME_HOURS,
            default=DEFAULT_FILTER_LIFETIME_RUNTIME_HOURS,
        ): vol.All(
            vol.Coerce(float),
            vol.Range(min=1, max=MAX_FILTER_LIFETIME_RUNTIME_HOURS),
        ),
        vol.Optional(
            CONF_FILTER_MAX_AGE_DAYS,
            default=DEFAULT_FILTER_MAX_AGE_DAYS,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_FILTER_MAX_AGE_DAYS)),
        vol.Optional(
            CONF_FILTER_NOTICE_DAYS,
            default=DEFAULT_FILTER_NOTICE_DAYS,
        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_FILTER_NOTICE_DAYS)),
        vol.Optional("enabled", default=True): cv.boolean,
    }
)

SENSOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ID): vol.Coerce(int),
        vol.Optional(CONF_THERMOSTAT_ID): vol.Coerce(int),
        vol.Optional(CONF_SLUG): cv.slug,
        vol.Optional(CONF_OVERRIDE_NAME): cv.string,
        vol.Optional(CONF_TEMPERATURE_ENTITY_ID): _TEMPERATURE_ENTITY_ID_SCHEMA,
        vol.Optional(CONF_OCCUPANCY_ENTITY_ID): _OCCUPANCY_ENTITY_ID_SCHEMA,
        vol.Optional(CONF_MOTION_ENTITY_ID): _MOTION_ENTITY_ID_SCHEMA,
        vol.Optional(CONF_INCLUDE_TEMPERATURE): cv.boolean,
        vol.Optional(CONF_INCLUDE_AIR_QUALITY): cv.boolean,
        vol.Optional(CONF_INCLUDE_CO2): cv.boolean,
        vol.Optional(CONF_INCLUDE_VOC): cv.boolean,
        vol.Optional("enabled", default=True): cv.boolean,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Schema(
            {
                vol.Required(CONF_API_KEY): vol.All(cv.string, vol.Length(min=1)),
                vol.Optional(CONF_API_BASE, default=API_BASE): cv.url,
                vol.Optional(
                    CONF_POINT_LOOKBACK_DAYS,
                    default=DEFAULT_POINT_LOOKBACK_DAYS,
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=1, max=MAX_POINT_LOOKBACK_DAYS),
                ),
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=DEFAULT_SCAN_INTERVAL,
                ): cv.time_period,
                vol.Optional(CONF_THERMOSTATS, default=[]): vol.All(
                    cv.ensure_list,
                    [THERMOSTAT_SCHEMA],
                ),
                vol.Optional(CONF_SENSORS, default=[]): vol.All(
                    cv.ensure_list,
                    [SENSOR_SCHEMA],
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

IMPORT_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_POINT_LOOKBACK_DAYS): vol.All(
            vol.Coerce(int),
            vol.Range(min=1, max=MAX_POINT_LOOKBACK_DAYS),
        ),
        vol.Optional(ATTR_SKIP_SYNC, default=False): cv.boolean,
    }
)

REBUILD_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_THERMOSTAT_ID): vol.Coerce(int),
        vol.Optional(ATTR_START_DATE): cv.date,
        vol.Optional(ATTR_END_DATE): cv.date,
        vol.Optional(ATTR_SKIP_SYNC, default=False): cv.boolean,
    }
)

GET_CONFIGURATION_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


class UnknownThermostatError(ValueError):
    """Raised when a service asks to import an unconfigured thermostat."""

    def __init__(self, thermostat_id: int) -> None:
        super().__init__(f"Unknown Beestat thermostat ID: {thermostat_id}")
        self.thermostat_id = thermostat_id


@dataclass(frozen=True, slots=True)
class SummaryImportPlan:
    """How summary rows should be imported for one import pass."""

    rows: list[dict[str, Any]]
    seeds: dict[str, CumulativeStatisticSeed]
    mode: str
    window_start: dt_date | None
    window_end: dt_date | None
    overlap_days: int | None
    fallback_reason: str | None


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Summary of one import pass."""

    imported_series: int
    imported_rows: int
    source_rows: int
    skipped_windows: int
    skipped_runtime_thermostat_windows: int
    skipped_runtime_sensor_windows: int
    latest_start_by_statistic_id: dict[str, str | None]
    summary_mode: str
    summary_window_start: str | None
    summary_window_end: str | None
    summary_overlap_days: int | None
    summary_fallback_reason: str | None
    cumulative_seed_count: int


class BeestatStatisticsImporter:
    """Fetch Beestat data and import derived daily statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: BeestatClient,
        coordinator: BeestatRuntimeDataCoordinator,
        *,
        point_lookback_days: int,
        local_tz: ZoneInfo,
    ) -> None:
        self._hass = hass
        self._client = client
        self._coordinator = coordinator
        self._point_lookback_days = point_lookback_days
        self._local_tz = local_tz
        self._lock = asyncio.Lock()

    async def async_import_statistics(
        self,
        *,
        point_lookback_days: int | None = None,
        skip_sync: bool = False,
        force_full_summary: bool = False,
        rebuild_start: dt_date | None = None,
        rebuild_end: dt_date | None = None,
        thermostat_id: int | None = None,
    ) -> ImportResult:
        """Sync Beestat and import external statistics."""

        async with self._lock:
            lookback_days = point_lookback_days or self._point_lookback_days
            runtime_data = await self._coordinator.async_refresh_runtime(
                skip_sync=skip_sync,
                summary_window=not force_full_summary,
            )
            _validate_thermostat_id(runtime_data, thermostat_id)
            summary_plan = await self._async_summary_import_plan(
                runtime_data,
                force_full_summary=force_full_summary,
            )
            summary_rows = _filter_summary_rows_by_thermostat(
                summary_plan.rows,
                thermostat_id,
            )
            skipped_windows: list[dict[str, Any]] = []
            thermostat_rows_by_id = await self._async_fetch_thermostat_rows(
                lookback_days,
                runtime_data,
                skipped_windows,
                start_day=rebuild_start,
                end_day=rebuild_end,
                thermostat_id=thermostat_id,
            )
            sensor_rows_by_id = await self._async_fetch_sensor_rows(
                lookback_days,
                runtime_data,
                skipped_windows,
                start_day=rebuild_start,
                end_day=rebuild_end,
                thermostat_id=thermostat_id,
            )
            series = build_statistics(
                summary_rows,
                thermostat_rows_by_id,
                sensor_rows_by_id,
                self._local_tz,
                runtime_data.config,
            )
            if summary_plan.seeds:
                series = apply_cumulative_seeds(series, summary_plan.seeds)
            if rebuild_start is not None or rebuild_end is not None:
                series = _filter_series_statistics(
                    series,
                    start_day=rebuild_start,
                    end_day=rebuild_end,
                    local_tz=self._local_tz,
                )
            series = [item for item in series if item.statistics]

            imported_rows = 0
            latest_start_by_id: dict[str, str | None] = {}
            for item in series:
                async_add_external_statistics(
                    self._hass,
                    item.metadata,
                    item.statistics,
                )
                imported_rows += len(item.statistics)
                latest_start_by_id[item.statistic_id] = _format_start(item)

            result = ImportResult(
                imported_series=len(series),
                imported_rows=imported_rows,
                source_rows=len(summary_rows)
                + sum(len(rows) for rows in thermostat_rows_by_id.values())
                + sum(len(rows) for rows in sensor_rows_by_id.values()),
                skipped_windows=len(skipped_windows),
                skipped_runtime_thermostat_windows=sum(
                    1
                    for item in skipped_windows
                    if item["resource"] == "runtime_thermostat"
                ),
                skipped_runtime_sensor_windows=sum(
                    1
                    for item in skipped_windows
                    if item["resource"] == "runtime_sensor"
                ),
                latest_start_by_statistic_id=latest_start_by_id,
                summary_mode=summary_plan.mode,
                summary_window_start=_format_day(summary_plan.window_start),
                summary_window_end=_format_day(summary_plan.window_end),
                summary_overlap_days=summary_plan.overlap_days,
                summary_fallback_reason=summary_plan.fallback_reason,
                cumulative_seed_count=len(summary_plan.seeds),
            )
            self._coordinator.async_record_import_result(
                imported_series=result.imported_series,
                imported_rows=result.imported_rows,
                source_rows=result.source_rows,
                skipped_windows=result.skipped_windows,
                skipped_runtime_thermostat_windows=(
                    result.skipped_runtime_thermostat_windows
                ),
                skipped_runtime_sensor_windows=result.skipped_runtime_sensor_windows,
                summary_mode=result.summary_mode,
                summary_window_start=result.summary_window_start,
                summary_window_end=result.summary_window_end,
                summary_overlap_days=result.summary_overlap_days,
                summary_fallback_reason=result.summary_fallback_reason,
                cumulative_seed_count=result.cumulative_seed_count,
            )
            _LOGGER.info(
                (
                    "Imported %s Beestat statistics rows across %s series; "
                    "summary_mode=%s skipped_windows=%s"
                ),
                result.imported_rows,
                result.imported_series,
                result.summary_mode,
                result.skipped_windows,
            )
            return result

    async def _async_summary_import_plan(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        force_full_summary: bool,
    ) -> SummaryImportPlan:
        cached_rows = list(runtime_data.summary_rows)
        if force_full_summary:
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan(
                rows=full_rows,
                seeds={},
                mode="full",
                window_start=None,
                window_end=None,
                overlap_days=None,
                fallback_reason="forced_full_baseline",
            )

        statistic_ids = cumulative_statistic_ids(runtime_data.config)
        if not statistic_ids:
            return SummaryImportPlan(
                rows=cached_rows,
                seeds={},
                mode="full",
                window_start=None,
                window_end=None,
                overlap_days=None,
                fallback_reason="no_cumulative_statistics",
            )

        latest_by_id = await self._async_latest_cumulative_starts(statistic_ids)
        if len(latest_by_id) != len(statistic_ids):
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan(
                rows=full_rows,
                seeds={},
                mode="full",
                window_start=None,
                window_end=None,
                overlap_days=None,
                fallback_reason="missing_latest_recorder_statistics",
            )

        latest_day = min(
            value.astimezone(self._local_tz).date() for value in latest_by_id.values()
        )
        window_start = latest_day - timedelta(days=DEFAULT_SUMMARY_OVERLAP_DAYS)
        window_end = _latest_summary_day(cached_rows) or datetime.now(
            timezone.utc
        ).astimezone(self._local_tz).date()
        if window_start > window_end:
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan(
                rows=full_rows,
                seeds={},
                mode="full",
                window_start=None,
                window_end=None,
                overlap_days=None,
                fallback_reason="empty_summary_window",
            )

        seeds = await self._async_cumulative_seeds(
            statistic_ids,
            seed_day=window_start - timedelta(days=1),
            window_start=window_start,
        )
        if len(seeds) != len(statistic_ids):
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan(
                rows=full_rows,
                seeds={},
                mode="full",
                window_start=None,
                window_end=None,
                overlap_days=None,
                fallback_reason="missing_prior_recorder_seed",
            )

        try:
            rows = await self._client.async_read_runtime_thermostat_summary(
                window_start.isoformat(),
                window_end.isoformat(),
            )
        except BeestatAuthError:
            raise
        except BeestatApiError as err:
            _LOGGER.warning(
                "Falling back to full Beestat summary baseline after windowed read failed: %s",
                self._client.redact_error(err),
            )
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan(
                rows=full_rows,
                seeds={},
                mode="full",
                window_start=None,
                window_end=None,
                overlap_days=None,
                fallback_reason="summary_window_read_failed",
            )

        return SummaryImportPlan(
            rows=rows,
            seeds=seeds,
            mode="windowed",
            window_start=window_start,
            window_end=window_end,
            overlap_days=DEFAULT_SUMMARY_OVERLAP_DAYS,
            fallback_reason=None,
        )

    async def _async_full_summary_rows(
        self,
        runtime_data: BeestatRuntimeData,
    ) -> list[dict[str, Any]]:
        if runtime_data.summary_rows_full:
            return list(runtime_data.summary_rows)
        return await self._client.async_read_id("runtime_thermostat_summary")


    async def _async_latest_cumulative_starts(
        self,
        statistic_ids: Iterable[str],
    ) -> dict[str, datetime]:
        return await get_recorder_instance(self._hass).async_add_executor_job(
            partial(_latest_cumulative_starts, self._hass, tuple(statistic_ids))
        )

    async def _async_cumulative_seeds(
        self,
        statistic_ids: Iterable[str],
        *,
        seed_day: dt_date,
        window_start: dt_date,
    ) -> dict[str, CumulativeStatisticSeed]:
        return await get_recorder_instance(self._hass).async_add_executor_job(
            partial(
                _cumulative_seeds_during_period,
                self._hass,
                tuple(statistic_ids),
                _local_midnight(seed_day, self._local_tz).astimezone(timezone.utc),
                _local_midnight(window_start, self._local_tz).astimezone(timezone.utc),
            )
        )

    async def _async_fetch_thermostat_rows(
        self,
        lookback_days: int,
        runtime_data: BeestatRuntimeData,
        skipped_windows: list[dict[str, Any]],
        *,
        start_day: dt_date | None = None,
        end_day: dt_date | None = None,
        thermostat_id: int | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        start, end = _point_window(lookback_days, self._local_tz, start_day, end_day)
        thermostat_data_end = _thermostat_data_end_map(list(runtime_data.thermostat_rows))

        rows_by_id: dict[int, list[dict[str, Any]]] = {}
        thermostat_ids = sorted(
            thermostat.thermostat_id
            for thermostat in runtime_data.config.thermostats
            if thermostat_id is None or thermostat.thermostat_id == thermostat_id
        )
        for current_thermostat_id in thermostat_ids:
            rows: list[dict[str, Any]] = []
            cap_end = min(end, thermostat_data_end.get(current_thermostat_id, end))
            if start > cap_end:
                rows_by_id[current_thermostat_id] = []
                continue
            for window_start, window_end in _iter_windows(start, cap_end):
                rows.extend(
                    await self._async_read_runtime_thermostat_window(
                        current_thermostat_id,
                        window_start,
                        window_end,
                        skipped_windows,
                    )
                )
            rows_by_id[current_thermostat_id] = _dedupe_rows(
                rows,
                id_field="thermostat_id",
            )
        return rows_by_id

    async def _async_read_runtime_thermostat_window(
        self,
        thermostat_id: int,
        start: datetime,
        end: datetime,
        skipped_windows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        try:
            return await self._client.async_read_runtime_thermostat(
                thermostat_id,
                _format_beestat_time(start),
                _format_beestat_time(end),
            )
        except BeestatAuthError:
            raise
        except BeestatApiError as err:
            if end - start > timedelta(days=1):
                midpoint = start + ((end - start) / 2)
                rows: list[dict[str, Any]] = []
                rows.extend(
                    await self._async_read_runtime_thermostat_window(
                        thermostat_id,
                        start,
                        midpoint,
                        skipped_windows,
                    )
                )
                rows.extend(
                    await self._async_read_runtime_thermostat_window(
                        thermostat_id,
                        midpoint,
                        end,
                        skipped_windows,
                    )
                )
                return rows

            skipped_windows.append(
                {
                    "resource": "runtime_thermostat",
                    "thermostat_id": thermostat_id,
                    "start": _format_beestat_time(start),
                    "end": _format_beestat_time(end),
                }
            )
            _LOGGER.warning(
                "Skipping Beestat runtime_thermostat window thermostat_id=%s start=%s end=%s: %s",
                thermostat_id,
                _format_beestat_time(start),
                _format_beestat_time(end),
                err,
            )
            return []

    async def _async_fetch_sensor_rows(
        self,
        lookback_days: int,
        runtime_data: BeestatRuntimeData,
        skipped_windows: list[dict[str, Any]],
        *,
        start_day: dt_date | None = None,
        end_day: dt_date | None = None,
        thermostat_id: int | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        start, end = _point_window(lookback_days, self._local_tz, start_day, end_day)
        sensor_to_thermostat = _sensor_thermostat_map(list(runtime_data.sensor_rows))
        thermostat_data_end = _thermostat_data_end_map(list(runtime_data.thermostat_rows))
        configured_sensor_ids = {
            sensor.sensor_id
            for sensor in runtime_data.config.sensors
            if thermostat_id is None or sensor.thermostat_id == thermostat_id
        }

        rows_by_id: dict[int, list[dict[str, Any]]] = {}
        sensor_ids = sorted(
            spec.sensor_id
            for spec in build_sensor_specs(runtime_data.config)
            if spec.sensor_id in configured_sensor_ids
        )
        for sensor_id in sensor_ids:
            rows: list[dict[str, Any]] = []
            cap_end = min(
                end,
                thermostat_data_end.get(sensor_to_thermostat.get(sensor_id), end),
            )
            if start > cap_end:
                rows_by_id[sensor_id] = []
                continue
            for window_start, window_end in _iter_windows(start, cap_end):
                rows.extend(
                    await self._async_read_runtime_sensor_window(
                        sensor_id,
                        window_start,
                        window_end,
                        skipped_windows,
                    )
                )
            rows_by_id[sensor_id] = _dedupe_rows(rows, id_field="sensor_id")
        return rows_by_id

    async def _async_read_runtime_sensor_window(
        self,
        sensor_id: int,
        start: datetime,
        end: datetime,
        skipped_windows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        try:
            return await self._client.async_read_runtime_sensor(
                sensor_id,
                _format_beestat_time(start),
                _format_beestat_time(end),
            )
        except BeestatAuthError:
            raise
        except BeestatApiError as err:
            if end - start > timedelta(days=1):
                midpoint = start + ((end - start) / 2)
                rows: list[dict[str, Any]] = []
                rows.extend(
                    await self._async_read_runtime_sensor_window(
                        sensor_id,
                        start,
                        midpoint,
                        skipped_windows,
                    )
                )
                rows.extend(
                    await self._async_read_runtime_sensor_window(
                        sensor_id,
                        midpoint,
                        end,
                        skipped_windows,
                    )
                )
                return rows

            skipped_windows.append(
                {
                    "resource": "runtime_sensor",
                    "sensor_id": sensor_id,
                    "start": _format_beestat_time(start),
                    "end": _format_beestat_time(end),
                }
            )
            _LOGGER.warning(
                "Skipping Beestat runtime_sensor window sensor_id=%s start=%s end=%s: %s",
                sensor_id,
                _format_beestat_time(start),
                _format_beestat_time(end),
                err,
            )
            return []

async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up Beestat Statistics and import YAML configuration if present."""

    async def async_handle_import_service(call: ServiceCall) -> None:
        runtime = _first_runtime(hass)
        if runtime is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_loaded_entry",
            )
        try:
            await runtime.importer.async_import_statistics(
                point_lookback_days=call.data.get(CONF_POINT_LOOKBACK_DAYS),
                skip_sync=call.data.get(ATTR_SKIP_SYNC, False),
            )
        except BeestatAuthError as err:
            runtime.coordinator.async_record_import_error(err)
            runtime.coordinator.config_entry.async_start_reauth_if_available(hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="beestat_auth_failed",
            ) from err
        except BeestatApiError as err:
            runtime.coordinator.async_record_import_error(err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="beestat_request_failed",
            ) from err
        except Exception as err:
            runtime.coordinator.async_record_import_error(err)
            _LOGGER.exception("Unexpected Beestat statistics import service failure")
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="statistics_import_failed",
            ) from err

    async def async_handle_get_configuration(
        call: ServiceCall,
    ) -> ServiceResponse:
        entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY_ID])
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
            or (runtime := getattr(entry, "runtime_data", None)) is None
            or runtime.coordinator.data is None
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_loaded_entry",
            )
        return configuration_response(
            entry_id=entry.entry_id,
            entry_data=entry.data,
            entry_options=entry.options,
            config=runtime.coordinator.data.config,
            point_lookback_days=_entry_point_lookback_days(entry),
            scan_interval_seconds=_entry_scan_interval_seconds(entry),
        )

    async def async_handle_rebuild_service(call: ServiceCall) -> None:
        runtime = _first_runtime(hass)
        if runtime is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_loaded_entry",
            )
        start_date = call.data.get(ATTR_START_DATE)
        end_date = call.data.get(ATTR_END_DATE)
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_rebuild_date_range",
            )
        try:
            await runtime.importer.async_import_statistics(
                skip_sync=call.data.get(ATTR_SKIP_SYNC, False),
                force_full_summary=True,
                rebuild_start=start_date,
                rebuild_end=end_date,
                thermostat_id=call.data.get(CONF_THERMOSTAT_ID),
            )
        except UnknownThermostatError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_thermostat_id",
                translation_placeholders={
                    "thermostat_id": str(err.thermostat_id),
                },
            ) from err
        except BeestatAuthError as err:
            runtime.coordinator.async_record_import_error(err)
            runtime.coordinator.config_entry.async_start_reauth_if_available(hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="beestat_auth_failed",
            ) from err
        except BeestatApiError as err:
            runtime.coordinator.async_record_import_error(err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="beestat_request_failed",
            ) from err
        except Exception as err:
            runtime.coordinator.async_record_import_error(err)
            _LOGGER.exception("Unexpected Beestat statistics rebuild service failure")
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="statistics_import_failed",
            ) from err
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_STATISTICS,
        async_handle_import_service,
        schema=IMPORT_SERVICE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CONFIGURATION,
        async_handle_get_configuration,
        schema=GET_CONFIGURATION_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REBUILD_STATISTICS,
        async_handle_rebuild_service,
        schema=REBUILD_SERVICE_SCHEMA,
    )

    if conf := config.get(DOMAIN):
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                **entry_data_from_yaml(conf),
                **entry_options_from_yaml(conf),
            },
        )

    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> bool:
    """Set up Beestat Statistics from a config entry."""

    local_tz = ZoneInfo(str(hass.config.time_zone))
    client = BeestatClient(
        async_get_clientsession(hass),
        entry.data[CONF_API_KEY],
        entry.data[CONF_API_BASE],
    )
    coordinator = BeestatRuntimeDataCoordinator(
        hass,
        entry,
        client,
        local_tz=local_tz,
    )
    importer = BeestatStatisticsImporter(
        hass,
        client,
        coordinator,
        point_lookback_days=_entry_point_lookback_days(entry),
        local_tz=local_tz,
    )
    runtime = BeestatStatisticsRuntime(
        client=client,
        coordinator=coordinator,
        importer=importer,
        scan_interval=timedelta(seconds=_entry_scan_interval_seconds(entry)),
    )
    entry.runtime_data = runtime

    await coordinator.async_config_entry_first_refresh()
    async_register_service_device(hass, entry)
    _migrate_legacy_unique_ids(hass, entry, coordinator.data)
    _async_enable_default_problem_entities(hass, entry, coordinator.data)
    _async_migrate_homekit_device_assignments(hass, entry, coordinator.data)
    _async_update_override_issues(hass, entry)

    scheduled_import_unavailable_logged = False

    async def async_run_scheduled_import(*, skip_sync: bool = False) -> None:
        nonlocal scheduled_import_unavailable_logged
        try:
            await importer.async_import_statistics(skip_sync=skip_sync)
        except BeestatAuthError as err:
            coordinator.config_entry.async_start_reauth_if_available(hass)
            coordinator.async_record_import_error(err)
            if not scheduled_import_unavailable_logged:
                _LOGGER.info("Beestat statistics import is unavailable: %s", err)
                scheduled_import_unavailable_logged = True
        except Exception as err:
            coordinator.async_record_import_error(err)
            if not scheduled_import_unavailable_logged:
                _LOGGER.info(
                    "Beestat statistics import is unavailable: %s",
                    err,
                    exc_info=True,
                )
                scheduled_import_unavailable_logged = True
        else:
            if scheduled_import_unavailable_logged:
                _LOGGER.info("Beestat statistics import is available again")
                scheduled_import_unavailable_logged = False

    @callback
    def async_schedule_import(_event_or_time: Any) -> None:
        """Schedule an import from a Home Assistant event-loop callback."""

        entry.async_create_background_task(
            hass,
            async_run_scheduled_import(),
            f"{DOMAIN}_scheduled_import",
        )

    filter_changed_entity_ids = _filter_changed_entity_ids(coordinator.data)
    if filter_changed_entity_ids:
        entry.async_on_unload(
            async_track_state_change_event(
                hass,
                filter_changed_entity_ids,
                async_schedule_import,
            )
        )

    remove_interval = async_track_time_interval(
        hass,
        async_schedule_import,
        runtime.scan_interval,
    )
    entry.async_on_unload(remove_interval)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass,
        async_run_scheduled_import(skip_sync=True),
        f"{DOMAIN}_startup_import",
        eager_start=False,
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate Beestat Statistics config entries."""

    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Cannot migrate Beestat Statistics config entry from version %s.%s",
            entry.version,
            entry.minor_version,
        )
        return False

    migrated_data, migrated_options = migrate_entry_payload(entry.data, entry.options)
    if (
        entry.version != CONFIG_ENTRY_VERSION
        or entry.minor_version != CONFIG_ENTRY_MINOR_VERSION
        or migrated_data != dict(entry.data)
        or migrated_options != dict(entry.options)
    ):
        hass.config_entries.async_update_entry(
            entry,
            data=migrated_data,
            options=migrated_options,
            version=CONFIG_ENTRY_VERSION,
            minor_version=CONFIG_ENTRY_MINOR_VERSION,
        )

    _LOGGER.debug(
        "Migrated Beestat Statistics config entry to version %s.%s",
        CONFIG_ENTRY_VERSION,
        CONFIG_ENTRY_MINOR_VERSION,
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> bool:
    """Unload a Beestat Statistics config entry."""

    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow stale Beestat-only fallback devices to be removed manually."""

    runtime: BeestatStatisticsRuntime | None = getattr(entry, "runtime_data", None)
    data = runtime.coordinator.data if runtime is not None else None
    if data is None:
        return False

    beestat_identifiers = {
        identifier
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN
    }
    if not beestat_identifiers:
        return False

    return beestat_identifiers.isdisjoint(_current_beestat_device_identifiers(data))


def _current_beestat_device_identifiers(
    data: BeestatRuntimeData,
) -> set[tuple[str, str]]:
    """Return Beestat-owned fallback identifiers currently present in live data."""

    identifiers = {(DOMAIN, "service")}
    identifiers.update(
        (DOMAIN, f"thermostat_{thermostat.thermostat_id}")
        for thermostat in data.config.thermostats
        if not thermostat.device_identifiers and not thermostat.device_connections
    )
    identifiers.update(
        (DOMAIN, f"sensor_{sensor.sensor_id}")
        for sensor in data.config.sensors
        if not sensor.device_identifiers and not sensor.device_connections
    )
    return identifiers


@callback
def _migrate_legacy_unique_ids(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    data: BeestatRuntimeData | None,
) -> None:
    """Migrate slug-derived entity unique IDs to stable Beestat ID keys."""

    if data is None:
        return

    registry = er.async_get(hass)
    mappings = _legacy_unique_id_migration(data)
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        new_unique_id = mappings.get(entity_entry.unique_id)
        if new_unique_id is None or new_unique_id == entity_entry.unique_id:
            continue
        existing_entity_id = registry.async_get_entity_id(
            entity_entry.domain,
            entity_entry.platform,
            new_unique_id,
        )
        if existing_entity_id not in (None, entity_entry.entity_id):
            _LOGGER.warning(
                "Skipping Beestat unique ID migration for %s because %s already uses %s",
                entity_entry.entity_id,
                existing_entity_id,
                new_unique_id,
            )
            continue
        registry.async_update_entity(
            entity_entry.entity_id,
            new_unique_id=new_unique_id,
        )


@callback
def _async_enable_default_problem_entities(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    data: BeestatRuntimeData | None,
) -> None:
    """Enable and rename stale diagnostics disabled by earlier releases."""

    if data is None:
        return

    target_entity_ids = {
        thermostat_entity_unique_id(thermostat.thermostat_id, suffix): (
            _default_problem_entity_id(thermostat, suffix)
        )
        for thermostat in data.config.thermostats
        for suffix in _DEFAULT_ENABLED_PROBLEM_ENTITY_SUFFIXES
    }
    registry = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        target_entity_id = target_entity_ids.get(entity_entry.unique_id)
        if target_entity_id is None:
            continue
        updates: dict[str, Any] = {}
        if entity_entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
            updates["disabled_by"] = None
        if (
            entity_entry.entity_id != target_entity_id
            and _is_generic_problem_entity_id(entity_entry.entity_id)
            and registry.async_get(target_entity_id) is None
        ):
            updates["new_entity_id"] = target_entity_id
        if not updates:
            continue
        registry.async_update_entity(
            entity_entry.entity_id,
            **updates,
        )
        _LOGGER.info(
            "Repaired Beestat diagnostic entity %s after default visibility change",
            entity_entry.entity_id,
        )


def _default_problem_entity_id(
    thermostat: ConfiguredThermostat,
    suffix: str,
) -> str:
    if thermostat.device_identifiers or thermostat.device_connections:
        object_id = f"{thermostat.slug}_{suffix}"
    else:
        object_id = f"beestat_{thermostat.slug}_{suffix}"
    return f"binary_sensor.{object_id}"


def _is_generic_problem_entity_id(entity_id: str) -> bool:
    object_id = entity_id.split(".", 1)[-1]
    return object_id.endswith("_problem") or object_id.endswith("_problem_2")


@callback
def _async_migrate_homekit_device_assignments(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    data: BeestatRuntimeData | None,
) -> None:
    """Move existing Beestat entities to mapped HomeKit devices."""

    if data is None:
        return

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    target_device_ids = _mapped_unique_id_device_ids(device_registry, data)
    for entity_entry in er.async_entries_for_config_entry(
        entity_registry,
        entry.entry_id,
    ):
        target_device_id = target_device_ids.get(entity_entry.unique_id)
        if target_device_id is None or entity_entry.device_id == target_device_id:
            continue
        entity_registry.async_update_entity(
            entity_entry.entity_id,
            device_id=target_device_id,
        )
        _LOGGER.info(
            "Moved Beestat entity %s to mapped HomeKit/Ecobee device",
            entity_entry.entity_id,
        )

    current_fallback_identifiers = _current_beestat_device_identifiers(data)
    for device_entry in dr.async_entries_for_config_entry(
        device_registry,
        entry.entry_id,
    ):
        beestat_identifiers = {
            identifier
            for identifier in device_entry.identifiers
            if identifier[0] == DOMAIN
        }
        if not beestat_identifiers:
            continue
        if not beestat_identifiers.isdisjoint(current_fallback_identifiers):
            continue
        device_registry.async_remove_device(device_entry.id)
        _LOGGER.info(
            "Removed stale Beestat fallback device %s after HomeKit mapping",
            device_entry.name,
        )


def _mapped_unique_id_device_ids(
    device_registry: dr.DeviceRegistry,
    data: BeestatRuntimeData,
) -> dict[str, str]:
    """Return Beestat unique IDs that should attach to HomeKit devices."""

    mappings: dict[str, str] = {}
    for thermostat in data.config.thermostats:
        device_id = _mapped_device_id(
            device_registry,
            thermostat.device_identifiers,
            thermostat.device_connections,
        )
        if device_id is None:
            continue
        for suffix in _THERMOSTAT_ENTITY_SUFFIXES:
            mappings[thermostat_entity_unique_id(thermostat.thermostat_id, suffix)] = (
                device_id
            )
        mappings[
            thermostat_entity_unique_id(thermostat.thermostat_id, "active_alert")
        ] = device_id
        mappings[
            thermostat_entity_unique_id(
                thermostat.thermostat_id,
                "runtime_summary_stale",
            )
        ] = device_id
        mappings[
            thermostat_entity_unique_id(thermostat.thermostat_id, "cloud_data_stale")
        ] = device_id

    for sensor in data.config.sensors:
        device_id = _mapped_device_id(
            device_registry,
            sensor.device_identifiers,
            sensor.device_connections,
        )
        if device_id is None:
            continue
        mappings[sensor_entity_unique_id(sensor.sensor_id, "sensor_in_use")] = device_id
    return mappings


def _mapped_device_id(
    device_registry: dr.DeviceRegistry,
    identifiers: tuple[tuple[str, str], ...],
    connections: tuple[tuple[str, str], ...],
) -> str | None:
    if not identifiers and not connections:
        return None
    device = device_registry.async_get_device(
        identifiers=set(identifiers) or None,
        connections=set(connections) or None,
    )
    return device.id if device is not None else None


def _legacy_unique_id_migration(data: BeestatRuntimeData) -> dict[str, str]:
    """Return old slug-based unique IDs mapped to stable ID-based values."""

    mappings = dict(_GLOBAL_UNIQUE_ID_MIGRATION)
    for thermostat in data.config.thermostats:
        old_prefix = f"beestat_{thermostat.slug}_hvac"
        for suffix in _THERMOSTAT_ENTITY_SUFFIXES:
            new_unique_id = thermostat_entity_unique_id(
                thermostat.thermostat_id,
                suffix,
            )
            mappings[f"{old_prefix}_{suffix}"] = new_unique_id
            mappings[f"beestat_{new_unique_id}"] = new_unique_id
        active_alert_unique_id = thermostat_entity_unique_id(
            thermostat.thermostat_id,
            "active_alert",
        )
        mappings[f"{old_prefix}_active_alert"] = active_alert_unique_id
        mappings[f"beestat_{active_alert_unique_id}"] = active_alert_unique_id
    for sensor in data.config.sensors:
        new_unique_id = sensor_entity_unique_id(
            sensor.sensor_id,
            "sensor_in_use",
        )
        mappings[f"beestat_{sensor.slug}_sensor_in_use"] = new_unique_id
        mappings[f"beestat_{new_unique_id}"] = new_unique_id
    return mappings


def _filter_changed_entity_ids(data: BeestatRuntimeData | None) -> list[str]:
    if data is None:
        return []
    return sorted(
        {
            thermostat.filter_changed_entity_id
            for thermostat in data.config.thermostats
            if thermostat.filter_changed_entity_id is not None
        }
    )


@callback
def _async_update_override_issues(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> None:
    _async_update_missing_override_entity_issue(hass, entry)
    _async_update_invalid_override_domain_issue(hass, entry)


@callback
def _async_update_missing_override_entity_issue(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> None:
    missing = _missing_override_entity_ids(hass, entry_runtime_config_data(entry))
    if not missing:
        ir.async_delete_issue(hass, DOMAIN, _MISSING_OVERRIDE_ENTITIES_ISSUE_ID)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        _MISSING_OVERRIDE_ENTITIES_ISSUE_ID,
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_MISSING_OVERRIDE_ENTITIES_ISSUE_ID,
        translation_placeholders={
            "entities": ", ".join(missing),
        },
    )


@callback
def _async_update_invalid_override_domain_issue(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> None:
    errors = configured_override_entity_domain_errors(entry_runtime_config_data(entry))
    if not errors:
        ir.async_delete_issue(hass, DOMAIN, _INVALID_OVERRIDE_ENTITY_DOMAINS_ISSUE_ID)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        _INVALID_OVERRIDE_ENTITY_DOMAINS_ISSUE_ID,
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_INVALID_OVERRIDE_ENTITY_DOMAINS_ISSUE_ID,
        translation_placeholders={
            "entities": ", ".join(errors),
        },
    )


def _missing_override_entity_ids(
    hass: HomeAssistant,
    config_data: Mapping[str, Any],
) -> tuple[str, ...]:
    registry = er.async_get(hass)
    return tuple(
        entity_id
        for entity_id in configured_override_entity_ids(config_data)
        if hass.states.get(entity_id) is None and registry.async_get(entity_id) is None
    )


def _first_runtime(hass: HomeAssistant) -> BeestatStatisticsRuntime | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        runtime: BeestatStatisticsRuntime | None = getattr(entry, "runtime_data", None)
        if runtime is not None:
            return runtime
    return None


def _latest_cumulative_starts(
    hass: HomeAssistant,
    statistic_ids: tuple[str, ...],
) -> dict[str, datetime]:
    """Read the latest Recorder row for each cumulative statistic."""

    latest: dict[str, datetime] = {}
    for statistic_id in statistic_ids:
        rows = get_last_statistics(
            hass,
            1,
            statistic_id,
            False,
            {"state", "sum"},
        ).get(statistic_id, [])
        if not rows:
            continue
        if (start := _row_start_datetime(rows[-1])) is not None:
            latest[statistic_id] = start
    return latest


def _cumulative_seeds_during_period(
    hass: HomeAssistant,
    statistic_ids: tuple[str, ...],
    seed_start: datetime,
    window_start: datetime,
) -> dict[str, CumulativeStatisticSeed]:
    """Read Recorder cumulative values immediately before a window."""

    rows_by_id = statistics_during_period(
        hass,
        seed_start,
        window_start,
        set(statistic_ids),
        "hour",
        None,
        {"state", "sum"},
    )
    seeds: dict[str, CumulativeStatisticSeed] = {}
    for statistic_id, rows in rows_by_id.items():
        if not rows:
            continue
        row = rows[-1]
        start = _row_start_datetime(row)
        state = _row_float(row.get("state"))
        sum_value = _row_float(row.get("sum"))
        if start is None or state is None or sum_value is None:
            continue
        seeds[statistic_id] = CumulativeStatisticSeed(
            start=start,
            state=state,
            sum=sum_value,
        )
    return seeds


def _validate_thermostat_id(
    runtime_data: BeestatRuntimeData,
    thermostat_id: int | None,
) -> None:
    if thermostat_id is None:
        return
    configured_ids = {
        thermostat.thermostat_id for thermostat in runtime_data.config.thermostats
    }
    if thermostat_id not in configured_ids:
        raise UnknownThermostatError(thermostat_id)


def _filter_summary_rows_by_thermostat(
    rows: list[dict[str, Any]],
    thermostat_id: int | None,
) -> list[dict[str, Any]]:
    if thermostat_id is None:
        return rows
    return [
        row
        for row in rows
        if _row_int(row, "thermostat_id", "id") == thermostat_id
    ]


def _filter_series_statistics(
    series: list[StatisticsSeries],
    *,
    start_day: dt_date | None,
    end_day: dt_date | None,
    local_tz: ZoneInfo,
) -> list[StatisticsSeries]:
    filtered: list[StatisticsSeries] = []
    for item in series:
        stats = [
            row
            for row in item.statistics
            if _statistic_row_in_range(
                row,
                start_day=start_day,
                end_day=end_day,
                local_tz=local_tz,
            )
        ]
        filtered.append(
            StatisticsSeries(
                metadata=item.metadata,
                statistics=stats,
                source_rows=item.source_rows,
            )
        )
    return filtered


def _statistic_row_in_range(
    row: dict[str, Any],
    *,
    start_day: dt_date | None,
    end_day: dt_date | None,
    local_tz: ZoneInfo,
) -> bool:
    start = row.get("start")
    if not isinstance(start, datetime):
        return False
    local_day = start.astimezone(local_tz).date()
    if start_day is not None and local_day < start_day:
        return False
    if end_day is not None and local_day > end_day:
        return False
    return True


def _point_window(
    lookback_days: int,
    local_tz: ZoneInfo,
    start_day: dt_date | None,
    end_day: dt_date | None,
) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    if end_day is not None:
        end = _local_midnight(end_day + timedelta(days=1), local_tz).astimezone(
            timezone.utc
        )
    if start_day is None:
        local_start_day = end.astimezone(local_tz).date() - timedelta(
            days=lookback_days,
        )
    else:
        local_start_day = start_day
    start = _local_midnight(local_start_day, local_tz).astimezone(timezone.utc)
    return start, end


def _local_midnight(local_day: dt_date, local_tz: ZoneInfo) -> datetime:
    return datetime.combine(local_day, time.min, local_tz)


def _latest_summary_day(rows: list[dict[str, Any]]) -> dt_date | None:
    days = [_row_date(row.get("date")) for row in rows]
    valid_days = [item for item in days if item is not None]
    return max(valid_days) if valid_days else None


def _row_date(value: Any) -> dt_date | None:
    if value in (None, ""):
        return None
    try:
        return dt_date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _row_start_datetime(row: dict[str, Any]) -> datetime | None:
    value = row.get("start")
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromtimestamp(float(value), timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_float(value: Any) -> float | None:
    if value in (None, "", "unknown", "unavailable"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_day(value: dt_date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _entry_point_lookback_days(entry: BeestatStatisticsConfigEntry) -> int:
    return int(
        entry.options.get(
            CONF_POINT_LOOKBACK_DAYS,
            DEFAULT_POINT_LOOKBACK_DAYS,
        )
    )


def _entry_scan_interval_seconds(entry: BeestatStatisticsConfigEntry) -> int:
    return max(
        int(
            entry.options.get(
                CONF_SCAN_INTERVAL_SECONDS,
                DEFAULT_SCAN_INTERVAL_SECONDS,
            )
        ),
        MIN_SCAN_INTERVAL_SECONDS,
    )


def _iter_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    current = start
    while current <= end:
        window_end = min(current + timedelta(days=MAX_WINDOW_DAYS), end)
        windows.append((current, window_end))
        if window_end >= end:
            break
        current = window_end
    return windows


def _format_beestat_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _sensor_thermostat_map(rows: list[dict[str, Any]]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for row in rows:
        sensor_id = _row_int(row, "id", "sensor_id")
        thermostat_id = _row_int(row, "thermostat_id")
        if sensor_id is not None and thermostat_id is not None:
            mapping[sensor_id] = thermostat_id
    return mapping


def _thermostat_data_end_map(rows: list[dict[str, Any]]) -> dict[int, datetime]:
    mapping: dict[int, datetime] = {}
    for row in rows:
        thermostat_id = _row_int(row, "id", "thermostat_id")
        data_end = _parse_beestat_time(row.get("data_end"))
        if thermostat_id is not None and data_end is not None:
            mapping[thermostat_id] = data_end
    return mapping


def _row_int(row: dict[str, Any], *fields: str) -> int | None:
    for field in fields:
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _parse_beestat_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dedupe_rows(rows: list[dict[str, Any]], *, id_field: str) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        if row.get("runtime_sensor_id") is not None:
            key = ("runtime_sensor_id", row["runtime_sensor_id"])
        elif row.get("runtime_thermostat_id") is not None:
            key = ("runtime_thermostat_id", row["runtime_thermostat_id"])
        elif row.get("timestamp") is not None:
            key = (id_field, row.get(id_field), "timestamp", row["timestamp"])
        else:
            key = ("row", tuple(sorted((str(key), str(value)) for key, value in row.items())))
        deduped[key] = row
    return sorted(
        deduped.values(),
        key=lambda row: (str(row.get("timestamp", "")), str(row.get(id_field, ""))),
    )


def _format_start(series: StatisticsSeries) -> str | None:
    latest = series.latest_start
    return latest.isoformat() if latest else None

"""Import Beestat HVAC data into Home Assistant external statistics."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from datetime import date as dt_date
from functools import partial
from math import isfinite
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.components.recorder.models.statistics import (
    StatisticData,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigEntryState
from homeassistant.const import (
    CONF_API_KEY,
    CONF_SCAN_INTERVAL,
    EVENT_CORE_CONFIG_UPDATE,
    Platform,
)
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import (
    ConfigEntryError,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.recorder import get_instance as get_recorder_instance

from .api import (
    BeestatApiError,
    BeestatAuthError,
    BeestatClient,
    BeestatPermanentError,
    exception_fingerprint,
)
from .config_model import (
    BeestatConfig,
    ConfiguredSensor,
    ConfiguredThermostat,
    build_beestat_config,
    configured_mapping_device_conflicts,
    configured_override_entity_domain_errors,
    configured_override_entity_ids,
    configured_unresolved_entity_ids,
)
from .config_model import (
    build_sensor_statistics as build_sensor_specs,
)
from .config_payload import (
    entry_data_from_yaml,
    entry_options_from_yaml,
    entry_runtime_config_data,
    migrate_entry_payload,
    normalize_point_lookback_days,
    normalize_scan_interval_seconds,
)
from .config_rows import positive_resource_id
from .configuration import configuration_response
from .const import (
    API_BASE,
    ATTR_CHANGED_AT,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_END,
    ATTR_END_DATE,
    ATTR_EPOCH_START,
    ATTR_EXPECTED_CHANGED_AT,
    ATTR_EXPECTED_CHANGED_DATE,
    ATTR_EXPECTED_REQUEST_ID,
    ATTR_EXPECTED_REVISION,
    ATTR_PREVIEW_DIGEST,
    ATTR_REQUEST_ID,
    ATTR_SKIP_SYNC,
    ATTR_START,
    ATTR_START_DATE,
    ATTR_STATISTIC_IDS,
    CONF_API_BASE,
    CONF_CLIMATE_ENTITY_ID,
    CONF_ENABLED,
    CONF_FILTER_CHANGED_DATE,
    CONF_FILTER_CHANGED_ENTITY_ID,
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
    DEFAULT_SUMMARY_OVERLAP_DAYS,
    DETAILED_RUNTIME_FIELDS,
    DOMAIN,
    MAX_FILTER_LIFETIME_RUNTIME_HOURS,
    MAX_FILTER_MAX_AGE_DAYS,
    MAX_FILTER_NOTICE_DAYS,
    MAX_POINT_LOOKBACK_DAYS,
    MAX_WINDOW_DAYS,
    RUNTIME_FIELD_GROUPS,
    SERVICE_GET_CONFIGURATION,
    SERVICE_GET_HOURLY_COVERAGE,
    SERVICE_GET_RAW_POINTS,
    SERVICE_IMPORT_STATISTICS,
    SERVICE_REBUILD_STATISTICS,
    SERVICE_RECORD_FILTER_CHANGE,
    SERVICE_REPAIR_FILTER_CHANGE_BOUNDARY,
    SERVICE_SELECT_HOURLY_STATISTICS,
    STATISTIC_SOURCE,
    SUMMARY_MEAN_STATISTICS,
    SUMMARY_SUM_STATISTICS,
    THERMOSTAT_POINT_STATISTICS,
    sensor_entity_unique_id,
    thermostat_entity_unique_id,
)
from .coordinator import (
    BeestatRuntimeData,
    BeestatRuntimeDataCoordinator,
    TemporalContext,
)
from .entity import (
    async_register_service_device,
    async_remove_cross_integration_device_ownership,
    is_beestat_only_device,
)
from .entity_reference import (
    configured_entity_references,
    entity_reference_matches_entry,
)
from .entry_options import (
    FilterChangeConflictError,
    async_mark_filter_changed,
    resolve_filter_change_timestamp,
    saved_filter_boundary,
)
from .filter_forecast import build_filter_forecast, filter_forecast_quality_attributes
from .hourly_history_contract import (
    MAX_SOURCE_BYTES,
    SERVICE_APPLY_HOURLY_HISTORY,
    SERVICE_PLAN_HOURLY_HISTORY,
    SERVICE_STAGE_HOURLY_SOURCE,
)
from .hourly_history_contract import (
    digest as history_digest,
)
from .hourly_history_contract import (
    quantity_id as history_quantity_id,
)
from .hourly_history_query import history_response
from .hourly_history_runtime import async_refresh_history
from .hourly_history_service import (
    APPLY_HISTORY_SCHEMA,
    COVERAGE_HISTORY_SCHEMA,
    PLAN_HISTORY_SCHEMA,
    STAGE_HISTORY_SCHEMA,
)
from .hourly_history_values import build_history_series
from .hourly_import import (
    HourlyImportError,
    HourlyImportManager,
    HourlyReconciliationError,
)
from .hourly_recorder import HourlyRecorderError
from .hourly_sources import stage_source
from .hourly_statistics import HourlySeries, build_hourly_statistics
from .hourly_storage import HourlyStorageError
from .import_evidence import SkippedWindowEvidence, SkippedWindowResource
from .issues import (
    async_set_insecure_api_base_issue,
    async_set_yaml_connection_change_issue,
)
from .raw_points import (
    RawPointIdentity,
    RawPointRequest,
    async_read_raw_points,
    parse_raw_point_request,
    validate_raw_point_identity,
)
from .runtime import BeestatStatisticsConfigEntry, BeestatStatisticsRuntime
from .source_identity import is_thermostat_identity_source
from .statistics_builder import (
    CumulativeStatisticSeed,
    StatisticsSeries,
    apply_cumulative_seeds,
    build_statistics,
    cumulative_statistic_ids,
    detailed_runtime_statistic_ids,
)
from .task_coalescer import CoalescingTaskScheduler
from .url_validation import normalize_api_base

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
_MAPPING_DEVICE_CONFLICTS_ISSUE_ID = "mapping_device_conflicts"
_IMPORT_TEMPORAL_CONTEXT_ATTEMPTS = 3
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
        vol.Optional(CONF_ENABLED, default=True): cv.boolean,
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
        vol.Optional(CONF_ENABLED, default=True): cv.boolean,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Schema(
            {
                vol.Required(CONF_API_KEY): vol.All(
                    cv.string,
                    str.strip,
                    vol.Length(min=1),
                ),
                vol.Optional(CONF_API_BASE, default=API_BASE): vol.All(
                    cv.url,
                    normalize_api_base,
                ),
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


def _hourly_revision(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise vol.Invalid("The expected revision must be a non-negative integer")
    return value


SELECT_HOURLY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_EPOCH_START): cv.datetime,
        vol.Required(ATTR_STATISTIC_IDS): vol.All([cv.string], vol.Length(min=1)),
        vol.Required(ATTR_EXPECTED_REVISION): _hourly_revision,
        vol.Optional(ATTR_PREVIEW_DIGEST): cv.string,
    }
)

GET_HOURLY_COVERAGE_SERVICE_SCHEMA = vol.Any(
    COVERAGE_HISTORY_SCHEMA,
    vol.Schema(
        {
            vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Required(ATTR_START): cv.datetime,
            vol.Required(ATTR_END): cv.datetime,
            vol.Optional(ATTR_STATISTIC_IDS): vol.All([cv.string], vol.Length(min=1)),
        }
    ),
)

GET_RAW_POINTS_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required("resource"): vol.In(("runtime_thermostat", "runtime_sensor")),
        vol.Required("resource_id"): int,
        vol.Required(ATTR_START): cv.datetime,
        vol.Required(ATTR_END): cv.datetime,
    }
)

REPAIR_FILTER_CHANGE_BOUNDARY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(CONF_THERMOSTAT_ID): vol.Coerce(int),
        vol.Required(ATTR_CHANGED_AT): cv.datetime,
    }
)


def _positive_thermostat_id(value: Any) -> int:
    if (thermostat_id := positive_resource_id(value)) is None:
        raise vol.Invalid("thermostat_id must be an exact positive integer")
    return thermostat_id


RECORD_FILTER_CHANGE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(CONF_THERMOSTAT_ID): _positive_thermostat_id,
        vol.Required(ATTR_CHANGED_AT): cv.datetime,
        vol.Required(ATTR_EXPECTED_CHANGED_AT): vol.Any(None, cv.datetime),
        vol.Required(ATTR_EXPECTED_CHANGED_DATE): vol.Any(None, cv.date),
        vol.Required(ATTR_EXPECTED_REQUEST_ID): vol.Any(
            None, vol.All(cv.string, vol.Length(min=1, max=128))
        ),
        vol.Required(ATTR_REQUEST_ID): vol.All(cv.string, vol.Length(min=1, max=128)),
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

    @classmethod
    def full(
        cls,
        rows: list[dict[str, Any]],
        *,
        fallback_reason: str,
    ) -> SummaryImportPlan:
        """Build an unseeded complete baseline with its existing fallback reason."""

        return cls(
            rows=rows,
            seeds={},
            mode="full",
            window_start=None,
            window_end=None,
            overlap_days=None,
            fallback_reason=fallback_reason,
        )


@dataclass(frozen=True, slots=True)
class PreparedImport:
    """One complete statistics import prepared before Recorder effects."""

    summary_plan: SummaryImportPlan
    summary_rows: list[dict[str, Any]]
    skipped_windows: SkippedWindowEvidence
    thermostat_rows_by_id: dict[int, list[dict[str, Any]]]
    sensor_rows_by_id: dict[int, list[dict[str, Any]]]
    series: list[StatisticsSeries]


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Summary of one import pass."""

    imported_series: int
    imported_rows: int
    source_rows: int
    skipped_windows: int
    skipped_runtime_thermostat_windows: int
    skipped_runtime_sensor_windows: int
    skipped_window_examples: tuple[dict[str, str], ...]
    latest_start_by_statistic_id: dict[str, str | None]
    summary_mode: str
    summary_window_start: str | None
    summary_window_end: str | None
    summary_overlap_days: int | None
    summary_fallback_reason: str | None
    cumulative_seed_count: int
    legacy_imported_series: int = 0
    legacy_imported_rows: int = 0
    hourly_imported_series: int | None = 0
    hourly_imported_rows: int | None = 0
    hourly_blocked_reason: str | None = None
    coverage_incomplete: bool = False


@dataclass(frozen=True, slots=True)
class PreparedHourlyImport:
    """Detached hourly source evidence prepared before durable Recorder effects."""

    series: tuple[HourlySeries, ...]
    identity: dict[str, Any]
    source_rows: int
    skipped_windows: SkippedWindowEvidence
    ordinary_start: datetime | None
    eligible_resources: dict[str, dict[str, Any]]


def _combined_import_result(
    legacy: ImportResult | None,
    hourly: ImportResult | None,
    *,
    has_hourly: bool,
    hourly_blocked_reason: str | None,
) -> ImportResult:
    """Report acknowledged counts separately from uncertain hourly effects."""

    parts = [result for result in (legacy, hourly) if result is not None]
    return ImportResult(
        imported_series=sum(result.imported_series for result in parts),
        imported_rows=sum(result.imported_rows for result in parts),
        source_rows=sum(result.source_rows for result in parts),
        skipped_windows=sum(result.skipped_windows for result in parts),
        skipped_runtime_thermostat_windows=sum(
            result.skipped_runtime_thermostat_windows for result in parts
        ),
        skipped_runtime_sensor_windows=sum(
            result.skipped_runtime_sensor_windows for result in parts
        ),
        skipped_window_examples=tuple(
            example for result in parts for example in result.skipped_window_examples
        ),
        latest_start_by_statistic_id={
            key: value
            for result in parts
            for key, value in result.latest_start_by_statistic_id.items()
        },
        summary_mode=(
            "mixed"
            if has_hourly and legacy is not None
            else "hourly"
            if has_hourly
            else legacy.summary_mode
            if legacy is not None
            else "full"
        ),
        summary_window_start=legacy.summary_window_start if legacy else None,
        summary_window_end=legacy.summary_window_end if legacy else None,
        summary_overlap_days=legacy.summary_overlap_days if legacy else None,
        summary_fallback_reason=(
            hourly_blocked_reason
            or (hourly.summary_fallback_reason if hourly else None)
            or (legacy.summary_fallback_reason if legacy else None)
        ),
        cumulative_seed_count=legacy.cumulative_seed_count if legacy else 0,
        legacy_imported_series=legacy.imported_series if legacy else 0,
        legacy_imported_rows=legacy.imported_rows if legacy else 0,
        hourly_imported_series=(
            hourly.imported_series if hourly else None if hourly_blocked_reason else 0
        ),
        hourly_imported_rows=(
            hourly.imported_rows if hourly else None if hourly_blocked_reason else 0
        ),
        hourly_blocked_reason=hourly_blocked_reason,
        coverage_incomplete=bool(hourly_blocked_reason)
        or any(result.coverage_incomplete for result in parts),
    )


class BeestatStatisticsImporter:
    """Fetch Beestat data and import derived daily statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: BeestatClient,
        coordinator: BeestatRuntimeDataCoordinator,
        *,
        point_lookback_days: int,
    ) -> None:
        self._hass = hass
        self._client = client
        self._coordinator = coordinator
        self._point_lookback_days = point_lookback_days
        self._lock = asyncio.Lock()
        self._unloaded = False
        self._history_worker: asyncio.Task[None] | None = None
        self.hourly = HourlyImportManager(hass, coordinator.beestat_config_entry)
        coordinator.beestat_config_entry.async_on_unload(self._async_unload)

    @callback
    def _async_unload(self) -> None:
        """Prevent old service references from starting work after unload."""

        self._unloaded = True
        self.hourly.close()

    def _history_context(self) -> dict[str, Any]:
        """Detach identity/configuration without acquiring or changing anything."""
        entry = self._coordinator.beestat_config_entry
        runtime = getattr(entry, "runtime_data", None)
        data = self._coordinator.data
        if (
            self._unloaded
            or data is None
            or entry.state is not ConfigEntryState.LOADED
            or runtime is None
            or runtime.importer is not self
            or runtime.client is not self._client
        ):
            raise ValueError("history_entry_unavailable")
        temporal = self._coordinator.capture_temporal_context()
        identity = _writer_identity(entry, data)
        configuration = configuration_response(
            entry_id=entry.entry_id,
            entry_data=entry.data,
            entry_options=entry.options,
            config=data.config,
            point_lookback_days=self._point_lookback_days,
            scan_interval_seconds=_entry_scan_interval_seconds(entry),
        )
        revision = history_digest(configuration)
        stop = temporal.evaluated_at.replace(minute=0, second=0, microsecond=0)
        inventory = build_hourly_statistics(
            {},
            {},
            data.config,
            start=stop - timedelta(hours=1),
            end=stop,
            evaluated_at=temporal.evaluated_at,
            source_end_by_thermostat={},
            existing_statistic_ids=(
                *cumulative_statistic_ids(data.config, list(data.summary_rows)),
                *(
                    base.removesuffix("_hourly_v2")
                    for base, resource in identity["resources"].items()
                    if history_quantity_id(resource)
                    in self.hourly.history_quantity_ids()
                ),
            ),
        )
        # Inventory describes method eligibility; it does not claim source data.
        inventory = tuple(
            replace(
                item,
                hours=(),
                blocked_reason=(
                    item.blocked_reason
                    if item.blocked_reason == "voc_unit_unresolved"
                    else None
                ),
            )
            for item in inventory
        )

        def check_current() -> None:
            current = self._coordinator.data
            if (
                self._unloaded
                or entry.state is not ConfigEntryState.LOADED
                or getattr(entry, "runtime_data", None) is not runtime
                or current is None
                or current.config != data.config
                or _writer_identity(entry, current) != identity
                or not self._coordinator.temporal_context_is_current(temporal)
                or history_digest(
                    configuration_response(
                        entry_id=entry.entry_id,
                        entry_data=entry.data,
                        entry_options=entry.options,
                        config=current.config,
                        point_lookback_days=self._point_lookback_days,
                        scan_interval_seconds=_entry_scan_interval_seconds(entry),
                    )
                )
                != revision
            ):
                raise ValueError("history_context_changed")

        return {
            "identity": deepcopy(identity),
            "config": deepcopy(data.config),
            "config_revision": revision,
            "timezone": temporal.local_tz.key,
            "timezone_revision": temporal.timezone_revision,
            "evaluated_at": temporal.evaluated_at.isoformat(),
            "descriptors": [
                item.descriptor for item in build_history_series(inventory, identity)
            ],
            "check_current": check_current,
        }

    def history_configuration(self) -> dict[str, Any]:
        """Project cached capability and status without a provider/Recorder read."""
        return self.hourly.history_configuration(self._history_context())

    async def async_stage_hourly_source(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            context = self._history_context()
            receipt = await self._hass.async_add_executor_job(
                self._stage_uploaded_source, deepcopy(request), context["identity"]
            )
            context["check_current"]()
            return receipt

    def _stage_uploaded_source(
        self, request: dict[str, Any], identity: dict[str, Any]
    ) -> dict[str, Any]:
        # The native context includes cleanup and must remain off the event loop.
        # Both immutable objects are durably verified before the upload is freed.
        with process_uploaded_file(self._hass, request["file_id"]) as path:
            with path.open("rb") as source:
                content = source.read(MAX_SOURCE_BYTES + 1)
            return stage_source(
                self.hourly._store,
                content,
                request["manifest"],
                request["sha256"],
                identity,
            )

    async def async_plan_hourly_history(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            context = self._history_context()
            response = await self.hourly.async_plan_history(request, context=context)
            context["check_current"]()
            return response

    async def async_apply_hourly_history(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        if request["plan"]["config_entry_id"] != request["config_entry_id"]:
            raise ValueError("history_plan_entry_mismatch")
        # Service cancellation cannot cancel durably accepted work or prevent its
        # entry worker from being scheduled after acceptance.
        task = self._coordinator.beestat_config_entry.async_create_background_task(
            self._hass,
            self._async_accept_hourly_history(request),
            f"{DOMAIN}_accept_hourly_history",
        )
        return await asyncio.shield(task)

    async def _async_accept_hourly_history(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            context = self._history_context()
            response = await self.hourly.async_accept_history(
                {**request["plan"], "plan_digest": request["plan_digest"]},
                context=context,
            )
            self._ensure_history_worker()
            self._notify_history_changed()
            return response

    def _ensure_history_worker(self) -> None:
        if self._unloaded or not self.hourly.has_pending_history:
            return
        if self._history_worker is None or self._history_worker.done():
            self._history_worker = (
                self._coordinator.beestat_config_entry.async_create_background_task(
                    self._hass,
                    self._async_run_hourly_history_operation(),
                    f"{DOMAIN}_hourly_history",
                )
            )

    def _notify_history_changed(self) -> None:
        """Reuse status-entity updates for corrections to closed history."""
        status = self.hourly.status().get("history_v3", {})
        self._coordinator.hourly_history_revision = status.get("root_revision", 0)
        self._coordinator.hourly_history_status = status.get("status", "legacy")
        self._coordinator.async_update_listeners()

    async def _async_run_hourly_history_operation(self) -> None:
        try:
            while not self._unloaded and self.hourly.has_pending_history:
                async with self._lock:
                    context = self._history_context()
                    result = await self.hourly.async_advance_history(context=context)
                    self._notify_history_changed()
                if result.get("status") in {"blocked", "completed"}:
                    break
                await asyncio.sleep(0)
        except Exception as err:  # noqa: BLE001 - journal retains exact recovery intent
            _LOGGER.warning("History operation paused (%s)", exception_fingerprint(err))
            self._notify_history_changed()

    async def async_get_hourly_history(self, request: dict[str, Any]) -> dict[str, Any]:
        # A read must see pending suppression while a writer awaits native work.
        request = deepcopy(request)
        context = self._history_context()
        material = await self.hourly.async_history_material(request, context=context)
        context["check_current"]()
        # Material is a detached readback. Keep the pure, potentially large
        # digest/projection off the event loop without passing runtime owners.
        projection_context = {
            key: context[key]
            for key in (
                "identity",
                "config_revision",
                "timezone",
                "timezone_revision",
                "evaluated_at",
            )
        }
        response = await self._hass.async_add_executor_job(
            history_response, request, material, projection_context
        )
        context["check_current"]()
        await self.hourly.async_check_history_material(material, context=context)
        context["check_current"]()
        return response

    async def _async_refresh_hourly_history(
        self, *, lookback_days: int, rebuilding: bool
    ) -> str | None:
        """Refresh adopted v3 quantities on the existing locked import cadence."""
        status = self.hourly.history_status()
        if status["status"] == "unselected":
            return None
        if rebuilding:
            # An explicit historical repair needs its own sealed plan and bounds.
            return "history_rebuild_requires_plan"
        if status["status"] != "completed":
            self._ensure_history_worker()
            return "history_operation_pending"
        try:
            result = await async_refresh_history(
                self, self._history_context(), lookback_days=lookback_days
            )
        except (
            ValueError,
            HourlyImportError,
            HourlyRecorderError,
            HourlyStorageError,
            BeestatApiError,
        ) as err:
            _LOGGER.warning("History refresh paused (%s)", exception_fingerprint(err))
            return "history_refresh_unverified"
        finally:
            self._ensure_history_worker()
            self._notify_history_changed()
        return (
            "history_operation_pending"
            if result.get("status") in {"accepted", "in_progress", "blocked"}
            else None
        )

    async def async_get_raw_points(self, request: RawPointRequest) -> dict[str, Any]:
        """Run a bounded source read under the loaded entry's task lifecycle."""

        entry = self._coordinator.beestat_config_entry
        runtime = entry.runtime_data
        self._raw_point_identity(request, runtime)
        return await entry.async_create_background_task(
            self._hass,
            self._async_get_raw_points(request, runtime),
            f"{DOMAIN}_get_raw_points",
        )

    def _raw_point_identity(
        self, request: RawPointRequest, runtime: BeestatStatisticsRuntime
    ) -> RawPointIdentity:
        entry = self._coordinator.beestat_config_entry
        if (
            self._unloaded
            or entry.state is not ConfigEntryState.LOADED
            or entry.runtime_data is not runtime
            or runtime.importer is not self
            or runtime.client is not self._client
            or runtime.coordinator is not self._coordinator
        ):
            raise RuntimeError(
                "Beestat Statistics config entry is unloaded or replaced"
            )
        data = self._coordinator.data
        return validate_raw_point_identity(
            request,
            data.config,
            data.thermostat_rows,
            data.sensor_rows,
            config_entry_id=entry.entry_id,
            metadata_fetched_at=data.fetched_at,
        )

    async def _async_get_raw_points(
        self, request: RawPointRequest, runtime: BeestatStatisticsRuntime
    ) -> dict[str, Any]:
        async with self._lock:
            identity = self._raw_point_identity(request, runtime)
            response = await async_read_raw_points(runtime.client, request, identity)
            if self._raw_point_identity(request, runtime) != identity:
                raise ValueError("Cached resource identity changed during source read")
            return response

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

        if self._unloaded:
            raise RuntimeError("Beestat Statistics config entry is unloaded")
        return (
            await self._coordinator.beestat_config_entry.async_create_background_task(
                self._hass,
                self._async_import_statistics(
                    point_lookback_days=point_lookback_days,
                    skip_sync=skip_sync,
                    force_full_summary=force_full_summary,
                    rebuild_start=rebuild_start,
                    rebuild_end=rebuild_end,
                    thermostat_id=thermostat_id,
                ),
                f"{DOMAIN}_import_statistics",
            )
        )

    async def _async_import_statistics(
        self,
        *,
        point_lookback_days: int | None,
        skip_sync: bool,
        force_full_summary: bool,
        rebuild_start: dt_date | None,
        rebuild_end: dt_date | None,
        thermostat_id: int | None,
    ) -> ImportResult:
        """Run both disjoint writers under the existing config-entry lock."""

        async with self._lock:
            if self._unloaded:
                raise RuntimeError("Beestat Statistics config entry is unloaded")
            partition = await self.hourly.async_writer_partition(
                _writer_identity(
                    self._coordinator.beestat_config_entry, self._coordinator.data
                )
            )
            self._ensure_history_worker()
            blocked = partition.hourly_blocked_reason
            if partition.hourly_ready:
                try:
                    await self.hourly.async_reconcile()
                except (
                    HourlyReconciliationError,
                    HourlyRecorderError,
                    HourlyStorageError,
                ):
                    # Only identified hourly persistence/readback failures are
                    # isolated. Revalidation below must still prove ownership.
                    blocked = "hourly_reconciliation_unverified"
            lookback_days = point_lookback_days or self._point_lookback_days
            runtime_data = await self._coordinator.async_refresh_runtime(
                skip_sync=skip_sync,
                summary_window=not force_full_summary,
            )
            _validate_thermostat_id(runtime_data, thermostat_id)
            partition = await self.hourly.async_writer_partition(
                _writer_identity(self._coordinator.beestat_config_entry, runtime_data)
            )
            blocked = blocked or partition.hourly_blocked_reason
            hourly_result: ImportResult | None = None
            if (
                partition.hourly_ready
                and partition.hourly_statistic_ids
                and blocked is None
            ):
                try:
                    hourly_result = await self._async_import_hourly(
                        runtime_data,
                        lookback_days=lookback_days,
                        rebuild_start=rebuild_start,
                        rebuild_end=rebuild_end,
                        thermostat_id=thermostat_id,
                    )
                except (
                    HourlyReconciliationError,
                    HourlyRecorderError,
                    HourlyStorageError,
                ):
                    blocked = "hourly_effect_unverified"
            history_blocked = await self._async_refresh_hourly_history(
                lookback_days=lookback_days,
                rebuilding=rebuild_start is not None or rebuild_end is not None,
            )
            blocked = blocked or history_blocked
            # A failed save can invalidate the manager's in-memory state, and
            # settings may change during an await. Never reuse an old partition.
            if self._unloaded:
                raise RuntimeError("Beestat Statistics config entry is unloaded")
            runtime_data = self._coordinator.data
            partition = await self.hourly.async_writer_partition(
                _writer_identity(self._coordinator.beestat_config_entry, runtime_data)
            )
            blocked = blocked or partition.hourly_blocked_reason
            legacy_result: ImportResult | None = None
            if partition.legacy_statistic_ids:
                legacy_result = await self._async_import_legacy(
                    runtime_data,
                    lookback_days=lookback_days,
                    force_full_summary=force_full_summary,
                    rebuild_start=rebuild_start,
                    rebuild_end=rebuild_end,
                    thermostat_id=thermostat_id,
                    allowed_legacy_ids=partition.legacy_statistic_ids,
                )
            result = _combined_import_result(
                legacy_result,
                hourly_result,
                has_hourly=partition.has_hourly,
                hourly_blocked_reason=blocked,
            )
            self._record_import_result(result)
            return result

    def _record_import_result(self, result: ImportResult) -> None:
        self._coordinator.async_record_import_result(
            imported_series=result.imported_series,
            imported_rows=result.imported_rows,
            source_rows=result.source_rows,
            skipped_windows=result.skipped_windows,
            skipped_runtime_thermostat_windows=result.skipped_runtime_thermostat_windows,
            skipped_runtime_sensor_windows=result.skipped_runtime_sensor_windows,
            skipped_window_examples=result.skipped_window_examples,
            summary_mode=result.summary_mode,
            summary_window_start=result.summary_window_start,
            summary_window_end=result.summary_window_end,
            summary_overlap_days=result.summary_overlap_days,
            summary_fallback_reason=result.summary_fallback_reason,
            cumulative_seed_count=result.cumulative_seed_count,
            coverage_incomplete=result.coverage_incomplete,
            writer_result={
                "legacy_imported_series": result.legacy_imported_series,
                "legacy_imported_rows": result.legacy_imported_rows,
                "hourly_imported_series": result.hourly_imported_series,
                "hourly_imported_rows": result.hourly_imported_rows,
                "hourly_blocked_reason": result.hourly_blocked_reason,
            },
        )
        _LOGGER.info(
            "Imported %s Beestat rows across %s series; mode=%s hourly_blocked=%s",
            result.imported_rows,
            result.imported_series,
            result.summary_mode,
            result.hourly_blocked_reason,
        )

    async def _async_import_legacy(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        lookback_days: int,
        force_full_summary: bool,
        rebuild_start: dt_date | None,
        rebuild_end: dt_date | None,
        thermostat_id: int | None,
        allowed_legacy_ids: frozenset[str],
    ) -> ImportResult:
        """Keep enabled unadmitted quantities on their existing daily writer."""

        prepared: PreparedImport | None = None
        for attempt in range(_IMPORT_TEMPORAL_CONTEXT_ATTEMPTS):
            temporal_context = self._coordinator.capture_temporal_context()
            prepared = await self._async_prepare_import(
                runtime_data,
                lookback_days=lookback_days,
                force_full_summary=force_full_summary,
                rebuild_start=rebuild_start,
                rebuild_end=rebuild_end,
                thermostat_id=thermostat_id,
                temporal_context=temporal_context,
                allowed_legacy_ids=allowed_legacy_ids,
            )
            current_partition = await self.hourly.async_writer_partition(
                _writer_identity(
                    self._coordinator.beestat_config_entry, self._coordinator.data
                )
            )
            if (
                self._coordinator.temporal_context_is_current(temporal_context)
                and runtime_data.config == self._coordinator.data.config
                and allowed_legacy_ids == current_partition.legacy_statistic_ids
            ):
                break
            if attempt + 1 == _IMPORT_TEMPORAL_CONTEXT_ATTEMPTS:
                raise RuntimeError(
                    "Home Assistant timezone or writer configuration changed repeatedly during "
                    "Beestat statistics import"
                )
            _LOGGER.info(
                "Restarting Beestat statistics preparation after a Home "
                "Assistant timezone change"
            )
            if self._coordinator.data is not None:
                runtime_data = self._coordinator.data
                allowed_legacy_ids = current_partition.legacy_statistic_ids

        if prepared is None:  # pragma: no cover - positive attempt constant
            raise RuntimeError("Beestat statistics import was not prepared")

        imported_rows = 0
        latest_start_by_id: dict[str, str | None] = {}
        for item in prepared.series:
            async_add_external_statistics(
                self._hass,
                cast(StatisticMetaData, item.metadata),
                cast(Iterable[StatisticData], item.statistics),
            )
            imported_rows += len(item.statistics)
            latest_start_by_id[item.statistic_id] = _format_start(item)

        return ImportResult(
            imported_series=len(prepared.series),
            imported_rows=imported_rows,
            source_rows=len(prepared.summary_rows)
            + sum(len(rows) for rows in prepared.thermostat_rows_by_id.values())
            + sum(len(rows) for rows in prepared.sensor_rows_by_id.values()),
            skipped_windows=prepared.skipped_windows.total_count,
            skipped_runtime_thermostat_windows=(
                prepared.skipped_windows.runtime_thermostat_count
            ),
            skipped_runtime_sensor_windows=(
                prepared.skipped_windows.runtime_sensor_count
            ),
            skipped_window_examples=prepared.skipped_windows.examples,
            latest_start_by_statistic_id=latest_start_by_id,
            summary_mode=prepared.summary_plan.mode,
            summary_window_start=_format_day(prepared.summary_plan.window_start),
            summary_window_end=_format_day(prepared.summary_plan.window_end),
            summary_overlap_days=prepared.summary_plan.overlap_days,
            summary_fallback_reason=prepared.summary_plan.fallback_reason,
            cumulative_seed_count=len(prepared.summary_plan.seeds),
            legacy_imported_series=len(prepared.series),
            legacy_imported_rows=imported_rows,
        )

    async def _async_import_hourly(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        lookback_days: int,
        rebuild_start: dt_date | None,
        rebuild_end: dt_date | None,
        thermostat_id: int | None,
    ) -> ImportResult:
        prepared = await self._async_prepare_hourly(
            runtime_data,
            lookback_days=lookback_days,
            rebuild_start=rebuild_start,
            rebuild_end=rebuild_end,
            thermostat_id=thermostat_id,
        )
        imported = await self.hourly.async_import(
            prepared.series,
            prepared.identity,
            ordinary_start=prepared.ordinary_start,
            eligible_resources=prepared.eligible_resources,
        )
        status = self.hourly.status()
        coverage_incomplete = bool(status.get("pending")) or any(
            record.get("coverage_incomplete", False)
            for record in status.get("series", {}).values()
        )
        skipped = prepared.skipped_windows
        return ImportResult(
            imported_series=imported["imported_series"],
            imported_rows=imported["imported_rows"],
            source_rows=prepared.source_rows,
            skipped_windows=skipped.total_count,
            skipped_runtime_thermostat_windows=skipped.runtime_thermostat_count,
            skipped_runtime_sensor_windows=skipped.runtime_sensor_count,
            skipped_window_examples=skipped.examples,
            latest_start_by_statistic_id=imported["latest_start_by_statistic_id"],
            summary_mode="hourly",
            summary_window_start=None,
            summary_window_end=None,
            summary_overlap_days=None,
            summary_fallback_reason=(
                "hourly_coverage_incomplete" if coverage_incomplete else None
            ),
            cumulative_seed_count=0,
            hourly_imported_series=imported["imported_series"],
            hourly_imported_rows=imported["imported_rows"],
            coverage_incomplete=coverage_incomplete,
        )

    async def _async_prepare_hourly(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        lookback_days: int,
        rebuild_start: dt_date | None = None,
        rebuild_end: dt_date | None = None,
        thermostat_id: int | None = None,
        epoch_start: datetime | None = None,
        statistic_ids: tuple[str, ...] = (),
    ) -> PreparedHourlyImport:
        for attempt in range(_IMPORT_TEMPORAL_CONTEXT_ATTEMPTS):
            context = self._coordinator.capture_temporal_context()
            start, end, measurement_end = _hourly_window(
                context,
                lookback_days=lookback_days,
                rebuild_start=rebuild_start,
                rebuild_end=rebuild_end,
                epoch_start=epoch_start,
                bootstrap_start=self.hourly.bootstrap_start(
                    thermostat_id=thermostat_id,
                    eligible_resources=_hourly_resource_identities(runtime_data.config),
                ),
            )
            skipped = SkippedWindowEvidence()
            thermostat_rows = await self._async_fetch_thermostat_rows(
                lookback_days,
                runtime_data,
                skipped,
                thermostat_id=thermostat_id,
                temporal_context=context,
                window=(start, end),
                preserve_source_rows=True,
            )
            sensor_rows = await self._async_fetch_sensor_rows(
                lookback_days,
                runtime_data,
                skipped,
                thermostat_id=thermostat_id,
                temporal_context=context,
                window=(start, end),
                preserve_source_rows=True,
            )
            if (
                self._coordinator.temporal_context_is_current(context)
                and runtime_data.config == self._coordinator.data.config
            ):
                break
            if attempt + 1 == _IMPORT_TEMPORAL_CONTEXT_ATTEMPTS:
                raise RuntimeError(
                    "Home Assistant timezone or configuration changed during hourly preparation"
                )
            runtime_data = self._coordinator.data or runtime_data
        config = runtime_data.config
        if thermostat_id is not None:
            config = replace(
                config,
                thermostats=tuple(
                    item
                    for item in config.thermostats
                    if item.thermostat_id == thermostat_id
                ),
                sensors=tuple(
                    item
                    for item in config.sensors
                    if item.thermostat_id == thermostat_id
                ),
            )
        retained = (*self.hourly.base_statistic_ids(), *statistic_ids)
        ordinary_start = (
            end - timedelta(days=lookback_days)
            if epoch_start is None and rebuild_start is None
            else None
        )
        source_starts = (
            self.hourly.source_starts(
                _hourly_resource_identities(config),
                start=start,
                end=end,
                ordinary_start=ordinary_start,
            )
            if epoch_start is None
            else {}
        )
        series = build_hourly_statistics(
            thermostat_rows,
            sensor_rows,
            config,
            start=start,
            end=end,
            evaluated_at=context.evaluated_at,
            source_end_by_thermostat=_observed_hourly_horizons(
                thermostat_rows,
                _thermostat_data_end_map(list(runtime_data.thermostat_rows)),
            ),
            existing_statistic_ids=_hourly_retained_ids(config, retained),
            start_by_statistic_id=source_starts,
            measurement_end=measurement_end,
        )
        return PreparedHourlyImport(
            series,
            _hourly_identity(
                self._coordinator.beestat_config_entry,
                runtime_data,
                series,
                selected_thermostat_id=thermostat_id,
            ),
            sum(len(rows) for rows in thermostat_rows.values())
            + sum(len(rows) for rows in sensor_rows.values()),
            skipped,
            ordinary_start,
            _hourly_resource_identities(config),
        )

    async def async_select_hourly_statistics(
        self,
        *,
        epoch_start: datetime,
        statistic_ids: tuple[str, ...],
        expected_revision: int,
        preview_digest: str | None = None,
    ) -> dict[str, Any]:
        if self._unloaded:
            raise RuntimeError("Beestat Statistics config entry is unloaded")
        return (
            await self._coordinator.beestat_config_entry.async_create_background_task(
                self._hass,
                self._async_select_hourly_statistics(
                    epoch_start=epoch_start,
                    statistic_ids=statistic_ids,
                    expected_revision=expected_revision,
                    preview_digest=preview_digest,
                ),
                f"{DOMAIN}_select_hourly_statistics",
            )
        )

    async def _async_select_hourly_statistics(
        self,
        *,
        epoch_start: datetime,
        statistic_ids: tuple[str, ...],
        expected_revision: int,
        preview_digest: str | None,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._unloaded:
                raise RuntimeError("Beestat Statistics config entry is unloaded")
            epoch_start = _hourly_utc_hour(epoch_start)
            if not statistic_ids or len(set(statistic_ids)) != len(statistic_ids):
                raise ValueError("Select distinct hourly statistic IDs")
            await self.hourly.async_reconcile()
            data = await self._coordinator.async_refresh_runtime(
                skip_sync=True, summary_window=True
            )
            prepared = await self._async_prepare_hourly(
                data,
                lookback_days=self._point_lookback_days,
                epoch_start=epoch_start,
                statistic_ids=statistic_ids,
            )
            return await self.hourly.async_select(
                prepared.series,
                prepared.identity,
                epoch_start=epoch_start,
                statistic_ids=statistic_ids,
                expected_revision=expected_revision,
                preview_digest=preview_digest,
            )

    async def async_get_hourly_coverage(
        self,
        *,
        start: datetime,
        end: datetime,
        statistic_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        if self._unloaded:
            raise RuntimeError("Beestat Statistics config entry is unloaded")
        return (
            await self._coordinator.beestat_config_entry.async_create_background_task(
                self._hass,
                self._async_get_hourly_coverage(start, end, statistic_ids),
                f"{DOMAIN}_get_hourly_coverage",
            )
        )

    async def _async_get_hourly_coverage(
        self, start: datetime, end: datetime, statistic_ids: tuple[str, ...] | None
    ) -> dict[str, Any]:
        if self._unloaded:
            raise RuntimeError("Beestat Statistics config entry is unloaded")
        start, end = _hourly_utc_hour(start), _hourly_utc_hour(end)
        _validate_hourly_window(start, end)
        # A cached read must expose suppression while a writer awaits Recorder.
        return self.hourly.coverage(start=start, end=end, statistic_ids=statistic_ids)

    async def _async_prepare_import(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        lookback_days: int,
        force_full_summary: bool,
        rebuild_start: dt_date | None,
        rebuild_end: dt_date | None,
        thermostat_id: int | None,
        temporal_context: TemporalContext,
        allowed_legacy_ids: frozenset[str] | None = None,
    ) -> PreparedImport:
        """Prepare one coherent local-time import before Recorder writes."""

        existing_statistic_ids = await self._async_existing_detailed_statistic_ids(
            runtime_data, allowed_legacy_ids=allowed_legacy_ids
        )
        summary_plan = await self._async_summary_import_plan(
            runtime_data,
            force_full_summary=force_full_summary,
            temporal_context=temporal_context,
            existing_statistic_ids=existing_statistic_ids,
            allowed_legacy_ids=allowed_legacy_ids,
        )
        summary_rows = _filter_summary_rows_by_thermostat(
            summary_plan.rows,
            thermostat_id,
        )
        skipped_windows = SkippedWindowEvidence()
        thermostat_rows_by_id = await self._async_fetch_thermostat_rows(
            lookback_days,
            runtime_data,
            skipped_windows,
            start_day=rebuild_start,
            end_day=rebuild_end,
            thermostat_id=thermostat_id,
            temporal_context=temporal_context,
            allowed_statistic_ids=allowed_legacy_ids,
        )
        sensor_rows_by_id = await self._async_fetch_sensor_rows(
            lookback_days,
            runtime_data,
            skipped_windows,
            start_day=rebuild_start,
            end_day=rebuild_end,
            thermostat_id=thermostat_id,
            temporal_context=temporal_context,
            allowed_statistic_ids=allowed_legacy_ids,
        )
        series = build_statistics(
            summary_rows,
            thermostat_rows_by_id,
            sensor_rows_by_id,
            temporal_context.local_tz,
            runtime_data.config,
            existing_statistic_ids=existing_statistic_ids,
        )
        if allowed_legacy_ids is not None:
            known_ids = {
                key.removesuffix("_hourly_v2")
                for key in _hourly_resource_identities(runtime_data.config)
            }
            if any(item.statistic_id not in known_ids for item in series):
                raise ValueError("Legacy output has no verified resource identity")
            series = [
                item for item in series if item.statistic_id in allowed_legacy_ids
            ]
        if summary_plan.seeds:
            series = apply_cumulative_seeds(series, summary_plan.seeds)
        if rebuild_start is not None or rebuild_end is not None:
            series = _filter_series_statistics(
                series,
                start_day=rebuild_start,
                end_day=rebuild_end,
                local_tz=temporal_context.local_tz,
            )
        return PreparedImport(
            summary_plan=summary_plan,
            summary_rows=summary_rows,
            skipped_windows=skipped_windows,
            thermostat_rows_by_id=thermostat_rows_by_id,
            sensor_rows_by_id=sensor_rows_by_id,
            series=[item for item in series if item.statistics],
        )

    async def _async_summary_import_plan(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        force_full_summary: bool,
        temporal_context: TemporalContext,
        existing_statistic_ids: frozenset[str],
        allowed_legacy_ids: frozenset[str] | None = None,
    ) -> SummaryImportPlan:
        cached_rows = list(runtime_data.summary_rows)
        if force_full_summary:
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="forced_full_baseline",
            )

        statistic_ids = cumulative_statistic_ids(
            runtime_data.config,
            cached_rows,
            existing_statistic_ids=existing_statistic_ids,
        )
        if allowed_legacy_ids is not None:
            statistic_ids = tuple(
                value for value in statistic_ids if value in allowed_legacy_ids
            )
        if not statistic_ids:
            return SummaryImportPlan.full(
                cached_rows,
                fallback_reason="no_cumulative_statistics",
            )

        latest_by_id = await self._async_latest_cumulative_starts(statistic_ids)
        if len(latest_by_id) != len(statistic_ids):
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="missing_latest_recorder_statistics",
            )

        latest_day = min(
            value.astimezone(temporal_context.local_tz).date()
            for value in latest_by_id.values()
        )
        window_start = latest_day - timedelta(days=DEFAULT_SUMMARY_OVERLAP_DAYS)
        window_end = (
            _latest_summary_day(cached_rows)
            or temporal_context.evaluated_at.astimezone(
                temporal_context.local_tz
            ).date()
        )
        if window_start > window_end:
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="empty_summary_window",
            )

        seeds = await self._async_cumulative_seeds(
            statistic_ids,
            seed_day=window_start - timedelta(days=1),
            window_start=window_start,
            local_tz=temporal_context.local_tz,
        )
        if len(seeds) != len(statistic_ids):
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="missing_prior_recorder_seed",
            )

        try:
            rows = await self._client.async_read_runtime_thermostat_summary(
                window_start.isoformat(),
                window_end.isoformat(),
            )
        except BeestatAuthError:
            raise
        except BeestatApiError:
            _LOGGER.warning(
                "Falling back to full Beestat summary baseline after windowed read failed"
            )
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="summary_window_read_failed",
            )

        # The Recorder window can include hardware absent from the recent cache,
        # or a correction can introduce another stage between the two reads.
        if (
            not {
                value
                for value in cumulative_statistic_ids(
                    runtime_data.config,
                    rows,
                    existing_statistic_ids=existing_statistic_ids,
                )
                if allowed_legacy_ids is None or value in allowed_legacy_ids
            }
            <= seeds.keys()
        ):
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="missing_prior_recorder_seed",
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

    async def _async_existing_detailed_statistic_ids(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        allowed_legacy_ids: frozenset[str] | None = None,
    ) -> frozenset[str]:
        """Retain imported hardware even when its source runtime becomes all zero."""

        statistic_ids = set(detailed_runtime_statistic_ids(runtime_data.config))
        if allowed_legacy_ids is not None:
            statistic_ids.intersection_update(allowed_legacy_ids)
        if not statistic_ids:
            return frozenset()
        recorder = get_recorder_instance(self._hass)
        # Reads use a different executor from imports. Queue an unconditional
        # marker so even the last running import, including an old entry's
        # queued work, is processed before inventory and cumulative seed reads.
        synchronized: asyncio.Future[None] = self._hass.loop.create_future()
        recorder.queue_task(SynchronizeTask(synchronized))
        await asyncio.shield(synchronized)
        metadata = await recorder.async_add_executor_job(
            partial(get_metadata, self._hass, statistic_ids=statistic_ids)
        )
        return frozenset(metadata)

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
        local_tz: ZoneInfo,
    ) -> dict[str, CumulativeStatisticSeed]:
        return await get_recorder_instance(self._hass).async_add_executor_job(
            partial(
                _cumulative_seeds_during_period,
                self._hass,
                tuple(statistic_ids),
                _local_midnight(seed_day, local_tz).astimezone(UTC),
                _local_midnight(window_start, local_tz).astimezone(UTC),
            )
        )

    async def _async_fetch_thermostat_rows(
        self,
        lookback_days: int,
        runtime_data: BeestatRuntimeData,
        skipped_windows: SkippedWindowEvidence,
        *,
        start_day: dt_date | None = None,
        end_day: dt_date | None = None,
        thermostat_id: int | None = None,
        temporal_context: TemporalContext,
        window: tuple[datetime, datetime] | None = None,
        preserve_source_rows: bool = False,
        allowed_statistic_ids: frozenset[str] | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        start, end = window or _point_window(
            lookback_days,
            temporal_context.local_tz,
            start_day,
            end_day,
            evaluated_at=temporal_context.evaluated_at,
        )
        thermostat_data_end = _thermostat_data_end_map(
            list(runtime_data.thermostat_rows)
        )

        rows_by_id: dict[int, list[dict[str, Any]]] = {}
        thermostat_ids = sorted(
            thermostat.thermostat_id
            for thermostat in runtime_data.config.thermostats
            if thermostat_id is None or thermostat.thermostat_id == thermostat_id
            if allowed_statistic_ids is None
            or any(
                f"{STATISTIC_SOURCE}:{thermostat.slug}_{spec.statistic_suffix}"
                in allowed_statistic_ids
                for spec in THERMOSTAT_POINT_STATISTICS
            )
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
            rows_by_id[current_thermostat_id] = (
                rows
                if preserve_source_rows
                else _dedupe_rows(rows, id_field="thermostat_id")
            )
        return rows_by_id

    async def _async_read_runtime_thermostat_window(
        self,
        thermostat_id: int,
        start: datetime,
        end: datetime,
        skipped_windows: SkippedWindowEvidence,
    ) -> list[dict[str, Any]]:
        return await self._async_read_runtime_window(
            "runtime_thermostat", thermostat_id, start, end, skipped_windows
        )

    async def _async_fetch_sensor_rows(
        self,
        lookback_days: int,
        runtime_data: BeestatRuntimeData,
        skipped_windows: SkippedWindowEvidence,
        *,
        start_day: dt_date | None = None,
        end_day: dt_date | None = None,
        thermostat_id: int | None = None,
        temporal_context: TemporalContext,
        window: tuple[datetime, datetime] | None = None,
        preserve_source_rows: bool = False,
        allowed_statistic_ids: frozenset[str] | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        start, end = window or _point_window(
            lookback_days,
            temporal_context.local_tz,
            start_day,
            end_day,
            evaluated_at=temporal_context.evaluated_at,
        )
        sensor_to_thermostat = _sensor_thermostat_map(list(runtime_data.sensor_rows))
        thermostat_data_end = _thermostat_data_end_map(
            list(runtime_data.thermostat_rows)
        )
        configured_sensor_ids = {
            sensor.sensor_id
            for sensor in runtime_data.config.sensors
            if thermostat_id is None or sensor.thermostat_id == thermostat_id
        }

        rows_by_id: dict[int, list[dict[str, Any]]] = {}
        sensor_ids = sorted(
            {
                spec.sensor_id
                for spec in build_sensor_specs(runtime_data.config)
                if spec.sensor_id in configured_sensor_ids
                if allowed_statistic_ids is None
                or f"{STATISTIC_SOURCE}:{spec.statistic_suffix}"
                in allowed_statistic_ids
            }
        )
        for sensor_id in sensor_ids:
            rows: list[dict[str, Any]] = []
            mapped_thermostat_id = sensor_to_thermostat.get(sensor_id)
            cap_end = min(
                end,
                thermostat_data_end.get(mapped_thermostat_id, end)
                if mapped_thermostat_id is not None
                else end,
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
            rows_by_id[sensor_id] = (
                rows
                if preserve_source_rows
                else _dedupe_rows(rows, id_field="sensor_id")
            )
        return rows_by_id

    async def _async_read_runtime_sensor_window(
        self,
        sensor_id: int,
        start: datetime,
        end: datetime,
        skipped_windows: SkippedWindowEvidence,
    ) -> list[dict[str, Any]]:
        return await self._async_read_runtime_window(
            "runtime_sensor", sensor_id, start, end, skipped_windows
        )

    async def _async_read_runtime_window(
        self,
        resource: SkippedWindowResource,
        resource_id: int,
        start: datetime,
        end: datetime,
        skipped_windows: SkippedWindowEvidence,
    ) -> list[dict[str, Any]]:
        """Recover oversized source windows with one bounded bisection policy."""

        read = (
            self._client.async_read_runtime_thermostat
            if resource == "runtime_thermostat"
            else self._client.async_read_runtime_sensor
        )
        try:
            return await read(
                resource_id,
                _format_beestat_time(start),
                _format_beestat_time(end),
            )
        except BeestatAuthError, BeestatPermanentError:
            raise
        except BeestatApiError as err:
            if end - start > timedelta(days=1):
                midpoint = start + ((end - start) / 2)
                rows: list[dict[str, Any]] = []
                rows.extend(
                    await self._async_read_runtime_window(
                        resource,
                        resource_id,
                        start,
                        midpoint,
                        skipped_windows,
                    )
                )
                rows.extend(
                    await self._async_read_runtime_window(
                        resource,
                        resource_id,
                        midpoint,
                        end,
                        skipped_windows,
                    )
                )
                return rows

            skipped_windows.record(
                resource,
                start=_format_beestat_time(start),
                end=_format_beestat_time(end),
            )
            _LOGGER.warning(
                "Skipping Beestat %s window start=%s end=%s: %s",
                resource,
                _format_beestat_time(start),
                _format_beestat_time(end),
                exception_fingerprint(err),
            )
            return []


async def _async_handle_import_service(hass: HomeAssistant, call: ServiceCall) -> None:
    """Import Beestat statistics on demand."""

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
        runtime.coordinator.beestat_config_entry.async_start_reauth_if_available(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="beestat_auth_failed",
        ) from None
    except BeestatApiError as err:
        runtime.coordinator.async_record_import_error(err)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="beestat_request_failed",
        ) from None
    except Exception as err:  # noqa: BLE001 - sanitize at the HA service boundary
        runtime.coordinator.async_record_import_error(err)
        _LOGGER.error(
            "Unexpected Beestat statistics import service failure (%s)",
            exception_fingerprint(err),
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="statistics_import_failed",
        ) from None


async def _async_handle_get_configuration(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Return the effective configuration for one loaded entry."""

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
    data = runtime.coordinator.data
    return configuration_response(
        entry_id=entry.entry_id,
        entry_data=entry.data,
        entry_options=entry.options,
        config=runtime.coordinator.data.config,
        point_lookback_days=_entry_point_lookback_days(entry),
        scan_interval_seconds=_entry_scan_interval_seconds(entry),
        thermostat_rows=runtime.coordinator.data.thermostat_rows,
        thermostat_settings=getattr(
            runtime.coordinator.data,
            "thermostat_settings",
            {},
        ),
        runtime_quality={
            thermostat.thermostat_id: filter_forecast_quality_attributes(
                build_filter_forecast(
                    thermostat,
                    data.thermostats.get(thermostat.thermostat_id),
                    today=data.projected_at.astimezone(
                        runtime.coordinator.local_tz
                    ).date(),
                )
            )
            for thermostat in data.config.thermostats
        },
        hourly_statistics=(
            {
                **runtime.importer.hourly.status(),
                "history_v3": runtime.importer.history_configuration(),
            }
            if getattr(runtime, "importer", None) is not None
            else None
        ),
    )


def _loaded_hourly_importer(
    hass: HomeAssistant, entry_id: str
) -> BeestatStatisticsImporter:
    entry = hass.config_entries.async_get_entry(entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
        or (runtime := getattr(entry, "runtime_data", None)) is None
        or getattr(runtime, "importer", None) is None
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_loaded_entry"
        )
    return cast(BeestatStatisticsImporter, runtime.importer)


async def _async_handle_hourly_service(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    importer = _loaded_hourly_importer(hass, call.data[ATTR_CONFIG_ENTRY_ID])
    ids = call.data.get(ATTR_STATISTIC_IDS)
    try:
        if call.data.get("contract_version") == 3:
            return await importer.async_get_hourly_history(dict(call.data))
        if call.service == SERVICE_SELECT_HOURLY_STATISTICS:
            return await importer.async_select_hourly_statistics(
                epoch_start=call.data[ATTR_EPOCH_START],
                statistic_ids=tuple(ids or ()),
                expected_revision=call.data[ATTR_EXPECTED_REVISION],
                preview_digest=call.data.get(ATTR_PREVIEW_DIGEST),
            )
        return await importer.async_get_hourly_coverage(
            start=call.data[ATTR_START],
            end=call.data[ATTR_END],
            statistic_ids=tuple(ids) if ids is not None else None,
        )
    except BeestatAuthError:
        importer._coordinator.beestat_config_entry.async_start_reauth_if_available(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="beestat_auth_failed"
        ) from None
    except Exception as err:  # noqa: BLE001 - sanitize the service boundary
        _LOGGER.warning(
            "Hourly statistics action did not complete (%s)", exception_fingerprint(err)
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hourly_statistics_failed"
        ) from None


async def _async_handle_history_service(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Keep evidence admission and history effects behind an active admin."""
    user = (
        await hass.auth.async_get_user(call.context.user_id)
        if call.context.user_id is not None
        else None
    )
    if user is None or not user.is_active or not user.is_admin:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="hourly_history_admin_required"
        )
    importer = _loaded_hourly_importer(hass, call.data[ATTR_CONFIG_ENTRY_ID])
    try:
        request = dict(call.data)
        if call.service == SERVICE_STAGE_HOURLY_SOURCE:
            return await importer.async_stage_hourly_source(request)
        if call.service == SERVICE_PLAN_HOURLY_HISTORY:
            return await importer.async_plan_hourly_history(request)
        return await importer.async_apply_hourly_history(request)
    except Exception as err:  # noqa: BLE001 - no private payload or raw errors in output
        _LOGGER.warning(
            "History action did not complete (%s)", exception_fingerprint(err)
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hourly_statistics_failed"
        ) from None


async def _async_handle_raw_points_service(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Export only a bounded fixed resource read for an active admin caller."""

    user = (
        await hass.auth.async_get_user(call.context.user_id)
        if call.context.user_id is not None
        else None
    )
    if user is None or not user.is_active or not user.is_admin:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="raw_points_admin_required"
        )
    importer = _loaded_hourly_importer(hass, call.data[ATTR_CONFIG_ENTRY_ID])
    try:
        request = parse_raw_point_request(
            call.data["resource"],
            call.data["resource_id"],
            call.data[ATTR_START],
            call.data[ATTR_END],
        )
        return await importer.async_get_raw_points(request)
    except ValueError:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="raw_points_invalid"
        ) from None
    except Exception as err:  # noqa: BLE001 - sanitize without reauth or status writes
        _LOGGER.warning(
            "Raw point read did not complete (%s)", exception_fingerprint(err)
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="raw_points_failed"
        ) from None


async def _async_handle_rebuild_service(hass: HomeAssistant, call: ServiceCall) -> None:
    """Rebuild Beestat statistics on demand."""

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
            translation_placeholders={"thermostat_id": str(err.thermostat_id)},
        ) from None
    except BeestatAuthError as err:
        runtime.coordinator.async_record_import_error(err)
        runtime.coordinator.beestat_config_entry.async_start_reauth_if_available(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="beestat_auth_failed",
        ) from None
    except BeestatApiError as err:
        runtime.coordinator.async_record_import_error(err)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="beestat_request_failed",
        ) from None
    except Exception as err:  # noqa: BLE001 - sanitize at the HA service boundary
        runtime.coordinator.async_record_import_error(err)
        _LOGGER.error(
            "Unexpected Beestat statistics rebuild service failure (%s)",
            exception_fingerprint(err),
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="statistics_import_failed",
        ) from None


async def _async_handle_repair_filter_change_boundary(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Repair the filter change timestamp for an existing calendar date."""

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
    thermostat_id = call.data[CONF_THERMOSTAT_ID]
    thermostat = next(
        (
            item
            for item in runtime.coordinator.data.config.thermostats
            if item.thermostat_id == thermostat_id
        ),
        None,
    )
    if thermostat is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_thermostat_id",
            translation_placeholders={"thermostat_id": str(thermostat_id)},
        )
    changed_at = call.data[ATTR_CHANGED_AT]
    try:
        changed_at = resolve_filter_change_timestamp(
            changed_at,
            runtime.coordinator.local_tz,
        )
    except ValueError:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="filter_change_boundary_local_time_invalid",
        ) from None
    now = datetime.now(UTC)
    if changed_at > now or changed_at < now - timedelta(days=31):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="filter_change_boundary_out_of_range",
        )
    prior_boundary = saved_filter_boundary(runtime.coordinator, thermostat_id)
    saved_date = prior_boundary[1]
    repair_date = changed_at.astimezone(runtime.coordinator.local_tz).date()
    if saved_date is None or saved_date != repair_date:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="filter_change_boundary_date_mismatch",
        )
    await async_mark_filter_changed(
        runtime.coordinator,
        thermostat_id,
        changed_at,
        dismiss_alerts=False,
        source="repair",
        expected_boundary=prior_boundary,
    )


async def _async_handle_record_filter_change(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Record a timestamped physical replacement against an explicit prior cycle."""

    coordinator = _loaded_filter_coordinator(
        hass, call.data[ATTR_CONFIG_ENTRY_ID], call.data[CONF_THERMOSTAT_ID]
    )
    try:
        changed_at = resolve_filter_change_timestamp(
            call.data[ATTR_CHANGED_AT], coordinator.local_tz
        )
        expected_at = call.data[ATTR_EXPECTED_CHANGED_AT]
        if expected_at is not None:
            expected_at = resolve_filter_change_timestamp(
                expected_at, coordinator.local_tz
            )
    except ValueError, OverflowError:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="filter_change_boundary_local_time_invalid",
        ) from None
    now = datetime.now(UTC)
    try:
        return await async_mark_filter_changed(
            coordinator,
            call.data[CONF_THERMOSTAT_ID],
            changed_at,
            source="service",
            request_id=call.data[ATTR_REQUEST_ID],
            expected_boundary=(
                expected_at,
                call.data[ATTR_EXPECTED_CHANGED_DATE],
                call.data[ATTR_EXPECTED_REQUEST_ID],
            ),
            accepted_interval=(now - timedelta(days=31), now),
        )
    except FilterChangeConflictError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=str(err),
        ) from None
    except Exception as err:  # noqa: BLE001 - sanitize at the HA service boundary
        _LOGGER.error(
            "Unexpected filter-change action failure (%s)", exception_fingerprint(err)
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="beestat_request_failed",
        ) from None


def _loaded_filter_coordinator(
    hass: HomeAssistant, entry_id: str, thermostat_id: int
) -> BeestatRuntimeDataCoordinator:
    entry = hass.config_entries.async_get_entry(entry_id)
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
    if not any(
        item.thermostat_id == thermostat_id
        for item in runtime.coordinator.data.config.thermostats
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_thermostat_id",
            translation_placeholders={"thermostat_id": str(thermostat_id)},
        )
    return cast(BeestatRuntimeDataCoordinator, runtime.coordinator)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up Beestat Statistics and import YAML configuration if present."""

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_STATISTICS,
        partial(_async_handle_import_service, hass),
        schema=IMPORT_SERVICE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CONFIGURATION,
        partial(_async_handle_get_configuration, hass),
        schema=GET_CONFIGURATION_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    for service, schema in (
        (SERVICE_SELECT_HOURLY_STATISTICS, SELECT_HOURLY_SERVICE_SCHEMA),
        (SERVICE_GET_HOURLY_COVERAGE, GET_HOURLY_COVERAGE_SERVICE_SCHEMA),
    ):
        hass.services.async_register(
            DOMAIN,
            service,
            partial(_async_handle_hourly_service, hass),
            schema=schema,
            supports_response=SupportsResponse.ONLY,
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_RAW_POINTS,
        partial(_async_handle_raw_points_service, hass),
        schema=GET_RAW_POINTS_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    for service, schema in (
        (SERVICE_STAGE_HOURLY_SOURCE, STAGE_HISTORY_SCHEMA),
        (SERVICE_PLAN_HOURLY_HISTORY, PLAN_HISTORY_SCHEMA),
        (SERVICE_APPLY_HOURLY_HISTORY, APPLY_HISTORY_SCHEMA),
    ):
        hass.services.async_register(
            DOMAIN,
            service,
            partial(_async_handle_history_service, hass),
            schema=schema,
            supports_response=SupportsResponse.ONLY,
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REBUILD_STATISTICS,
        partial(_async_handle_rebuild_service, hass),
        schema=REBUILD_SERVICE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REPAIR_FILTER_CHANGE_BOUNDARY,
        partial(_async_handle_repair_filter_change_boundary, hass),
        schema=REPAIR_FILTER_CHANGE_BOUNDARY_SERVICE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD_FILTER_CHANGE,
        partial(_async_handle_record_filter_change, hass),
        schema=RECORD_FILTER_CHANGE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
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
    else:
        async_set_yaml_connection_change_issue(hass, active=False)

    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> bool:
    """Set up Beestat Statistics from a config entry."""

    local_tz = ZoneInfo(str(hass.config.time_zone))
    api_base = _validated_entry_api_base(hass, entry)
    client = BeestatClient(
        async_get_clientsession(hass),
        entry.data[CONF_API_KEY],
        api_base,
    )
    coordinator = BeestatRuntimeDataCoordinator(
        hass,
        entry,
        client,
        local_tz=local_tz,
        scan_interval_seconds=_entry_scan_interval_seconds(entry),
    )
    importer = BeestatStatisticsImporter(
        hass,
        client,
        coordinator,
        point_lookback_days=_entry_point_lookback_days(entry),
    )
    runtime = BeestatStatisticsRuntime(
        client=client,
        coordinator=coordinator,
        importer=importer,
        scan_interval=timedelta(seconds=_entry_scan_interval_seconds(entry)),
    )
    entry.runtime_data = runtime
    _async_track_time_zone_updates(hass, entry, coordinator)

    await coordinator.async_config_entry_first_refresh()
    async_register_service_device(hass, entry)
    _migrate_legacy_unique_ids(hass, entry, coordinator.data)
    _async_enable_default_problem_entities(hass, entry, coordinator.data)
    _async_migrate_homekit_device_assignments(hass, entry, coordinator.data)
    _async_track_source_device_relinks(hass, entry)
    _async_track_room_temperature_sources(hass, entry)
    if coordinator.data is not None:
        async_remove_cross_integration_device_ownership(
            hass,
            entry.entry_id,
            (
                *(item.device_id for item in coordinator.data.config.thermostats),
                *(item.device_id for item in coordinator.data.config.sensors),
            ),
        )
    _async_update_override_issues(hass, entry)
    _async_track_override_issue_updates(hass, entry)

    scheduled_import_unavailable_logged = False

    async def async_run_scheduled_import(*, skip_sync: bool = False) -> None:
        nonlocal scheduled_import_unavailable_logged
        try:
            await importer.async_import_statistics(skip_sync=skip_sync)
        except BeestatAuthError as err:
            coordinator.beestat_config_entry.async_start_reauth_if_available(hass)
            coordinator.async_record_import_error(err)
            if not scheduled_import_unavailable_logged:
                _LOGGER.info(
                    "Beestat statistics import is unavailable due to authentication failure"
                )
                scheduled_import_unavailable_logged = True
        except Exception as err:  # noqa: BLE001 - sanitize scheduled task failures
            coordinator.async_record_import_error(err)
            if not scheduled_import_unavailable_logged:
                _LOGGER.info(
                    "Beestat statistics import is unavailable (%s)",
                    exception_fingerprint(err),
                )
                scheduled_import_unavailable_logged = True
        else:
            if scheduled_import_unavailable_logged:
                _LOGGER.info("Beestat statistics import is available again")
                scheduled_import_unavailable_logged = False

    import_scheduler = CoalescingTaskScheduler(
        async_run_scheduled_import,
        lambda coroutine: entry.async_create_background_task(
            hass,
            coroutine,
            f"{DOMAIN}_scheduled_import",
        ),
    )

    @callback
    def async_schedule_import(_event_or_time: Any) -> None:
        """Schedule one bounded import pass from an event-loop callback."""

        import_scheduler.schedule()

    _async_track_runtime_entity_states(
        hass,
        entry,
        _filter_changed_entity_ids,
        async_schedule_import,
    )

    remove_interval = async_track_time_interval(
        hass,
        async_schedule_import,
        runtime.scan_interval,
    )
    entry.async_on_unload(remove_interval)

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception, asyncio.CancelledError:
        await _async_rollback_platforms(hass, entry)
        raise
    entry.async_create_background_task(
        hass,
        async_run_scheduled_import(skip_sync=True),
        f"{DOMAIN}_startup_import",
        eager_start=False,
    )
    return True


async def _async_rollback_platforms(
    hass: HomeAssistant, entry: BeestatStatisticsConfigEntry
) -> None:
    """Release acquired platforms without replacing the setup failure."""
    try:
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    except (Exception, asyncio.CancelledError) as err:  # noqa: BLE001 - preserve setup failure
        _LOGGER.error(
            "Error unloading platforms after setup failure (%s)",
            exception_fingerprint(err),
        )


def _validated_entry_api_base(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> str:
    """Return a secure stored API base before any credential-bearing transport."""

    try:
        api_base = normalize_api_base(entry.data[CONF_API_BASE])
    except ValueError:
        async_set_insecure_api_base_issue(hass, active=True)
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="invalid_api_base",
        ) from None
    async_set_insecure_api_base_issue(hass, active=False)
    return api_base


@callback
def _async_track_time_zone_updates(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    coordinator: BeestatRuntimeDataCoordinator,
) -> None:
    """Reproject cached state when Home Assistant's configured timezone changes."""

    @callback
    def handle_core_config_update(_event: Event[Any]) -> None:
        coordinator.async_update_local_timezone(ZoneInfo(str(hass.config.time_zone)))

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, handle_core_config_update)
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate Beestat Statistics config entries."""

    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Cannot migrate Beestat Statistics config entry from version %s.%s",
            entry.version,
            entry.minor_version,
        )
        return False

    migrated_data, migrated_options = migrate_entry_payload(
        entry.data,
        entry.options,
        entity_registry=er.async_get(hass),
    )
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

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


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

    if not is_beestat_only_device(device_entry, entry.entry_id):
        return False

    beestat_identifiers = set(device_entry.identifiers)
    return beestat_identifiers.isdisjoint(_current_beestat_device_identifiers(data))


def _current_beestat_device_identifiers(
    data: BeestatRuntimeData,
) -> set[tuple[str, str]]:
    """Return Beestat-owned fallback identifiers currently present in live data."""

    identifiers = {(DOMAIN, "service")}
    identifiers.update(
        (DOMAIN, f"thermostat_{thermostat.thermostat_id}")
        for thermostat in data.config.thermostats
        if thermostat.device_id is None
    )
    identifiers.update(
        (DOMAIN, f"sensor_{sensor.sensor_id}")
        for sensor in data.config.sensors
        if sensor.device_id is None
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
    skipped_conflicts = 0
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
            skipped_conflicts += 1
            continue
        registry.async_update_entity(
            entity_entry.entity_id,
            new_unique_id=new_unique_id,
        )
    if skipped_conflicts:
        _LOGGER.warning(
            "Skipped %s Beestat unique ID migration conflict(s)",
            skipped_conflicts,
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
    repaired_count = 0
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
        repaired_count += 1
    if repaired_count:
        _LOGGER.info(
            "Repaired %s Beestat diagnostic entity record(s) after default visibility change",
            repaired_count,
        )


def _default_problem_entity_id(
    thermostat: ConfiguredThermostat,
    suffix: str,
) -> str:
    if thermostat.device_id is not None:
        object_id = f"{thermostat.slug}_{suffix}"
    else:
        object_id = f"beestat_{thermostat.slug}_{suffix}"
    return f"binary_sensor.{object_id}"


def _is_generic_problem_entity_id(entity_id: str) -> bool:
    object_id = entity_id.split(".", 1)[-1]
    return object_id.endswith(("_problem", "_problem_2"))


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
    target_device_ids = _mapped_resource_device_ids(data)
    moved_count = 0
    for entity_entry in er.async_entries_for_config_entry(
        entity_registry,
        entry.entry_id,
    ):
        if (
            entity_entry.config_entry_id != entry.entry_id
            or entity_entry.platform != DOMAIN
        ):
            continue
        resource_type, separator, resource_suffix = entity_entry.unique_id.partition(
            "_"
        )
        resource_id, suffix_separator, suffix = resource_suffix.partition("_")
        resource_key = (resource_type, resource_id)
        if (
            not separator
            or not suffix_separator
            or not suffix
            or resource_key not in target_device_ids
        ):
            continue
        target_device_id = target_device_ids[resource_key]
        if target_device_id is None and _is_current_resource_fallback(
            device_registry,
            entity_entry.device_id,
            entry.entry_id,
            resource_key,
        ):
            continue
        if entity_entry.device_id == target_device_id:
            continue
        entity_registry.async_update_entity(
            entity_entry.entity_id,
            device_id=target_device_id,
        )
        moved_count += 1

    current_fallback_identifiers = _current_beestat_device_identifiers(data)
    removed_count = 0
    for device_entry in dr.async_entries_for_config_entry(
        device_registry,
        entry.entry_id,
    ):
        if not is_beestat_only_device(device_entry, entry.entry_id):
            continue
        beestat_identifiers = set(device_entry.identifiers)
        if not beestat_identifiers.isdisjoint(current_fallback_identifiers):
            continue
        device_registry.async_remove_device(device_entry.id)
        removed_count += 1
    if moved_count or removed_count:
        _LOGGER.info(
            "Reconciled HomeKit/Ecobee device mapping: moved %s entity record(s), "
            "removed %s stale fallback device record(s)",
            moved_count,
            removed_count,
        )


def _mapped_resource_device_ids(
    data: BeestatRuntimeData,
) -> dict[tuple[str, str], str | None]:
    """Return target devices for every stable thermostat and sensor identity."""

    return {
        **{
            ("thermostat", str(thermostat.thermostat_id)): thermostat.device_id
            for thermostat in data.config.thermostats
        },
        **{
            ("sensor", str(sensor.sensor_id)): sensor.device_id
            for sensor in data.config.sensors
        },
    }


def _is_current_resource_fallback(
    registry: dr.DeviceRegistry,
    device_id: str | None,
    entry_id: str,
    resource_key: tuple[str, str],
) -> bool:
    """Preserve only the current resource's exclusively owned fallback device."""

    return (
        device_id is not None
        and (device := registry.async_get(device_id)) is not None
        and is_beestat_only_device(device, entry_id)
        and device.identifiers == {(DOMAIN, "_".join(resource_key))}
    )


def _mapped_source_entity_ids(data: BeestatRuntimeData | None) -> set[str]:
    """Return source registry entities whose association drives helper linking."""

    if data is None:
        return set()
    entity_ids: set[str] = set()
    for thermostat in data.config.thermostats:
        entity_ids.update(_configured_source_entity_ids(thermostat))
    for sensor in data.config.sensors:
        entity_ids.update(_configured_source_entity_ids(sensor))
    return entity_ids


def _configured_source_entity_ids(
    item: ConfiguredThermostat | ConfiguredSensor,
) -> set[str]:
    """Return explicitly or automatically selected source entities."""

    references = [
        item.temperature_entity_id,
        item.occupancy_entity_id,
        item.motion_entity_id,
    ]
    if isinstance(item, ConfiguredThermostat):
        references.append(item.climate_entity_id)
    return {reference for reference in references if reference is not None}


def _mapped_source_device_ids(data: BeestatRuntimeData | None) -> set[str]:
    """Return current source device IDs that drive helper linking."""

    if data is None:
        return set()
    return {
        device_id
        for device_id in (
            *(item.device_id for item in data.config.thermostats),
            *(item.device_id for item in data.config.sensors),
        )
        if device_id is not None
    }


@callback
def _room_temperature_entity_ids(data: BeestatRuntimeData | None) -> set[str]:
    """Return mapped temperature sources used by profile-aware projections."""

    if data is None:
        return set()
    return {
        entity_id
        for entity_id in (
            *(item.temperature_entity_id for item in data.config.thermostats),
            *(item.temperature_entity_id for item in data.config.sensors),
        )
        if entity_id is not None
    }


@callback
def _async_track_room_temperature_sources(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> Callable[[], None]:
    """Reproject current-profile spreads on local temperature state changes."""

    @callback
    def handle_temperature_change(_event: Event[Any]) -> None:
        entry.runtime_data.coordinator.async_rebuild_runtime_from_cached_rows()

    return _async_track_runtime_entity_states(
        hass,
        entry,
        _room_temperature_entity_ids,
        handle_temperature_change,
    )


@callback
def _async_track_runtime_entity_states(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
    sources: Callable[[BeestatRuntimeData | None], Iterable[str]],
    action: Callable[[Event[Any]], None],
) -> Callable[[], None]:
    """Keep one state listener aligned with the current normalized source set."""

    coordinator = entry.runtime_data.coordinator
    tracked_entity_ids: tuple[str, ...] = ()
    remove_state_listener: Callable[[], None] | None = None

    @callback
    def rebind_state_listener() -> None:
        nonlocal tracked_entity_ids, remove_state_listener
        entity_ids = tuple(sorted(set(sources(coordinator.data))))
        if entity_ids == tracked_entity_ids:
            return
        if remove_state_listener is not None:
            remove_state_listener()
        tracked_entity_ids = entity_ids
        remove_state_listener = (
            async_track_state_change_event(
                hass,
                entity_ids,
                action,
            )
            if entity_ids
            else None
        )

    rebind_state_listener()
    remove_coordinator_listener = coordinator.async_add_listener(rebind_state_listener)

    @callback
    def remove() -> None:
        remove_coordinator_listener()
        if remove_state_listener is not None:
            remove_state_listener()

    entry.async_on_unload(remove)
    return remove


@callback
def _async_track_source_device_relinks(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> tuple[Callable[[], None], ...]:
    """Rebind existing helpers when a mapped foreign source association changes."""

    coordinator = entry.runtime_data.coordinator
    watched_entity_ids = _mapped_source_entity_ids(coordinator.data)
    entity_registry = er.async_get(hass)
    stable_references = configured_entity_references(entry_runtime_config_data(entry))
    watched_entity_ids.update(
        configured_override_entity_ids(
            entry_runtime_config_data(entry),
            entity_registry=entity_registry,
        )
    )
    watched_device_ids = _mapped_source_device_ids(coordinator.data)

    @callback
    def track_registered_devices() -> None:
        # The physical probe may belong to the paired Ecobee registration while
        # enrichment remains attached to HomeKit. Observe identity changes on both.
        # Unselected candidate thermostats can introduce or remove ambiguity.
        watched_entity_ids.update(
            source.entity_id
            for source in entity_registry.entities.values()
            if is_thermostat_identity_source(source)
        )
        watched_device_ids.update(
            source.device_id
            for entity_id in watched_entity_ids
            if (source := entity_registry.async_get(entity_id)) is not None
            and source.device_id is not None
        )

    track_registered_devices()

    @callback
    def handle_coordinator_update() -> None:
        data = coordinator.data
        watched_entity_ids.update(_mapped_source_entity_ids(data))
        watched_entity_ids.update(
            configured_override_entity_ids(
                entry_runtime_config_data(entry),
                entity_registry=entity_registry,
            )
        )
        watched_device_ids.update(_mapped_source_device_ids(data))
        track_registered_devices()
        _async_migrate_homekit_device_assignments(hass, entry, data)
        _async_update_mapping_device_conflicts_issue(hass, entry)

    @callback
    def reconcile_assignments() -> None:
        coordinator.async_rebuild_runtime_from_cached_rows()

    @callback
    def handle_entity_registry_update(event: Event[Any]) -> None:
        changed_entity_ids = {
            str(value)
            for key in ("entity_id", "old_entity_id")
            if (value := event.data.get(key)) is not None
        }
        if any(
            is_thermostat_identity_source(entity_registry.async_get(entity_id))
            for entity_id in changed_entity_ids
        ):
            track_registered_devices()
        if watched_entity_ids.isdisjoint(
            changed_entity_ids
        ) and not _entity_registry_event_matches_references(
            entity_registry,
            changed_entity_ids,
            stable_references,
        ):
            return
        reconcile_assignments()

    @callback
    def handle_device_registry_update(event: Event[Any]) -> None:
        device_id = event.data.get("device_id")
        if device_id is None or str(device_id) not in watched_device_ids:
            return
        reconcile_assignments()

    removers = (
        coordinator.async_add_listener(handle_coordinator_update),
        hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            handle_entity_registry_update,
        ),
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            handle_device_registry_update,
        ),
    )
    for remove_listener in removers:
        entry.async_on_unload(remove_listener)
    return removers


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


def _filter_changed_entity_ids(data: BeestatRuntimeData | None) -> set[str]:
    if data is None:
        return set()
    return {
        thermostat.filter_changed_entity_id
        for thermostat in data.config.thermostats
        if thermostat.filter_changed_entity_id is not None
    }


@callback
def _async_update_override_issues(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> None:
    _async_update_missing_override_entity_issue(hass, entry)
    _async_update_invalid_override_domain_issue(hass, entry)
    _async_update_mapping_device_conflicts_issue(hass, entry)


@callback
def _async_track_override_issue_updates(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> None:
    """Refresh mapping Repairs when a referenced registry entity changes."""

    entity_registry = er.async_get(hass)
    config_data = entry_runtime_config_data(entry)
    watched_entity_ids = set(
        configured_override_entity_ids(
            config_data,
            entity_registry=entity_registry,
        )
    )
    stable_references = configured_entity_references(config_data)
    if not watched_entity_ids and not stable_references:
        return

    @callback
    def handle_registry_update(event: Event[Any]) -> None:
        changed_entity_ids = {
            str(value)
            for key in ("entity_id", "old_entity_id")
            if (value := event.data.get(key)) is not None
        }
        if watched_entity_ids.isdisjoint(
            changed_entity_ids
        ) and not _entity_registry_event_matches_references(
            entity_registry,
            changed_entity_ids,
            stable_references,
        ):
            return
        _async_update_override_issues(hass, entry)
        watched_entity_ids.update(
            configured_override_entity_ids(
                config_data,
                entity_registry=entity_registry,
            )
        )

    entry.async_on_unload(
        hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            handle_registry_update,
        )
    )


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


@callback
def _async_update_mapping_device_conflicts_issue(
    hass: HomeAssistant,
    entry: BeestatStatisticsConfigEntry,
) -> None:
    """Create or clear the Repair for inconsistent explicit source devices."""

    runtime = getattr(entry, "runtime_data", None)
    data = getattr(getattr(runtime, "coordinator", None), "data", None)
    config = entry_runtime_config_data(entry)
    if (
        data is not None
        and hasattr(data, "thermostat_rows")
        and hasattr(data, "sensor_rows")
    ):
        conflicts = build_beestat_config(
            hass, data.thermostat_rows, data.sensor_rows, config
        ).mapping_device_conflicts
    else:
        conflicts = configured_mapping_device_conflicts(
            config,
            er.async_get(hass),
            dr.async_get(hass),
        )
    if not conflicts:
        ir.async_delete_issue(hass, DOMAIN, _MAPPING_DEVICE_CONFLICTS_ISSUE_ID)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        _MAPPING_DEVICE_CONFLICTS_ISSUE_ID,
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_MAPPING_DEVICE_CONFLICTS_ISSUE_ID,
        translation_placeholders={"conflict_count": str(len(conflicts))},
    )


def _missing_override_entity_ids(
    hass: HomeAssistant,
    config_data: Mapping[str, Any],
) -> tuple[str, ...]:
    registry = er.async_get(hass)
    unresolved = frozenset(configured_unresolved_entity_ids(config_data, registry))
    return tuple(
        entity_id
        for entity_id in configured_override_entity_ids(
            config_data,
            entity_registry=registry,
        )
        if entity_id in unresolved
        or (
            hass.states.get(entity_id) is None and registry.async_get(entity_id) is None
        )
    )


def _entity_registry_event_matches_references(
    registry: er.EntityRegistry,
    changed_entity_ids: set[str],
    references: tuple[Mapping[str, Any], ...],
) -> bool:
    """Return whether a current registry event restores a stable source identity."""

    for entity_id in changed_entity_ids:
        entry = registry.async_get(entity_id)
        if entry is not None and any(
            entity_reference_matches_entry(reference, entry) for reference in references
        ):
            return True
    return False


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
        row for row in rows if _row_int(row, "thermostat_id", "id") == thermostat_id
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
                end_day=None if item.metadata.get("has_sum") else end_day,
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
    return end_day is None or local_day <= end_day


def _point_window(
    lookback_days: int,
    local_tz: ZoneInfo,
    start_day: dt_date | None,
    end_day: dt_date | None,
    *,
    evaluated_at: datetime,
) -> tuple[datetime, datetime]:
    end = evaluated_at
    if end_day is not None:
        end = _local_midnight(end_day + timedelta(days=1), local_tz).astimezone(UTC)
    if start_day is None:
        local_start_day = end.astimezone(local_tz).date() - timedelta(
            days=lookback_days,
        )
    else:
        local_start_day = start_day
    start = _local_midnight(local_start_day, local_tz).astimezone(UTC)
    return start, end


def _hourly_utc_hour(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Hourly bounds require an explicit UTC offset")
    result = value.astimezone(UTC)
    if result.minute or result.second or result.microsecond:
        raise ValueError("Hourly bounds must be whole UTC hours")
    return result


def _validate_hourly_window(start: datetime, end: datetime) -> None:
    if not timedelta(0) < end - start <= timedelta(days=MAX_POINT_LOOKBACK_DAYS):
        raise ValueError(
            "Hourly bounds must span more than zero and at most 366 elapsed days"
        )


def _hourly_window(
    context: TemporalContext,
    *,
    lookback_days: int,
    rebuild_start: dt_date | None,
    rebuild_end: dt_date | None,
    epoch_start: datetime | None,
    bootstrap_start: datetime | None = None,
) -> tuple[datetime, datetime, datetime | None]:
    end = context.evaluated_at.astimezone(UTC).replace(
        minute=0, second=0, microsecond=0
    )
    if epoch_start is not None:
        start = _hourly_utc_hour(epoch_start)
    elif rebuild_start is not None:
        start = _hourly_utc_hour(_local_midnight(rebuild_start, context.local_tz))
    else:
        start = end - timedelta(days=lookback_days)
        if bootstrap_start is not None:
            start = min(start, _hourly_utc_hour(bootstrap_start))
    _validate_hourly_window(start, end)
    measurement_end = None
    if rebuild_end is not None:
        measurement_end = min(
            end,
            _hourly_utc_hour(
                _local_midnight(rebuild_end + timedelta(days=1), context.local_tz)
            ),
        )
        _validate_hourly_window(start, measurement_end)
    return start, end, measurement_end


def _observed_hourly_horizons(
    rows_by_id: Mapping[int, list[dict[str, Any]]], caps: Mapping[int, datetime]
) -> dict[int, datetime]:
    result: dict[int, datetime] = {}
    for thermostat_id, rows in rows_by_id.items():
        stamps = [
            stamp
            for row in rows
            if _row_int(row, "thermostat_id") == thermostat_id
            and isinstance(row.get("timestamp"), str)
            and (stamp := _parse_beestat_time(row["timestamp"])) is not None
            and not (stamp.minute % 5 or stamp.second or stamp.microsecond)
            and (thermostat_id not in caps or stamp <= caps[thermostat_id])
        ]
        if stamps:
            result[thermostat_id] = max(stamps)
    return result


def _hourly_retained_ids(
    config: BeestatConfig, retained: tuple[str, ...]
) -> tuple[str, ...]:
    # Retain selected detailed quantities even after a display-derived slug changes.
    quantities = {
        key
        for key, _label, _field in DETAILED_RUNTIME_FIELDS
        if any(value.endswith(f"_{key}_runtime_hours_hourly_v2") for value in retained)
    }
    return (
        *retained,
        *(
            f"beestat:{thermostat.slug}_{key}_runtime_hours_hourly_v2"
            for thermostat in config.thermostats
            for key in sorted(quantities)
        ),
    )


def _hourly_resource_identities(config: BeestatConfig) -> dict[str, dict[str, Any]]:
    quantities = {
        *(f"{key}_runtime_hours" for key, _label, _fields in RUNTIME_FIELD_GROUPS),
        *(f"{key}_runtime_hours" for key, _label, _field in DETAILED_RUNTIME_FIELDS),
        *(spec.statistic_suffix for spec in SUMMARY_MEAN_STATISTICS),
        *(spec.statistic_suffix for spec in SUMMARY_SUM_STATISTICS),
        *(spec.statistic_suffix for spec in THERMOSTAT_POINT_STATISTICS),
    }
    candidates = [
        (
            f"beestat:{thermostat.slug}_{quantity}_hourly_v2",
            {
                "thermostat_id": thermostat.thermostat_id,
                "sensor_id": None,
                "quantity": quantity,
            },
        )
        for thermostat in config.thermostats
        for quantity in quantities
    ]
    sensors = {sensor.sensor_id: sensor for sensor in config.sensors}
    candidates.extend(
        (
            f"beestat:{spec.statistic_suffix}_hourly_v2",
            {
                "thermostat_id": sensors[spec.sensor_id].thermostat_id,
                "sensor_id": spec.sensor_id,
                "quantity": spec.field,
            },
        )
        for spec in build_sensor_specs(config)
    )
    if len({key for key, _value in candidates}) != len(candidates):
        raise ValueError("Hourly statistic identity is ambiguous")
    return dict(candidates)


def _hourly_identity(
    entry: BeestatStatisticsConfigEntry,
    data: BeestatRuntimeData,
    series: tuple[HourlySeries, ...],
    *,
    selected_thermostat_id: int | None = None,
    require_account: bool = True,
) -> dict[str, Any]:
    anchors = sorted(
        {
            hashlib.sha256(str(resource_id).encode()).hexdigest()
            for row in data.thermostat_rows
            if (resource_id := _row_int(row, "thermostat_id", "id")) is not None
        }
    )
    if not anchors and require_account:
        raise ValueError("Hourly account identity is unavailable")
    parsed = urlsplit(normalize_api_base(entry.data.get(CONF_API_BASE, API_BASE)))
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    origin = f"https://{host.lower()}" + (
        f":{parsed.port}" if parsed.port not in (None, 443) else ""
    )
    resources = _hourly_resource_identities(data.config)
    selected = {item.statistic_id: resources[item.statistic_id] for item in series}
    return {
        "entry_id": entry.entry_id,
        "api_base": origin,
        "account_anchors": anchors,
        "resources": selected,
        "selected_thermostat_id": selected_thermostat_id,
    }


def _writer_identity(
    entry: BeestatStatisticsConfigEntry, data: BeestatRuntimeData
) -> dict[str, Any]:
    """Map eligible quantities separately from stable cached sensor topology."""

    identity = _hourly_identity(entry, data, (), require_account=False)
    identity["resources"] = _hourly_resource_identities(data.config)
    parents: dict[int, int | None] = {}
    for row in data.sensor_rows:
        sensor_id = positive_resource_id(row.get("sensor_id", row.get("id")))
        if sensor_id is None:
            continue
        parent = positive_resource_id(row.get("thermostat_id"))
        if sensor_id in parents and parents[sensor_id] != parent:
            raise ValueError("Sensor parent identity is ambiguous")
        parents[sensor_id] = parent
    for sensor in data.config.sensors:
        # Keep provider topology independent of configured overrides. The
        # manager checks both against adopted resources; unadmitted legacy
        # quantities retain their existing configuration semantics.
        parents.setdefault(sensor.sensor_id, sensor.thermostat_id)
    identity["sensor_parents"] = parents
    return identity


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


def _row_start_datetime(row: Mapping[str, Any]) -> datetime | None:
    value = row.get("start")
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        return None
    else:
        timestamp = _row_float(value)
        if timestamp is None:
            return None
        try:
            parsed = datetime.fromtimestamp(timestamp, UTC)
        except OverflowError, OSError, ValueError:
            return None
    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except OverflowError, OSError, ValueError:
        return None


def _row_float(value: Any) -> float | None:
    if value is None or value in ("", "unknown", "unavailable"):
        return None
    try:
        parsed = float(value)
    except OverflowError, TypeError, ValueError:
        return None
    return parsed if isfinite(parsed) else None


def _format_day(value: dt_date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _entry_point_lookback_days(entry: BeestatStatisticsConfigEntry) -> int:
    return normalize_point_lookback_days(entry.options.get(CONF_POINT_LOOKBACK_DAYS))


def _entry_scan_interval_seconds(entry: BeestatStatisticsConfigEntry) -> int:
    return normalize_scan_interval_seconds(
        entry.options.get(CONF_SCAN_INTERVAL_SECONDS)
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
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _sensor_thermostat_map(rows: list[dict[str, Any]]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for row in rows:
        sensor_id = _row_int(row, "sensor_id", "id")
        thermostat_id = _row_int(row, "thermostat_id")
        if sensor_id is not None and thermostat_id is not None:
            mapping[sensor_id] = thermostat_id
    return mapping


def _thermostat_data_end_map(rows: list[dict[str, Any]]) -> dict[int, datetime]:
    mapping: dict[int, datetime] = {}
    for row in rows:
        thermostat_id = _row_int(row, "thermostat_id", "id")
        data_end = _parse_beestat_time(row.get("data_end"))
        if thermostat_id is not None and data_end is not None:
            mapping[thermostat_id] = data_end
    return mapping


def _row_int(row: dict[str, Any], *fields: str) -> int | None:
    for field in fields:
        if (value := positive_resource_id(row.get(field))) is not None:
            return value
    return None


def _parse_beestat_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC)
    except OverflowError, ValueError:
        return None


def _dedupe_rows(rows: list[dict[str, Any]], *, id_field: str) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key: tuple[Any, ...]
        if (runtime_sensor_id := _row_int(row, "runtime_sensor_id")) is not None:
            key = ("runtime_sensor_id", runtime_sensor_id)
        elif (
            runtime_thermostat_id := _row_int(row, "runtime_thermostat_id")
        ) is not None:
            key = ("runtime_thermostat_id", runtime_thermostat_id)
        elif (resource_id := _row_int(row, id_field)) is not None and (
            timestamp := _parse_beestat_time(row.get("timestamp"))
        ) is not None:
            key = (id_field, resource_id, "timestamp", timestamp)
        else:
            key = (
                "row",
                tuple(sorted((str(key), str(value)) for key, value in row.items())),
            )
        deduped[key] = row
    return sorted(
        (row for row in deduped.values() if not row.get("deleted")),
        key=lambda row: (str(row.get("timestamp", "")), str(row.get(id_field, ""))),
    )


def _format_start(series: StatisticsSeries) -> str | None:
    latest = series.latest_start
    return latest.isoformat() if latest else None

"""Home Assistant service actions for Beestat Statistics."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast

import probatio
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import BeestatApiError, BeestatAuthError, exception_fingerprint
from .config_rows import positive_resource_id
from .configuration import configuration_response
from .const import (
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
    CONF_POINT_LOOKBACK_DAYS,
    CONF_THERMOSTAT_ID,
    DOMAIN,
    MAX_POINT_LOOKBACK_DAYS,
    SERVICE_GET_CONFIGURATION,
    SERVICE_GET_HOURLY_COVERAGE,
    SERVICE_GET_RAW_POINTS,
    SERVICE_IMPORT_STATISTICS,
    SERVICE_REBUILD_STATISTICS,
    SERVICE_RECORD_FILTER_CHANGE,
    SERVICE_REPAIR_FILTER_CHANGE_BOUNDARY,
    SERVICE_SELECT_HOURLY_STATISTICS,
)
from .coordinator import BeestatRuntimeDataCoordinator
from .entry_options import (
    FilterChangeConflictError,
    async_mark_filter_changed,
    resolve_filter_change_timestamp,
    saved_filter_boundary,
)
from .filter_forecast import build_filter_forecast, filter_forecast_quality_attributes
from .hourly_history_contract import (
    SERVICE_APPLY_HOURLY_HISTORY,
    SERVICE_PLAN_HOURLY_HISTORY,
    SERVICE_STAGE_HOURLY_SOURCE,
)
from .hourly_history_service import (
    APPLY_HISTORY_SCHEMA,
    COVERAGE_HISTORY_SCHEMA,
    PLAN_HISTORY_SCHEMA,
    STAGE_HISTORY_SCHEMA,
)
from .import_support import UnknownThermostatError
from .importer import BeestatStatisticsImporter
from .raw_points import parse_raw_point_request
from .runtime import (
    BeestatStatisticsRuntime,
    entry_point_lookback_days,
    entry_scan_interval_seconds,
)

_LOGGER = logging.getLogger(__name__)

IMPORT_SERVICE_SCHEMA = probatio.Schema(
    {
        probatio.Optional(CONF_POINT_LOOKBACK_DAYS): probatio.All(
            probatio.Coerce(int),
            probatio.Range(min=1, max=MAX_POINT_LOOKBACK_DAYS),
        ),
        probatio.Optional(ATTR_SKIP_SYNC, default=False): cv.boolean,
    }
)

REBUILD_SERVICE_SCHEMA = probatio.Schema(
    {
        probatio.Optional(CONF_THERMOSTAT_ID): probatio.Coerce(int),
        probatio.Optional(ATTR_START_DATE): cv.date,
        probatio.Optional(ATTR_END_DATE): cv.date,
        probatio.Optional(ATTR_SKIP_SYNC, default=False): cv.boolean,
    }
)

GET_CONFIGURATION_SERVICE_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


def _hourly_revision(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise probatio.Invalid("The expected revision must be a non-negative integer")
    return value


SELECT_HOURLY_SERVICE_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        probatio.Required(ATTR_EPOCH_START): cv.datetime,
        probatio.Required(ATTR_STATISTIC_IDS): probatio.All(
            [cv.string], probatio.Length(min=1)
        ),
        probatio.Required(ATTR_EXPECTED_REVISION): _hourly_revision,
        probatio.Optional(ATTR_PREVIEW_DIGEST): cv.string,
    }
)

GET_HOURLY_COVERAGE_SERVICE_SCHEMA = probatio.Any(
    COVERAGE_HISTORY_SCHEMA,
    probatio.Schema(
        {
            probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
            probatio.Required(ATTR_START): cv.datetime,
            probatio.Required(ATTR_END): cv.datetime,
            probatio.Optional(ATTR_STATISTIC_IDS): probatio.All(
                [cv.string], probatio.Length(min=1)
            ),
        }
    ),
)

GET_RAW_POINTS_SERVICE_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        probatio.Required("resource"): probatio.In(
            ("runtime_thermostat", "runtime_sensor")
        ),
        probatio.Required("resource_id"): int,
        probatio.Required(ATTR_START): cv.datetime,
        probatio.Required(ATTR_END): cv.datetime,
    }
)

REPAIR_FILTER_CHANGE_BOUNDARY_SERVICE_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        probatio.Required(CONF_THERMOSTAT_ID): probatio.Coerce(int),
        probatio.Required(ATTR_CHANGED_AT): cv.datetime,
    }
)


def _positive_thermostat_id(value: Any) -> int:
    if (thermostat_id := positive_resource_id(value)) is None:
        raise probatio.Invalid("thermostat_id must be an exact positive integer")
    return thermostat_id


RECORD_FILTER_CHANGE_SERVICE_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        probatio.Required(CONF_THERMOSTAT_ID): _positive_thermostat_id,
        probatio.Required(ATTR_CHANGED_AT): cv.datetime,
        probatio.Required(ATTR_EXPECTED_CHANGED_AT): probatio.Any(None, cv.datetime),
        probatio.Required(ATTR_EXPECTED_CHANGED_DATE): probatio.Any(None, cv.date),
        probatio.Required(ATTR_EXPECTED_REQUEST_ID): probatio.Any(
            None, probatio.All(cv.string, probatio.Length(min=1, max=128))
        ),
        probatio.Required(ATTR_REQUEST_ID): probatio.All(
            cv.string, probatio.Length(min=1, max=128)
        ),
    }
)


def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's domain-level actions."""

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


def _first_runtime(hass: HomeAssistant) -> BeestatStatisticsRuntime | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        runtime: BeestatStatisticsRuntime | None = getattr(entry, "runtime_data", None)
        if runtime is not None:
            return runtime
    return None


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
            skip_sync=call.data[ATTR_SKIP_SYNC],
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
        point_lookback_days=entry_point_lookback_days(entry),
        scan_interval_seconds=entry_scan_interval_seconds(entry),
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
    try:
        if call.data.get("contract_version") == 3:
            return await importer.async_get_hourly_history(dict(call.data))
        if call.service == SERVICE_SELECT_HOURLY_STATISTICS:
            return await importer.async_select_hourly_statistics(
                epoch_start=call.data[ATTR_EPOCH_START],
                statistic_ids=tuple(call.data[ATTR_STATISTIC_IDS]),
                expected_revision=call.data[ATTR_EXPECTED_REVISION],
                preview_digest=call.data.get(ATTR_PREVIEW_DIGEST),
            )
        ids = call.data.get(ATTR_STATISTIC_IDS)
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
            skip_sync=call.data[ATTR_SKIP_SYNC],
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

    thermostat_id = call.data[CONF_THERMOSTAT_ID]
    coordinator = _loaded_filter_coordinator(
        hass, call.data[ATTR_CONFIG_ENTRY_ID], thermostat_id
    )
    changed_at = call.data[ATTR_CHANGED_AT]
    try:
        changed_at = resolve_filter_change_timestamp(
            changed_at,
            coordinator.local_tz,
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
    prior_boundary = saved_filter_boundary(coordinator, thermostat_id)
    saved_date = prior_boundary[1]
    repair_date = changed_at.astimezone(coordinator.local_tz).date()
    if saved_date is None or saved_date != repair_date:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="filter_change_boundary_date_mismatch",
        )
    await async_mark_filter_changed(
        coordinator,
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

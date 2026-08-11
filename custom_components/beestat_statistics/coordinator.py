"""Runtime-summary coordinator for Beestat native Home Assistant entities."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, time, timedelta
from math import fsum, isfinite
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    BeestatApiError,
    BeestatAuthError,
    BeestatClient,
    exception_fingerprint,
)
from .config_model import (
    BeestatConfig,
    ConfiguredSensor,
    ConfiguredThermostat,
    build_beestat_config,
)
from .config_payload import (
    effective_thermostat_override,
    entry_runtime_config_data,
    update_thermostat_override_options,
)
from .const import (
    CLOUD_DATA_STALE_GRACE_MINUTES,
    CLOUD_DATA_STALE_MINIMUM_MINUTES,
    CONF_FILTER_CHANGE_BOUNDARY_RECONCILED_AT,
    CONF_FILTER_CHANGE_BOUNDARY_SOURCE_DATA_END,
    CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS,
    CONF_FILTER_CHANGED_AT,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    FILTER_RECENT_RUNTIME_DAYS,
)
from .filter_forecast import build_filter_forecast
from .profile import ScheduleProfile, schedule_profiles_by_ref
from .thermostat_settings import (
    ThermostatSettingsSnapshot,
    build_thermostat_settings_snapshots,
)

_LOGGER = logging.getLogger(__name__)
_FILTER_BOUNDARY_RETRY_DELAY = timedelta(minutes=15)
_FILTER_BOUNDARY_FAST_RETRY_WINDOW = timedelta(hours=6)
_SUMMARY_TEMPORAL_CONTEXT_ATTEMPTS = 2

if TYPE_CHECKING:
    from .runtime import BeestatStatisticsConfigEntry


@dataclass(frozen=True, slots=True)
class ThermostatRuntimeSummary:
    """Derived daily runtime summary values for one Beestat thermostat."""

    thermostat_id: int
    slug: str
    label: str
    latest_date: date | None
    lag_days: int | None
    filter_changed_date: date | None
    filter_changed_source: str | None
    filter_runtime_hours: float | None
    recent_runtime_hours_per_day: float | None


@dataclass(frozen=True, slots=True)
class ProfileSensorReference:
    """One Ecobee comfort-profile sensor without exposing it to diagnostics."""

    identifier: str | None
    name: str | None


@dataclass(frozen=True, slots=True)
class ThermostatMetadata:
    """Beestat thermostat metadata useful as native HA status."""

    thermostat_id: int
    slug: str
    label: str
    data_begin: datetime | None
    data_end: datetime | None
    data_lag_minutes: int | None
    current_climate_ref: str | None
    current_climate_name: str | None
    scheduled_climate_ref: str | None
    scheduled_climate_name: str | None
    next_scheduled_climate_ref: str | None
    next_scheduled_climate_name: str | None
    next_scheduled_at: datetime | None
    schedule_profiles: tuple[ScheduleProfile, ...]
    active_sensor_count: int
    active_sensor_names: tuple[str, ...]
    current_profile_sensor_names: tuple[str, ...]
    active_alert_count: int
    active_alerts: tuple[dict[str, Any], ...]
    current_profile_sensors: tuple[ProfileSensorReference, ...] = ()


@dataclass(frozen=True, slots=True)
class SensorMetadata:
    """Beestat sensor metadata useful for comfort-profile diagnostics."""

    sensor_id: int
    thermostat_id: int | None
    name: str | None
    identifier: str | None
    sensor_type: str | None
    in_use: bool
    inactive: bool
    deleted: bool


@dataclass(frozen=True, slots=True)
class RoomTemperatureSpread:
    """Local temperatures for the sensors participating in the current profile."""

    value: float | None
    unit: str | None
    participating_sensor_count: int
    valid_sensor_count: int
    participating_sensor_names: tuple[str, ...]
    unavailable_sensor_names: tuple[str, ...]
    hottest_sensor_name: str | None
    coldest_sensor_name: str | None


@dataclass(frozen=True, slots=True)
class BeestatRuntimeData:
    """Latest Beestat runtime summary readback."""

    config: BeestatConfig
    fetched_at: datetime
    projected_at: datetime
    sync_success_at: datetime | None
    metadata_sync_success_at: datetime | None
    summary_rows: tuple[dict[str, Any], ...]
    summary_rows_full: bool
    summary_window_start: date | None
    summary_window_end: date | None
    thermostat_rows: tuple[dict[str, Any], ...]
    sensor_rows: tuple[dict[str, Any], ...]
    summary_row_count: int
    thermostats: dict[int, ThermostatRuntimeSummary]
    thermostat_metadata: dict[int, ThermostatMetadata]
    sensor_metadata: dict[int, SensorMetadata]
    thermostat_settings: dict[int, ThermostatSettingsSnapshot] = dataclass_field(
        default_factory=dict
    )
    room_temperature_spreads: dict[int, RoomTemperatureSpread] = dataclass_field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class RawFilterBoundary:
    """One reconciled raw-runtime boundary at Beestat's source resolution."""

    baseline_seconds: float
    effective_at: datetime
    source_data_end: datetime


@dataclass(frozen=True, slots=True)
class TemporalContext:
    """One immutable clock and timezone revision for derived local state."""

    evaluated_at: datetime
    local_tz: ZoneInfo
    timezone_revision: int


def _typed_config_entry(coordinator: Any) -> BeestatStatisticsConfigEntry:
    """Return the coordinator entry for both runtime and lightweight test doubles."""

    entry = getattr(coordinator, "_beestat_config_entry", None)
    if entry is None:
        entry = coordinator.config_entry
    return cast("BeestatStatisticsConfigEntry", entry)


class BeestatRuntimeDataCoordinator(DataUpdateCoordinator[BeestatRuntimeData]):
    """Coordinate Beestat runtime sync/read calls for sensors and imports."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: BeestatStatisticsConfigEntry,
        client: BeestatClient,
        *,
        local_tz: ZoneInfo,
        scan_interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_runtime",
            config_entry=config_entry,
        )
        self._client = client
        self._beestat_config_entry = config_entry
        self._local_tz = local_tz
        self._cloud_data_stale_threshold_minutes = cloud_data_stale_threshold_minutes(
            scan_interval_seconds
        )
        self._timezone_revision = 0
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.last_import_success_at: datetime | None = None
        self.last_imported_series: int | None = None
        self.last_imported_rows: int | None = None
        self.last_import_source_rows: int | None = None
        self.last_import_partial: bool | None = None
        self.last_import_skipped_windows: int | None = None
        self.last_import_skipped_runtime_thermostat_windows: int | None = None
        self.last_import_skipped_runtime_sensor_windows: int | None = None
        self.last_import_skipped_window_examples: tuple[dict[str, str], ...] = ()
        self.last_import_summary_mode: str | None = None
        self.last_import_summary_window_start: str | None = None
        self.last_import_summary_window_end: str | None = None
        self.last_import_summary_overlap_days: int | None = None
        self.last_import_summary_fallback_reason: str | None = None
        self.last_import_cumulative_seed_count: int | None = None
        self.last_filter_alert_dismiss_attempt_at: datetime | None = None
        self.last_filter_alert_dismiss_thermostat_id: int | None = None
        self.last_filter_alert_dismiss_matched: int | None = None
        self.last_filter_alert_dismissed: int | None = None
        self.last_filter_alert_dismiss_error: str | None = None
        self.last_filter_boundary_reconcile_attempt_at: datetime | None = None
        self.last_filter_boundary_reconciled_count: int = 0
        self.last_filter_boundary_pending_count: int = 0
        self.last_filter_boundary_reconcile_error: str | None = None
        self._cancel_filter_boundary_retry: Callable[[], None] | None = None
        self._cancel_projection_boundary: Callable[[], None] | None = None
        config_entry.async_on_unload(self._async_cancel_filter_boundary_retry)
        config_entry.async_on_unload(self._async_cancel_projection_boundary)

    @property
    def status(self) -> str:
        """Return a compact operator status."""

        if self.last_error is not None:
            return "error"
        if self.data is None:
            return "unknown"
        return "ok"

    @property
    def local_tz(self) -> ZoneInfo:
        """Return the Home Assistant local time zone used for Beestat dates."""

        return self._local_tz

    @property
    def cloud_data_stale_threshold_minutes(self) -> int:
        """Return the cadence-aware Beestat source-lag threshold."""

        return getattr(
            self,
            "_cloud_data_stale_threshold_minutes",
            CLOUD_DATA_STALE_MINIMUM_MINUTES,
        )

    @callback
    def capture_temporal_context(self) -> TemporalContext:
        """Capture one clock and timezone revision for an awaited operation."""

        return TemporalContext(
            evaluated_at=datetime.now(UTC),
            local_tz=self._local_tz,
            timezone_revision=self._timezone_revision,
        )

    @callback
    def temporal_context_is_current(self, context: TemporalContext) -> bool:
        """Return whether a captured timezone revision is still current."""

        return context.timezone_revision == self._timezone_revision

    @callback
    def async_update_local_timezone(self, local_tz: ZoneInfo) -> None:
        """Reproject cached state after Home Assistant's timezone changes."""

        if local_tz == self._local_tz:
            return
        previous_local_tz = self._local_tz
        self._local_tz = local_tz
        self._timezone_revision += 1
        self._async_cancel_projection_boundary()
        self._async_rebuild_projection_from_cached(
            datetime.now(UTC),
            previous_local_tz=previous_local_tz,
        )

    @property
    def beestat_config_entry(self) -> BeestatStatisticsConfigEntry:
        """Return the non-optional config entry supplied at construction."""

        return self._beestat_config_entry

    def filter_runtime_seconds_on_date(
        self,
        thermostat_id: int,
        target_date: date,
    ) -> float | None:
        """Return the freshest known daily fan-runtime total for a thermostat."""

        if self.data is None:
            return None
        return _runtime_seconds_on_date(
            self.data.summary_rows,
            thermostat_id=thermostat_id,
            target_date=target_date,
        )

    @callback
    def async_schedule_filter_boundary_reconcile(
        self,
        config: BeestatConfig | None = None,
    ) -> None:
        """Schedule one bounded retry while an exact boundary is pending."""

        if (
            self._cancel_filter_boundary_retry is not None
            or not self._has_fast_retryable_filter_boundary(config)
        ):
            return
        self._cancel_filter_boundary_retry = async_call_later(
            self.hass,
            _FILTER_BOUNDARY_RETRY_DELAY,
            self._async_retry_filter_boundaries,
        )

    @callback
    def _has_fast_retryable_filter_boundary(
        self,
        config: BeestatConfig | None = None,
    ) -> bool:
        """Return whether one recent click still merits 15-minute retries."""

        if config is None:
            data = self.data
            if data is None:
                return False
            config = data.config
        now = datetime.now(UTC)
        for thermostat in config.thermostats:
            current_override = effective_thermostat_override(
                _typed_config_entry(self).data,
                _typed_config_entry(self).options,
                thermostat.thermostat_id,
            )
            changed_at = thermostat.filter_changed_at
            reconciled_at = thermostat.filter_change_boundary_reconciled_at
            if current_override is not None:
                changed_at = _parse_datetime(
                    current_override.get(CONF_FILTER_CHANGED_AT)
                )
                reconciled_at = _parse_datetime(
                    current_override.get(CONF_FILTER_CHANGE_BOUNDARY_RECONCILED_AT)
                )
            if reconciled_at is None and _filter_boundary_fast_retry_due(
                changed_at,
                now,
            ):
                return True
        return False

    @callback
    def _async_cancel_filter_boundary_retry(self) -> None:
        if self._cancel_filter_boundary_retry is None:
            return
        self._cancel_filter_boundary_retry()
        self._cancel_filter_boundary_retry = None

    async def _async_retry_filter_boundaries(self, _now: datetime) -> None:
        self._cancel_filter_boundary_retry = None
        try:
            await self.async_refresh_runtime(skip_sync=False, summary_window=True)
        except Exception as err:  # noqa: BLE001 - coordinator retains normal status
            _LOGGER.warning(
                "Beestat filter boundary reconciliation retry remains pending (%s)",
                exception_fingerprint(err),
            )
            self.async_schedule_filter_boundary_reconcile()

    @callback
    def async_rebuild_runtime_from_cached_rows(self) -> None:
        """Rebuild derived state after a local option change without I/O."""

        data = self.data
        if data is None:
            return
        self.async_set_updated_data(
            self._build_runtime_data(
                list(data.summary_rows),
                list(data.thermostat_rows),
                list(data.sensor_rows),
                data.sync_success_at,
                data.metadata_sync_success_at,
                data.summary_rows_full,
                data.summary_window_start,
                data.summary_window_end,
                temporal_context=self.capture_temporal_context(),
                fetched_at=data.fetched_at,
                thermostat_settings=data.thermostat_settings,
            )
        )

    @callback
    def async_set_updated_data(self, data: BeestatRuntimeData) -> None:
        """Publish source data and replace its local projection deadline."""

        super().async_set_updated_data(data)
        self._async_schedule_projection_boundary(data)

    @callback
    def _async_schedule_projection_boundary(
        self,
        data: BeestatRuntimeData | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        """Schedule the earliest I/O-free cached projection boundary."""

        self._async_cancel_projection_boundary()
        if data is None:
            data = self.data
        if data is None:
            return
        deadline = _next_projection_deadline(
            data,
            self._local_tz,
            self.cloud_data_stale_threshold_minutes,
        )
        if now is None:
            now = datetime.now(UTC)
        if deadline <= now:
            self._async_rebuild_projection_from_cached(now)
            return
        self._cancel_projection_boundary = async_track_point_in_utc_time(
            self.hass,
            self._async_handle_projection_boundary,
            deadline,
        )

    @callback
    def _async_cancel_projection_boundary(self) -> None:
        """Cancel the config-entry-owned cached projection callback."""

        if self._cancel_projection_boundary is None:
            return
        cancel = self._cancel_projection_boundary
        self._cancel_projection_boundary = None
        cancel()

    @callback
    def _async_handle_projection_boundary(self, _scheduled_at: datetime) -> None:
        """Rebuild a due projection using the actual callback evaluation time."""

        self._async_rebuild_projection_from_cached(datetime.now(UTC))

    @callback
    def _async_rebuild_projection_from_cached(
        self,
        now: datetime,
        *,
        previous_local_tz: ZoneInfo | None = None,
    ) -> None:
        """Rebuild elapsed projections from cached rows without external I/O."""

        self._cancel_projection_boundary = None
        data = self.data
        if data is None:
            return
        projected = self._build_runtime_data(
            list(data.summary_rows),
            list(data.thermostat_rows),
            list(data.sensor_rows),
            data.sync_success_at,
            data.metadata_sync_success_at,
            data.summary_rows_full,
            data.summary_window_start,
            data.summary_window_end,
            temporal_context=TemporalContext(
                evaluated_at=now,
                local_tz=self._local_tz,
                timezone_revision=self._timezone_revision,
            ),
            fetched_at=data.fetched_at,
            thermostat_settings=data.thermostat_settings,
        )
        if _projection_changed(
            data,
            projected,
            previous_local_tz or self._local_tz,
            self._local_tz,
        ):
            self.data = projected
            self.async_update_listeners()
        self._async_schedule_projection_boundary(projected, now=now)

    async def _async_update_data(self) -> BeestatRuntimeData:
        try:
            return await self._async_fetch_runtime_data(skip_sync=False)
        except BeestatAuthError:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="beestat_auth_failed",
            ) from None
        except Exception:  # noqa: BLE001 - translate at the coordinator boundary
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="beestat_request_failed",
            ) from None

    async def async_refresh_runtime(
        self,
        *,
        skip_sync: bool = False,
        summary_window: bool = False,
    ) -> BeestatRuntimeData:
        """Refresh Beestat runtime summary data and notify coordinator entities."""

        try:
            data = await self._async_fetch_runtime_data(
                skip_sync=skip_sync,
                summary_window=summary_window,
            )
        except Exception as err:
            safe_error: Exception = err
            if not isinstance(err, BeestatApiError):
                safe_error = UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="beestat_request_failed",
                )
            self.async_set_update_error(safe_error)
            if isinstance(err, BeestatAuthError):
                _typed_config_entry(self).async_start_reauth_if_available(self.hass)
            if safe_error is err:
                raise
            raise safe_error from None
        self.async_set_updated_data(data)
        return data

    async def async_dismiss_filter_alerts(self, thermostat_id: int) -> int:
        """Dismiss active Beestat filter alerts for one thermostat."""

        self.last_filter_alert_dismiss_attempt_at = datetime.now(UTC)
        self.last_filter_alert_dismiss_thermostat_id = thermostat_id
        self.last_filter_alert_dismiss_matched = 0
        self.last_filter_alert_dismissed = 0
        self.last_filter_alert_dismiss_error = None
        data = self.data
        if data is None:
            self.async_update_listeners()
            return 0
        row = _thermostat_row(data.thermostat_rows, thermostat_id)
        if row is None:
            self.async_update_listeners()
            return 0

        guids = _filter_alert_guids(row)
        self.last_filter_alert_dismiss_matched = len(guids)
        dismissed = 0
        try:
            for guid in guids:
                await self._client.async_dismiss_alert(thermostat_id, guid)
                dismissed += 1
        except Exception as err:
            self.last_filter_alert_dismissed = dismissed
            self.last_filter_alert_dismiss_error = self._client.redact_error(err)
            self.async_update_listeners()
            raise

        self.last_filter_alert_dismissed = dismissed
        self.async_update_listeners()
        return dismissed

    @callback
    def async_record_import_result(
        self,
        *,
        imported_series: int,
        imported_rows: int,
        source_rows: int,
        skipped_windows: int,
        skipped_runtime_thermostat_windows: int,
        skipped_runtime_sensor_windows: int,
        skipped_window_examples: tuple[dict[str, str], ...],
        summary_mode: str,
        summary_window_start: str | None,
        summary_window_end: str | None,
        summary_overlap_days: int | None,
        summary_fallback_reason: str | None,
        cumulative_seed_count: int,
    ) -> None:
        """Record the latest Recorder import metrics for diagnostic sensors."""

        self.last_error = None
        self.last_error_at = None
        self.last_import_success_at = datetime.now(UTC)
        self.last_imported_series = imported_series
        self.last_imported_rows = imported_rows
        self.last_import_source_rows = source_rows
        self.last_import_partial = skipped_windows > 0
        self.last_import_skipped_windows = skipped_windows
        self.last_import_skipped_runtime_thermostat_windows = (
            skipped_runtime_thermostat_windows
        )
        self.last_import_skipped_runtime_sensor_windows = skipped_runtime_sensor_windows
        self.last_import_skipped_window_examples = skipped_window_examples
        self.last_import_summary_mode = summary_mode
        self.last_import_summary_window_start = summary_window_start
        self.last_import_summary_window_end = summary_window_end
        self.last_import_summary_overlap_days = summary_overlap_days
        self.last_import_summary_fallback_reason = summary_fallback_reason
        self.last_import_cumulative_seed_count = cumulative_seed_count
        self.async_update_listeners()

    @callback
    def async_record_import_error(self, err: Exception) -> None:
        """Record a failed Recorder import for diagnostic sensors."""

        self.last_import_summary_mode = "failed"
        self.last_import_summary_window_start = None
        self.last_import_summary_window_end = None
        self.last_import_summary_overlap_days = None
        self.last_import_summary_fallback_reason = "import_failed"
        self.last_import_cumulative_seed_count = None
        self._async_record_error(err)

    async def _async_fetch_runtime_data(
        self,
        *,
        skip_sync: bool,
        summary_window: bool = False,
    ) -> BeestatRuntimeData:
        try:
            sync_success_at = self.data.sync_success_at if self.data else None
            metadata_sync_success_at = (
                self.data.metadata_sync_success_at if self.data else None
            )
            if not skip_sync:
                await self._client.async_sync_runtime()
                await self._client.async_sync_resource("thermostat")
                await self._client.async_sync_resource("sensor")
                now = datetime.now(UTC)
                sync_success_at = now
                metadata_sync_success_at = now
            thermostat_rows = await self._client.async_read_id("thermostat")
            sensor_rows = await self._client.async_read_id("sensor")
            thermostat_rows_tuple = _effective_resource_rows(
                thermostat_rows,
                "thermostat_id",
                "id",
            )
            sensor_rows_tuple = _effective_resource_rows(
                sensor_rows,
                "sensor_id",
                "id",
            )
            thermostat_settings: dict[int, ThermostatSettingsSnapshot] = {}
            try:
                ecobee_thermostat_rows = await self._client.async_read_id(
                    "ecobee_thermostat"
                )
            except Exception as err:  # noqa: BLE001 - optional metadata fails closed
                _LOGGER.warning(
                    "Beestat Ecobee settings projection remains unavailable (%s)",
                    exception_fingerprint(err),
                )
            else:
                thermostat_settings = build_thermostat_settings_snapshots(
                    thermostat_rows_tuple,
                    ecobee_thermostat_rows,
                )
            config = build_beestat_config(
                self.hass,
                thermostat_rows_tuple,
                sensor_rows_tuple,
                entry_runtime_config_data(_typed_config_entry(self)),
            )
            await self._async_reconcile_pending_filter_boundaries(config)
            if summary_window:
                config = build_beestat_config(
                    self.hass,
                    thermostat_rows_tuple,
                    sensor_rows_tuple,
                    entry_runtime_config_data(_typed_config_entry(self)),
                )
                for _attempt in range(_SUMMARY_TEMPORAL_CONTEXT_ATTEMPTS):
                    query_context = self.capture_temporal_context()
                    query_day = query_context.evaluated_at.astimezone(
                        query_context.local_tz
                    ).date()
                    summary_start = self._summary_window_start(
                        config,
                        thermostat_rows_tuple,
                        query_day,
                    )
                    try:
                        rows = await self._client.async_read_runtime_thermostat_summary(
                            summary_start.isoformat(),
                            query_day.isoformat(),
                        )
                    except BeestatAuthError:
                        raise
                    except BeestatApiError:
                        _LOGGER.warning(
                            "Falling back to full Beestat summary status read "
                            "after windowed read failed"
                        )
                        rows = await self._client.async_read_id(
                            "runtime_thermostat_summary"
                        )
                        temporal_context = self.capture_temporal_context()
                        summary_rows_full = True
                        summary_window_start = None
                        summary_window_end = None
                        break

                    temporal_context = self.capture_temporal_context()
                    current_day = temporal_context.evaluated_at.astimezone(
                        temporal_context.local_tz
                    ).date()
                    if current_day == query_day:
                        summary_rows_full = False
                        summary_window_start = summary_start
                        summary_window_end = query_day
                        break
                else:
                    _LOGGER.info(
                        "Falling back to full Beestat summary status read after "
                        "the local date changed repeatedly during refresh"
                    )
                    rows = await self._client.async_read_id(
                        "runtime_thermostat_summary"
                    )
                    temporal_context = self.capture_temporal_context()
                    summary_rows_full = True
                    summary_window_start = None
                    summary_window_end = None
            else:
                rows = await self._client.async_read_id("runtime_thermostat_summary")
                temporal_context = self.capture_temporal_context()
                summary_rows_full = True
                summary_window_start = None
                summary_window_end = None
            data = self._build_runtime_data(
                rows,
                list(thermostat_rows_tuple),
                list(sensor_rows_tuple),
                sync_success_at,
                metadata_sync_success_at,
                summary_rows_full,
                summary_window_start,
                summary_window_end,
                temporal_context=temporal_context,
                thermostat_settings=thermostat_settings,
            )
        except Exception as err:
            self._async_record_error(err)
            raise

        self.last_error = None
        self.last_error_at = None
        return data

    @callback
    def _async_record_error(self, err: Exception) -> None:
        """Record an error message safe for Home Assistant state."""

        self.last_error = self._client.redact_error(err)
        self.last_error_at = datetime.now(UTC)
        self.async_update_listeners()

    async def _async_reconcile_pending_filter_boundaries(
        self,
        config: BeestatConfig,
    ) -> None:
        """Finalize persisted click timestamps from bounded raw runtime rows."""

        pending = [
            thermostat
            for thermostat in config.thermostats
            if thermostat.filter_changed_at is not None
            and thermostat.filter_change_boundary_reconciled_at is None
        ]
        self.last_filter_boundary_pending_count = len(pending)
        self.last_filter_boundary_reconciled_count = 0
        self.last_filter_boundary_reconcile_error = None
        if not pending:
            self._async_cancel_filter_boundary_retry()
            return

        temporal_context = self.capture_temporal_context()
        self.last_filter_boundary_reconcile_attempt_at = temporal_context.evaluated_at
        pending_count = 0
        for thermostat in pending:
            if not self.temporal_context_is_current(temporal_context):
                pending_count += 1
                continue
            changed_at = thermostat.filter_changed_at
            if changed_at is None:  # pragma: no cover - narrowed above
                continue
            local_date = changed_at.astimezone(temporal_context.local_tz).date()
            window_start = (
                datetime.combine(local_date, time.min)
                .replace(tzinfo=temporal_context.local_tz)
                .astimezone(UTC)
            )
            window_end = min(
                temporal_context.evaluated_at,
                changed_at + timedelta(minutes=5),
            )
            try:
                rows = await self._client.async_read_runtime_thermostat(
                    thermostat.thermostat_id,
                    window_start.isoformat(),
                    window_end.isoformat(),
                )
            except Exception as err:  # noqa: BLE001 - primary runtime remains usable
                pending_count += 1
                self.last_filter_boundary_reconcile_error = self._client.redact_error(
                    err
                )
                _LOGGER.warning(
                    "Unable to reconcile a pending Beestat filter boundary (%s)",
                    exception_fingerprint(err),
                )
                continue

            if not self.temporal_context_is_current(temporal_context):
                pending_count += 1
                continue

            boundary = _raw_filter_boundary(rows, changed_at)
            if boundary is None:
                pending_count += 1
                continue
            current_override = effective_thermostat_override(
                _typed_config_entry(self).data,
                _typed_config_entry(self).options,
                thermostat.thermostat_id,
            )
            current_changed_at = _parse_datetime(
                current_override.get(CONF_FILTER_CHANGED_AT)
                if current_override is not None
                else None
            )
            current_reconciled_at = _parse_datetime(
                current_override.get(CONF_FILTER_CHANGE_BOUNDARY_RECONCILED_AT)
                if current_override is not None
                else None
            )
            if current_changed_at != changed_at or current_reconciled_at is not None:
                if current_changed_at is not None and current_reconciled_at is None:
                    pending_count += 1
                continue
            reconciled_at = datetime.now(UTC)
            options = update_thermostat_override_options(
                _typed_config_entry(self).data,
                _typed_config_entry(self).options,
                thermostat.thermostat_id,
                {
                    CONF_FILTER_CHANGE_DAY_RUNTIME_BASELINE_SECONDS: (
                        boundary.baseline_seconds
                    ),
                    CONF_FILTER_CHANGE_BOUNDARY_RECONCILED_AT: (
                        reconciled_at.isoformat()
                    ),
                    CONF_FILTER_CHANGE_BOUNDARY_SOURCE_DATA_END: (
                        boundary.source_data_end.isoformat()
                    ),
                },
            )
            self.hass.config_entries.async_update_entry(
                _typed_config_entry(self),
                options=options,
            )
            self.last_filter_boundary_reconciled_count += 1

        self.last_filter_boundary_pending_count = pending_count
        if pending_count:
            self.async_schedule_filter_boundary_reconcile(config)
        else:
            self._async_cancel_filter_boundary_retry()

    def _build_runtime_data(
        self,
        rows: list[dict[str, Any]],
        thermostat_rows: list[dict[str, Any]],
        sensor_rows: list[dict[str, Any]],
        sync_success_at: datetime | None,
        metadata_sync_success_at: datetime | None,
        summary_rows_full: bool,
        summary_window_start: date | None,
        summary_window_end: date | None,
        *,
        temporal_context: TemporalContext | None = None,
        evaluated_at: datetime | None = None,
        fetched_at: datetime | None = None,
        thermostat_settings: dict[int, ThermostatSettingsSnapshot] | None = None,
    ) -> BeestatRuntimeData:
        if temporal_context is None:
            temporal_context = TemporalContext(
                evaluated_at=evaluated_at or datetime.now(UTC),
                local_tz=self._local_tz,
                timezone_revision=self._timezone_revision,
            )
        projected_at = temporal_context.evaluated_at
        local_tz = temporal_context.local_tz
        source_fetched_at = fetched_at or projected_at
        today = projected_at.astimezone(local_tz).date()
        rows_tuple = _effective_summary_rows(rows)
        thermostat_rows_tuple = _effective_resource_rows(
            thermostat_rows,
            "thermostat_id",
            "id",
        )
        sensor_rows_tuple = _effective_resource_rows(
            sensor_rows,
            "sensor_id",
            "id",
        )
        config = build_beestat_config(
            self.hass,
            thermostat_rows_tuple,
            sensor_rows_tuple,
            entry_runtime_config_data(_typed_config_entry(self)),
        )
        summaries: dict[int, ThermostatRuntimeSummary] = {}
        sensor_metadata = _build_sensor_metadata(sensor_rows_tuple)
        thermostat_row_by_id = {
            thermostat_id: row
            for row in thermostat_rows_tuple
            if (thermostat_id := _row_int(row, "thermostat_id", "id")) is not None
        }

        for thermostat in config.thermostats:
            thermostat_rows = [
                row
                for row in rows_tuple
                if str(row.get("thermostat_id")) == str(thermostat.thermostat_id)
            ]
            latest_date = _latest_row_date(thermostat_rows)
            lag_days = (today - latest_date).days if latest_date is not None else None
            changed_date, changed_source = self._filter_changed_date(
                thermostat,
                thermostat_row_by_id.get(thermostat.thermostat_id, {}),
            )
            summaries[thermostat.thermostat_id] = ThermostatRuntimeSummary(
                thermostat_id=thermostat.thermostat_id,
                slug=thermostat.slug,
                label=thermostat.name,
                latest_date=latest_date,
                lag_days=max(lag_days, 0) if lag_days is not None else None,
                filter_changed_date=changed_date,
                filter_changed_source=changed_source,
                filter_runtime_hours=_filter_runtime_hours(
                    thermostat_rows,
                    changed_date,
                    thermostat,
                    changed_source,
                ),
                recent_runtime_hours_per_day=_recent_runtime_hours_per_day(
                    thermostat_rows,
                    today,
                ),
            )

        thermostat_metadata = _build_thermostat_metadata(
            thermostat_rows_tuple,
            sensor_metadata,
            projected_at,
            local_tz,
            config.thermostats,
        )
        return BeestatRuntimeData(
            config=config,
            fetched_at=source_fetched_at,
            projected_at=projected_at,
            sync_success_at=sync_success_at,
            metadata_sync_success_at=metadata_sync_success_at,
            summary_rows=rows_tuple,
            summary_rows_full=summary_rows_full,
            summary_window_start=summary_window_start,
            summary_window_end=summary_window_end,
            thermostat_rows=thermostat_rows_tuple,
            sensor_rows=sensor_rows_tuple,
            summary_row_count=len(rows_tuple),
            thermostats=summaries,
            thermostat_metadata=thermostat_metadata,
            sensor_metadata=sensor_metadata,
            thermostat_settings=thermostat_settings or {},
            room_temperature_spreads=_build_room_temperature_spreads(
                self.hass,
                config,
                thermostat_metadata,
                sensor_metadata,
            ),
        )

    def _summary_window_start(
        self,
        config: BeestatConfig,
        thermostat_rows: tuple[dict[str, Any], ...],
        today: date,
    ) -> date:
        """Return the earliest summary day needed for native status sensors."""

        start = today - timedelta(days=FILTER_RECENT_RUNTIME_DAYS)
        thermostat_row_by_id = {
            thermostat_id: row
            for row in thermostat_rows
            if (thermostat_id := _row_int(row, "thermostat_id", "id")) is not None
        }
        for thermostat in config.thermostats:
            changed_date, _ = self._filter_changed_date(
                thermostat,
                thermostat_row_by_id.get(thermostat.thermostat_id, {}),
            )
            if changed_date is not None:
                start = min(start, changed_date)
        return start

    def _filter_changed_date(
        self,
        thermostat: ConfiguredThermostat,
        thermostat_row: dict[str, Any],
    ) -> tuple[date | None, str | None]:
        if thermostat.filter_changed_date is not None:
            return thermostat.filter_changed_date, "home_assistant"
        if thermostat.filter_changed_entity_id is not None:
            state = self.hass.states.get(thermostat.filter_changed_entity_id)
            if state is not None and (parsed := _parse_date(state.state)) is not None:
                return parsed, "helper"
        if changed_date := _beestat_filter_changed_date(thermostat_row):
            return changed_date, "beestat"
        return None, None


def _projection_changed(
    current: BeestatRuntimeData,
    projected: BeestatRuntimeData,
    current_local_tz: ZoneInfo,
    projected_local_tz: ZoneInfo | None = None,
) -> bool:
    """Return whether a cached time projection changes entity-visible state."""

    if projected_local_tz is None:
        projected_local_tz = current_local_tz
    if (
        current.config != projected.config
        or current.thermostats != projected.thermostats
        or current.thermostat_metadata != projected.thermostat_metadata
    ):
        return True

    current_day = current.projected_at.astimezone(current_local_tz).date()
    projected_day = projected.projected_at.astimezone(projected_local_tz).date()
    if current_day == projected_day:
        return False

    return any(
        build_filter_forecast(
            thermostat,
            current.thermostats.get(thermostat.thermostat_id),
            today=current_day,
        )
        != build_filter_forecast(
            thermostat,
            projected.thermostats.get(thermostat.thermostat_id),
            today=projected_day,
        )
        for thermostat in current.config.thermostats
    )


def _next_projection_deadline(
    data: BeestatRuntimeData,
    local_tz: ZoneInfo,
    stale_threshold_minutes: int = CLOUD_DATA_STALE_MINIMUM_MINUTES,
) -> datetime:
    """Return the earliest cached schedule, freshness, or local-date boundary."""

    projection_at = data.projected_at
    deadlines = [_next_local_midnight(projection_at, local_tz)]
    for metadata in data.thermostat_metadata.values():
        if (
            metadata.next_scheduled_at is not None
            and metadata.next_scheduled_at > projection_at
        ):
            deadlines.append(metadata.next_scheduled_at)
        if metadata.data_end is None:
            continue
        stale_at = _cloud_data_stale_deadline(
            metadata.data_end,
            stale_threshold_minutes,
        )
        if stale_at > projection_at:
            deadlines.append(stale_at)
    return min(deadlines)


def _next_local_midnight(now: datetime, local_tz: ZoneInfo) -> datetime:
    """Return the next local calendar boundary as an absolute UTC instant."""

    local_day = now.astimezone(local_tz).date() + timedelta(days=1)
    return datetime.combine(local_day, time.min, tzinfo=local_tz).astimezone(UTC)


def _latest_row_date(rows: list[dict[str, Any]]) -> date | None:
    dates = [_parse_date(row.get("date")) for row in rows]
    valid_dates = [item for item in dates if item is not None]
    return max(valid_dates) if valid_dates else None


def _runtime_hours_since(
    rows: list[dict[str, Any]],
    changed_date: date | None,
    *,
    change_day_baseline_seconds: float | None = None,
) -> float | None:
    if changed_date is None:
        return None
    matched_rows = [
        row
        for row in rows
        if (row_date := _parse_date(row.get("date"))) is not None
        and row_date >= changed_date
    ]
    if not matched_rows:
        return 0.0
    if change_day_baseline_seconds is None:
        total_seconds = _sum_fan_seconds(matched_rows)
        return round(total_seconds / 3600, 1) if total_seconds is not None else None
    changed_day_rows = [
        row for row in matched_rows if _parse_date(row.get("date")) == changed_date
    ]
    later_rows = [
        row
        for row in matched_rows
        if (row_date := _parse_date(row.get("date"))) is not None
        and row_date > changed_date
    ]
    changed_day_total = _sum_fan_seconds(changed_day_rows)
    later_total = _sum_fan_seconds(later_rows)
    if changed_day_total is None or later_total is None:
        return None
    changed_day_seconds = max(changed_day_total - change_day_baseline_seconds, 0.0)
    total_seconds = _finite_sum((changed_day_seconds, later_total))
    if total_seconds is None:
        return None
    return round(total_seconds / 3600, 1)


def _runtime_seconds_on_date(
    rows: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    thermostat_id: int,
    target_date: date,
) -> float | None:
    matched_rows = [
        row
        for row in rows
        if _row_int(row, "thermostat_id", "id") == thermostat_id
        and _parse_date(row.get("date")) == target_date
    ]
    if not matched_rows:
        return None
    return _sum_fan_seconds(matched_rows)


def _filter_runtime_hours(
    rows: list[dict[str, Any]],
    changed_date: date | None,
    thermostat: ConfiguredThermostat,
    changed_source: str | None,
) -> float | None:
    """Return runtime without charging pre-click runtime to a pending reset."""

    if (
        changed_source == "home_assistant"
        and thermostat.filter_changed_at is not None
        and thermostat.filter_change_boundary_reconciled_at is None
    ):
        return _runtime_hours_since(
            rows,
            changed_date + timedelta(days=1) if changed_date is not None else None,
        )
    return _runtime_hours_since(
        rows,
        changed_date,
        change_day_baseline_seconds=(
            thermostat.filter_change_day_runtime_baseline_seconds
            if changed_source == "home_assistant"
            else None
        ),
    )


def _raw_filter_boundary(
    rows: list[dict[str, Any]],
    changed_at: datetime,
) -> RawFilterBoundary | None:
    """Return a five-minute boundary once raw data covers the click bucket."""

    if changed_at.tzinfo is None:
        raise ValueError("changed_at must be timezone-aware")
    changed_at = changed_at.astimezone(UTC)
    parsed_rows = [
        (timestamp, row)
        for row in rows
        if (timestamp := _parse_datetime(row.get("timestamp"))) is not None
    ]
    if not parsed_rows:
        return None
    source_data_end = max(timestamp for timestamp, _row in parsed_rows)
    click_bucket = _floor_five_minutes(changed_at)
    if source_data_end < click_bucket:
        return None
    effective_at = _nearest_five_minutes(changed_at)
    baseline_seconds = _finite_sum(
        _float_or_zero(row.get("fan"))
        for timestamp, row in parsed_rows
        if timestamp < effective_at
    )
    if baseline_seconds is None:
        return None
    return RawFilterBoundary(
        baseline_seconds=baseline_seconds,
        effective_at=effective_at,
        source_data_end=source_data_end,
    )


def _filter_boundary_fast_retry_due(
    changed_at: datetime | None,
    now: datetime,
) -> bool:
    """Return whether a pending click remains in the fast retry window."""

    if changed_at is None or changed_at.tzinfo is None or now.tzinfo is None:
        return False
    age = now.astimezone(UTC) - changed_at.astimezone(UTC)
    return timedelta(0) <= age <= _FILTER_BOUNDARY_FAST_RETRY_WINDOW


def _floor_five_minutes(value: datetime) -> datetime:
    epoch = int(value.astimezone(UTC).timestamp())
    return datetime.fromtimestamp((epoch // 300) * 300, tz=UTC)


def _nearest_five_minutes(value: datetime) -> datetime:
    epoch = int(value.astimezone(UTC).timestamp())
    return datetime.fromtimestamp(((epoch + 150) // 300) * 300, tz=UTC)


def _recent_runtime_hours_per_day(
    rows: list[dict[str, Any]],
    today: date,
) -> float | None:
    cutoff = today - timedelta(days=FILTER_RECENT_RUNTIME_DAYS)
    matched_rows = [
        row
        for row in rows
        if (row_date := _parse_date(row.get("date"))) is not None and row_date >= cutoff
    ]
    if not matched_rows:
        return None
    total_seconds = _sum_fan_seconds(matched_rows)
    if total_seconds is None:
        return None
    return round((total_seconds / 3600) / len(matched_rows), 2)


def _sum_fan_seconds(rows: list[dict[str, Any]]) -> float | None:
    return _finite_sum(_float_or_zero(row.get("sum_fan")) for row in rows)


def _finite_sum(values: Iterable[float]) -> float | None:
    try:
        total = fsum(values)
    except OverflowError:
        return None
    return total if isfinite(total) else None


def _thermostat_row(
    rows: tuple[dict[str, Any], ...],
    thermostat_id: int,
) -> dict[str, Any] | None:
    for row in rows:
        if _row_int(row, "thermostat_id", "id") == thermostat_id:
            return row
    return None


def _build_sensor_metadata(
    rows: tuple[dict[str, Any], ...],
) -> dict[int, SensorMetadata]:
    metadata: dict[int, SensorMetadata] = {}
    for row in rows:
        sensor_id = _row_int(row, "sensor_id", "id")
        if sensor_id is None:
            continue
        metadata[sensor_id] = SensorMetadata(
            sensor_id=sensor_id,
            thermostat_id=_row_int(row, "thermostat_id"),
            name=_string_or_none(row.get("name")),
            identifier=_string_or_none(row.get("identifier")),
            sensor_type=_string_or_none(row.get("type")),
            in_use=_bool(row.get("in_use")),
            inactive=_bool(row.get("inactive")),
            deleted=_bool(row.get("deleted")),
        )
    return metadata


def _build_thermostat_metadata(
    thermostat_rows: tuple[dict[str, Any], ...],
    sensor_metadata: dict[int, SensorMetadata],
    fetched_at: datetime,
    local_tz: ZoneInfo,
    thermostats: tuple[ConfiguredThermostat, ...],
) -> dict[int, ThermostatMetadata]:
    metadata: dict[int, ThermostatMetadata] = {}
    for thermostat in thermostats:
        row = next(
            (
                item
                for item in thermostat_rows
                if str(item.get("thermostat_id") or item.get("id"))
                == str(thermostat.thermostat_id)
            ),
            {},
        )
        data_begin = _parse_datetime(row.get("data_begin"))
        data_end = _parse_datetime(row.get("data_end"))
        active_sensors = tuple(
            sorted(
                item.name or str(item.sensor_id)
                for item in sensor_metadata.values()
                if item.thermostat_id == thermostat.thermostat_id
                and item.in_use
                and not item.inactive
                and not item.deleted
            )
        )
        current_ref, current_name, current_profile_sensors = _current_profile(row)
        schedule = _schedule_snapshot(row, fetched_at, local_tz)
        active_alerts = _active_alerts(row)
        metadata[thermostat.thermostat_id] = ThermostatMetadata(
            thermostat_id=thermostat.thermostat_id,
            slug=thermostat.slug,
            label=thermostat.name,
            data_begin=data_begin,
            data_end=data_end,
            data_lag_minutes=_lag_minutes(fetched_at, data_end),
            current_climate_ref=current_ref,
            current_climate_name=current_name,
            scheduled_climate_ref=schedule["scheduled_ref"],
            scheduled_climate_name=schedule["scheduled_name"],
            next_scheduled_climate_ref=schedule["next_ref"],
            next_scheduled_climate_name=schedule["next_name"],
            next_scheduled_at=schedule["next_at"],
            schedule_profiles=schedule["profiles"],
            active_sensor_count=len(active_sensors),
            active_sensor_names=active_sensors,
            current_profile_sensor_names=tuple(
                sensor.name or "Unnamed sensor" for sensor in current_profile_sensors
            ),
            active_alert_count=len(active_alerts),
            active_alerts=active_alerts,
            current_profile_sensors=current_profile_sensors,
        )
    return metadata


def _build_room_temperature_spreads(
    hass: HomeAssistant,
    config: BeestatConfig,
    thermostat_metadata: dict[int, ThermostatMetadata],
    sensor_metadata: dict[int, SensorMetadata],
) -> dict[int, RoomTemperatureSpread]:
    """Build profile-aware spreads from identity-proven mapped local sources."""

    states = getattr(hass, "states", None)
    if states is None or not callable(getattr(states, "get", None)):
        return {}
    target_unit = _configured_temperature_unit(hass)
    metadata_by_identifier: dict[str, list[SensorMetadata]] = {}
    for beestat_sensor in sensor_metadata.values():
        if beestat_sensor.identifier is None:
            continue
        metadata_by_identifier.setdefault(beestat_sensor.identifier.strip(), []).append(
            beestat_sensor
        )
    configured_by_id: dict[int, list[ConfiguredSensor]] = {}
    for sensor in config.sensors:
        configured_by_id.setdefault(sensor.sensor_id, []).append(sensor)

    projections: dict[int, RoomTemperatureSpread] = {}
    for thermostat in config.thermostats:
        thermostat_details = thermostat_metadata.get(thermostat.thermostat_id)
        if thermostat_details is None or not thermostat_details.current_profile_sensors:
            continue
        participants = _unique_profile_sensors(
            thermostat_details.current_profile_sensors
        )
        valid: list[tuple[str, float]] = []
        unavailable: list[str] = []
        participating_names: list[str] = []
        resolved_unit = target_unit
        for participant in participants:
            participant_metadata = _profile_sensor_metadata(
                participant.identifier,
                metadata_by_identifier,
                thermostat.thermostat_id,
            )
            configured = (
                configured_by_id.get(participant_metadata.sensor_id, [])
                if participant_metadata is not None
                else []
            )
            source = configured[0] if len(configured) == 1 else None
            display_name = (
                source.name
                if source is not None
                else participant.name or "Unnamed sensor"
            )
            participating_names.append(display_name)
            if (
                participant_metadata is None
                or participant_metadata.thermostat_id != thermostat.thermostat_id
                or participant_metadata.inactive
                or participant_metadata.deleted
                or source is None
                or source.thermostat_id != thermostat.thermostat_id
                or source.temperature_entity_id is None
            ):
                unavailable.append(display_name)
                continue
            state = states.get(source.temperature_entity_id)
            reading = _temperature_state_value(state, resolved_unit)
            if reading is None:
                unavailable.append(display_name)
                continue
            value, source_unit = reading
            if resolved_unit is None:
                resolved_unit = source_unit
            valid.append((source.name, value))

        hottest = max(valid, key=lambda item: item[1]) if valid else None
        coldest = min(valid, key=lambda item: item[1]) if valid else None
        spread = (
            round(hottest[1] - coldest[1], 2)
            if hottest is not None and coldest is not None and len(valid) >= 2
            else None
        )
        projections[thermostat.thermostat_id] = RoomTemperatureSpread(
            value=spread,
            unit=resolved_unit,
            participating_sensor_count=len(participating_names),
            valid_sensor_count=len(valid),
            participating_sensor_names=tuple(participating_names),
            unavailable_sensor_names=tuple(unavailable),
            hottest_sensor_name=hottest[0] if hottest is not None else None,
            coldest_sensor_name=coldest[0] if coldest is not None else None,
        )
    return projections


def _configured_temperature_unit(hass: HomeAssistant) -> str | None:
    config = getattr(hass, "config", None)
    units = getattr(config, "units", None)
    return _canonical_temperature_unit(getattr(units, "temperature_unit", None))


def _temperature_state_value(
    state: Any,
    target_unit: str | None,
) -> tuple[float, str] | None:
    if state is None or str(getattr(state, "state", "")).lower() in {
        "unknown",
        "unavailable",
        "none",
        "",
    }:
        return None
    value = _finite_float(getattr(state, "state", None))
    attributes = getattr(state, "attributes", None)
    source_unit = _canonical_temperature_unit(
        attributes.get("unit_of_measurement") if isinstance(attributes, dict) else None
    )
    if value is None or source_unit is None:
        return None
    destination = target_unit or source_unit
    converted = _convert_temperature(value, source_unit, destination)
    return (converted, destination) if converted is not None else None


def _convert_temperature(value: float, source: str, target: str) -> float | None:
    if source == target:
        return value
    if source == "°F" and target == "°C":
        return (value - 32) * 5 / 9
    if source == "°C" and target == "°F":
        return value * 9 / 5 + 32
    if source == "°C" and target == "K":
        return value + 273.15
    if source == "K" and target == "°C":
        return value - 273.15
    if source == "°F" and target == "K":
        return (value - 32) * 5 / 9 + 273.15
    if source == "K" and target == "°F":
        return (value - 273.15) * 9 / 5 + 32
    return None


def _canonical_temperature_unit(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper().replace(" ", "")
    if normalized in {"°F", "F", "DEGREESF", "FAHRENHEIT"}:
        return "°F"
    if normalized in {"°C", "C", "DEGREESC", "CELSIUS"}:
        return "°C"
    if normalized in {"K", "°K", "KELVIN"}:
        return "K"
    return None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except OverflowError, TypeError, ValueError:
        return None
    return parsed if isfinite(parsed) else None


def _unique_profile_sensors(
    values: tuple[ProfileSensorReference, ...],
) -> tuple[ProfileSensorReference, ...]:
    """Deduplicate identity-proven participants without collapsing equal names."""

    unique: list[ProfileSensorReference] = []
    seen_identifiers: set[str] = set()
    for value in values:
        identifier = value.identifier.strip() if value.identifier is not None else None
        if identifier is not None and identifier in seen_identifiers:
            continue
        if identifier is not None:
            seen_identifiers.add(identifier)
        unique.append(value)
    return tuple(unique)


def _profile_sensor_metadata(
    identifier: str | None,
    metadata_by_identifier: dict[str, list[SensorMetadata]],
    thermostat_id: int,
) -> SensorMetadata | None:
    """Resolve one climate capability identifier within its thermostat owner."""

    if identifier is None or not (normalized := identifier.strip()):
        return None
    exact = [
        item
        for item in metadata_by_identifier.get(normalized, [])
        if item.thermostat_id == thermostat_id
    ]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    base, separator, capability = normalized.rpartition(":")
    if not separator or not base or not capability or ":" not in base:
        return None
    candidates = [
        item
        for item in metadata_by_identifier.get(base, [])
        if item.thermostat_id == thermostat_id
    ]
    return candidates[0] if len(candidates) == 1 else None


def _beestat_filter_changed_date(row: dict[str, Any]) -> date | None:
    filters = row.get("filters")
    candidates = _find_changed_dates(filters)
    return max(candidates) if candidates else None


def _find_changed_dates(value: Any) -> list[date]:
    dates: list[date] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if "changed" in str(key).lower() and (parsed := _parse_date(item)):
                dates.append(parsed)
            elif isinstance(item, (dict, list)):
                dates.extend(_find_changed_dates(item))
    elif isinstance(value, list):
        for item in value:
            dates.extend(_find_changed_dates(item))
    return dates


def _current_profile(
    row: dict[str, Any],
) -> tuple[str | None, str | None, tuple[ProfileSensorReference, ...]]:
    program = row.get("program")
    if not isinstance(program, dict):
        return None, None, ()
    current_ref = _string_or_none(program.get("currentClimateRef"))
    climates = program.get("climates")
    if current_ref is None or not isinstance(climates, list):
        return current_ref, current_ref, ()
    for climate in climates:
        if not isinstance(climate, dict) or climate.get("climateRef") != current_ref:
            continue
        sensors = climate.get("sensors")
        sensor_references = (
            tuple(
                ProfileSensorReference(
                    identifier=_string_or_none(item.get("id")),
                    name=_string_or_none(item.get("name")),
                )
                for item in sensors
                if isinstance(item, dict)
                and (
                    _string_or_none(item.get("id")) is not None
                    or _string_or_none(item.get("name")) is not None
                )
            )
            if isinstance(sensors, list)
            else ()
        )
        return (
            current_ref,
            _string_or_none(climate.get("name")) or current_ref,
            sensor_references,
        )
    return current_ref, current_ref, ()


def _schedule_snapshot(
    row: dict[str, Any],
    fetched_at: datetime,
    local_tz: ZoneInfo,
) -> dict[str, Any]:
    program = row.get("program")
    if not isinstance(program, dict):
        return _empty_schedule_snapshot()

    profile_by_ref = _schedule_profiles_by_ref(program)
    profiles = tuple(profile_by_ref.values())
    schedule = program.get("schedule")
    if not _valid_schedule(schedule):
        return {**_empty_schedule_snapshot(), "profiles": profiles}

    tz = _row_timezone(row, local_tz)
    local_now = fetched_at.astimezone(tz)
    day_index = _ecobee_day_index(local_now)
    slot_index = min(local_now.hour * 2 + (local_now.minute // 30), 47)
    scheduled_ref = _schedule_ref(schedule, day_index, slot_index)
    scheduled_profile = profile_by_ref.get(scheduled_ref or "")
    next_ref, next_at = _next_schedule_transition(
        schedule,
        local_now,
        scheduled_ref,
    )
    next_profile = profile_by_ref.get(next_ref or "")
    return {
        "scheduled_ref": scheduled_ref,
        "scheduled_name": _profile_name(scheduled_profile, scheduled_ref),
        "next_ref": next_ref,
        "next_name": _profile_name(next_profile, next_ref),
        "next_at": next_at.astimezone(UTC) if next_at else None,
        "profiles": profiles,
    }


def _empty_schedule_snapshot() -> dict[str, Any]:
    return {
        "scheduled_ref": None,
        "scheduled_name": None,
        "next_ref": None,
        "next_name": None,
        "next_at": None,
        "profiles": (),
    }


def _schedule_profiles_by_ref(program: dict[str, Any]) -> dict[str, ScheduleProfile]:
    return schedule_profiles_by_ref(program)


def _valid_schedule(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 7:
        return False
    return all(isinstance(day, list) and len(day) >= 48 for day in value)


def _row_timezone(row: dict[str, Any], fallback: ZoneInfo) -> ZoneInfo:
    for field in ("timezone", "time_zone", "timeZone"):
        value = _string_or_none(row.get(field))
        if value is None:
            continue
        try:
            return ZoneInfo(value)
        except ZoneInfoNotFoundError:
            continue
    return fallback


def _ecobee_day_index(value: datetime) -> int:
    """Return Ecobee's Monday-first schedule index for a local datetime."""

    return value.weekday()


def _schedule_ref(schedule: Any, day_index: int, slot_index: int) -> str | None:
    if not _valid_schedule(schedule):
        return None
    value = schedule[day_index][slot_index]
    return _string_or_none(value)


def _next_schedule_transition(
    schedule: Any,
    local_now: datetime,
    current_ref: str | None,
) -> tuple[str | None, datetime | None]:
    candidate_utc = local_now.astimezone(UTC).replace(second=0, microsecond=0)
    candidate_utc += timedelta(minutes=1)
    for _offset in range(8 * 24 * 60):
        candidate_local = candidate_utc.astimezone(local_now.tzinfo)
        if candidate_local.minute in (0, 30):
            candidate_ref = _schedule_ref(
                schedule,
                _ecobee_day_index(candidate_local),
                candidate_local.hour * 2 + (candidate_local.minute // 30),
            )
            if candidate_ref is not None and candidate_ref != current_ref:
                return candidate_ref, candidate_local
        candidate_utc += timedelta(minutes=1)
    return None, None


def _profile_name(profile: ScheduleProfile | None, ref: str | None) -> str | None:
    if profile is not None:
        return profile.name
    return ref


def _filter_alert_guids(row: dict[str, Any]) -> tuple[str, ...]:
    alerts = row.get("alerts")
    if not isinstance(alerts, list):
        return ()

    guids: list[str] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        if _bool(alert.get("dismissed")):
            continue
        if str(alert.get("acknowledgement", "")).lower() == "acknowledged":
            continue
        if not _is_filter_alert(alert):
            continue
        guid = _string_or_none(alert.get("guid"))
        if guid is not None:
            guids.append(guid)
    return tuple(dict.fromkeys(guids))


def _is_filter_alert(alert: dict[str, Any]) -> bool:
    code = str(alert.get("code") or alert.get("alertNumber") or "").lower()
    if code in {"3137", "3138", "filter"}:
        return True
    text = " ".join(
        str(alert.get(field) or "").lower()
        for field in ("notificationType", "type", "text")
    )
    return "filter" in text


def _active_alerts(row: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    alerts = row.get("alerts")
    if not isinstance(alerts, list):
        return ()
    active: list[dict[str, Any]] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        if _bool(alert.get("dismissed")):
            continue
        if str(alert.get("acknowledgement", "")).lower() == "acknowledged":
            continue
        active.append(
            {
                "code": alert.get("code") or alert.get("alertNumber"),
                "type": alert.get("notificationType") or alert.get("source"),
                "severity": alert.get("severity"),
                "timestamp": alert.get("timestamp")
                or _join_date_time(alert.get("date"), alert.get("time")),
                "text": alert.get("text"),
            }
        )
    return tuple(active)


def _join_date_time(date_value: Any, time_value: Any) -> str | None:
    if not isinstance(date_value, str):
        return None
    if not isinstance(time_value, str):
        return date_value
    return f"{date_value} {time_value}"


def _lag_minutes(now: datetime, then: datetime | None) -> int | None:
    if then is None:
        return None
    return max(round((now - then).total_seconds() / 60), 0)


def cloud_data_stale_threshold_minutes(scan_interval_seconds: int) -> int:
    """Return a lag threshold that permits one normal poll plus source grace."""

    scan_minutes = max((scan_interval_seconds + 59) // 60, 0)
    return max(
        CLOUD_DATA_STALE_MINIMUM_MINUTES,
        scan_minutes + CLOUD_DATA_STALE_GRACE_MINUTES,
    )


def _cloud_data_stale_deadline(
    data_end: datetime,
    threshold_minutes: int,
) -> datetime:
    """Return when rounded cloud lag first exceeds the shared threshold."""

    return data_end + timedelta(
        minutes=threshold_minutes,
        seconds=30,
        microseconds=1,
    )


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _row_int(row: dict[str, Any], *fields: str) -> int | None:
    for field in fields:
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except OverflowError, TypeError, ValueError:
            continue
    return None


def _effective_resource_rows(
    rows: list[dict[str, Any]],
    *id_fields: str,
) -> tuple[dict[str, Any], ...]:
    """Return one usable resource row per ID, with the last source row effective."""

    effective: dict[int, dict[str, Any]] = {}
    for row in rows:
        row_id = _row_int(row, *id_fields)
        if row_id is not None:
            effective[row_id] = row
    return tuple(row for row in effective.values() if not row.get("deleted"))


def _effective_summary_rows(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return one usable daily summary per identity, with the last row effective."""

    effective: dict[tuple[int, date], dict[str, Any]] = {}
    for row in rows:
        thermostat_id = _row_int(row, "thermostat_id")
        local_day = _parse_date(row.get("date"))
        if thermostat_id is not None and local_day is not None:
            effective[(thermostat_id, local_day)] = row
    return tuple(row for row in effective.values() if not row.get("deleted"))


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "on"}
    return bool(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return _bool(value)


def _float_or_zero(value: Any) -> float:
    try:
        parsed = float(value)
    except OverflowError, TypeError, ValueError:
        return 0.0
    return parsed if isfinite(parsed) else 0.0

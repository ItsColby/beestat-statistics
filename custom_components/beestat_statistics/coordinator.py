"""Runtime-summary coordinator for Beestat native Home Assistant entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BeestatApiError, BeestatAuthError, BeestatClient
from .config_model import (
    BeestatConfig,
    ConfiguredThermostat,
    build_beestat_config,
)
from .config_payload import entry_runtime_config_data
from .const import (
    DOMAIN,
    FILTER_RECENT_RUNTIME_DAYS,
)

_LOGGER = logging.getLogger(__name__)

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
    schedule_profiles: tuple["ScheduleProfile", ...]
    active_sensor_count: int
    active_sensor_names: tuple[str, ...]
    current_profile_sensor_names: tuple[str, ...]
    active_alert_count: int
    active_alerts: tuple[dict[str, Any], ...]


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
class ScheduleProfile:
    """One Ecobee comfort profile from the thermostat program."""

    ref: str
    name: str
    is_occupied: bool | None
    sensors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BeestatRuntimeData:
    """Latest Beestat runtime summary readback."""

    config: BeestatConfig
    fetched_at: datetime
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


class BeestatRuntimeDataCoordinator(DataUpdateCoordinator[BeestatRuntimeData]):
    """Coordinate Beestat runtime sync/read calls for sensors and imports."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: BeestatStatisticsConfigEntry,
        client: BeestatClient,
        *,
        local_tz: ZoneInfo,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_runtime",
            config_entry=config_entry,
        )
        self._client = client
        self._local_tz = local_tz
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

    async def _async_update_data(self) -> BeestatRuntimeData:
        try:
            return await self._async_fetch_runtime_data(skip_sync=False)
        except BeestatAuthError as err:
            raise ConfigEntryAuthFailed(self._client.redact_error(err)) from err
        except Exception as err:
            raise UpdateFailed(self._client.redact_error(err)) from err

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
            self.async_set_update_error(err)
            if isinstance(err, BeestatAuthError):
                self.config_entry.async_start_reauth_if_available(self.hass)
            raise
        self.async_set_updated_data(data)
        return data

    async def async_dismiss_filter_alerts(self, thermostat_id: int) -> int:
        """Dismiss active Beestat filter alerts for one thermostat."""

        self.last_filter_alert_dismiss_attempt_at = datetime.now(timezone.utc)
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
        self.last_import_success_at = datetime.now(timezone.utc)
        self.last_imported_series = imported_series
        self.last_imported_rows = imported_rows
        self.last_import_source_rows = source_rows
        self.last_import_partial = skipped_windows > 0
        self.last_import_skipped_windows = skipped_windows
        self.last_import_skipped_runtime_thermostat_windows = (
            skipped_runtime_thermostat_windows
        )
        self.last_import_skipped_runtime_sensor_windows = skipped_runtime_sensor_windows
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
                now = datetime.now(timezone.utc)
                sync_success_at = now
                metadata_sync_success_at = now
            thermostat_rows = await self._client.async_read_id("thermostat")
            sensor_rows = await self._client.async_read_id("sensor")
            thermostat_rows_tuple = tuple(
                row for row in thermostat_rows if not row.get("deleted")
            )
            sensor_rows_tuple = tuple(
                row for row in sensor_rows if not row.get("deleted")
            )
            if summary_window:
                today = datetime.now(timezone.utc).astimezone(self._local_tz).date()
                config = build_beestat_config(
                    self.hass,
                    thermostat_rows_tuple,
                    sensor_rows_tuple,
                    entry_runtime_config_data(self.config_entry),
                )
                summary_start = self._summary_window_start(
                    config,
                    thermostat_rows_tuple,
                    today,
                )
                try:
                    rows = await self._client.async_read_runtime_thermostat_summary(
                        summary_start.isoformat(),
                        today.isoformat(),
                    )
                except BeestatAuthError:
                    raise
                except BeestatApiError as err:
                    _LOGGER.warning(
                        (
                            "Falling back to full Beestat summary status read "
                            "after windowed read failed: %s"
                        ),
                        self._client.redact_error(err),
                    )
                    rows = await self._client.async_read_id(
                        "runtime_thermostat_summary"
                    )
                    summary_rows_full = True
                    summary_window_start = None
                    summary_window_end = None
                else:
                    summary_rows_full = False
                    summary_window_start = summary_start
                    summary_window_end = today
            else:
                rows = await self._client.async_read_id("runtime_thermostat_summary")
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
        self.last_error_at = datetime.now(timezone.utc)
        self.async_update_listeners()

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
    ) -> BeestatRuntimeData:
        fetched_at = datetime.now(timezone.utc)
        today = fetched_at.astimezone(self._local_tz).date()
        rows_tuple = tuple(row for row in rows if not row.get("deleted"))
        thermostat_rows_tuple = tuple(
            row for row in thermostat_rows if not row.get("deleted")
        )
        sensor_rows_tuple = tuple(
            row for row in sensor_rows if not row.get("deleted")
        )
        config = build_beestat_config(
            self.hass,
            thermostat_rows_tuple,
            sensor_rows_tuple,
            entry_runtime_config_data(self.config_entry),
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
                filter_runtime_hours=_runtime_hours_since(
                    thermostat_rows,
                    changed_date,
                ),
                recent_runtime_hours_per_day=_recent_runtime_hours_per_day(
                    thermostat_rows,
                    today,
                ),
            )

        return BeestatRuntimeData(
            config=config,
            fetched_at=fetched_at,
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
            thermostat_metadata=_build_thermostat_metadata(
                thermostat_rows_tuple,
                sensor_metadata,
                fetched_at,
                self._local_tz,
                config.thermostats,
            ),
            sensor_metadata=sensor_metadata,
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


def _latest_row_date(rows: list[dict[str, Any]]) -> date | None:
    dates = [_parse_date(row.get("date")) for row in rows]
    valid_dates = [item for item in dates if item is not None]
    return max(valid_dates) if valid_dates else None


def _runtime_hours_since(
    rows: list[dict[str, Any]],
    changed_date: date | None,
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
    return round(_sum_fan_seconds(matched_rows) / 3600, 1)


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
    return round((_sum_fan_seconds(matched_rows) / 3600) / len(matched_rows), 2)


def _sum_fan_seconds(rows: list[dict[str, Any]]) -> float:
    return sum(_float_or_zero(row.get("sum_fan")) for row in rows)


def _thermostat_row(
    rows: tuple[dict[str, Any], ...],
    thermostat_id: int,
) -> dict[str, Any] | None:
    for row in rows:
        if _row_int(row, "thermostat_id", "id") == thermostat_id:
            return row
    return None


def _build_sensor_metadata(rows: tuple[dict[str, Any], ...]) -> dict[int, SensorMetadata]:
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
            current_profile_sensor_names=current_profile_sensors,
            active_alert_count=len(active_alerts),
            active_alerts=active_alerts,
        )
    return metadata


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


def _current_profile(row: dict[str, Any]) -> tuple[str | None, str | None, tuple[str, ...]]:
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
        sensor_names = (
            tuple(
                item["name"]
                for item in sensors
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            )
            if isinstance(sensors, list)
            else ()
        )
        return current_ref, _string_or_none(climate.get("name")) or current_ref, sensor_names
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
        day_index,
        slot_index,
        scheduled_ref,
    )
    next_profile = profile_by_ref.get(next_ref or "")
    return {
        "scheduled_ref": scheduled_ref,
        "scheduled_name": _profile_name(scheduled_profile, scheduled_ref),
        "next_ref": next_ref,
        "next_name": _profile_name(next_profile, next_ref),
        "next_at": next_at.astimezone(timezone.utc) if next_at else None,
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
    climates = program.get("climates")
    if not isinstance(climates, list):
        return {}

    profiles: dict[str, ScheduleProfile] = {}
    for climate in climates:
        if not isinstance(climate, dict):
            continue
        ref = _string_or_none(climate.get("climateRef"))
        if ref is None:
            continue
        sensors = climate.get("sensors")
        sensor_names = (
            tuple(
                item["name"]
                for item in sensors
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            )
            if isinstance(sensors, list)
            else ()
        )
        profiles[ref] = ScheduleProfile(
            ref=ref,
            name=_string_or_none(climate.get("name")) or ref,
            is_occupied=_optional_bool(climate.get("isOccupied")),
            sensors=sensor_names,
        )
    return profiles


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
        except Exception:
            continue
    return fallback


def _ecobee_day_index(value: datetime) -> int:
    return (value.weekday() + 1) % 7


def _schedule_ref(schedule: Any, day_index: int, slot_index: int) -> str | None:
    if not _valid_schedule(schedule):
        return None
    value = schedule[day_index][slot_index]
    return _string_or_none(value)


def _next_schedule_transition(
    schedule: Any,
    local_now: datetime,
    day_index: int,
    slot_index: int,
    current_ref: str | None,
) -> tuple[str | None, datetime | None]:
    slot_hour = local_now.hour
    slot_minute = 30 if local_now.minute >= 30 else 0
    slot_start = datetime.combine(
        local_now.date(),
        time(hour=slot_hour, minute=slot_minute),
        tzinfo=local_now.tzinfo,
    )
    for offset in range(1, 7 * 48 + 1):
        absolute_slot = day_index * 48 + slot_index + offset
        candidate_day = (absolute_slot // 48) % 7
        candidate_slot = absolute_slot % 48
        candidate_ref = _schedule_ref(schedule, candidate_day, candidate_slot)
        if candidate_ref is not None and candidate_ref != current_ref:
            return candidate_ref, slot_start + timedelta(minutes=30 * offset)
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
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
        return float(value)
    except (TypeError, ValueError):
        return 0.0

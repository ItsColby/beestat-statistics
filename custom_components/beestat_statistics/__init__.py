"""Import Beestat HVAC data into Home Assistant external statistics."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .api import BeestatAuthError, BeestatClient, exception_fingerprint
from .config_payload import migrate_entry_payload
from .const import (
    CONF_API_BASE,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
)
from .coordinator import BeestatRuntimeDataCoordinator
from .entity import (
    async_register_service_device,
    async_remove_cross_integration_device_ownership,
    is_beestat_only_device,
)
from .importer import BeestatStatisticsImporter
from .issues import (
    _async_track_override_issue_updates,
    _async_update_override_issues,
    async_set_insecure_api_base_issue,
)
from .migrations import (
    UNIQUE_ID_MIGRATION_MINOR_VERSION,
    _async_migrate_homekit_device_assignments,
    _current_beestat_device_identifiers,
    _migrate_legacy_unique_ids,
)
from .runtime import (
    BeestatStatisticsConfigEntry,
    BeestatStatisticsRuntime,
    entry_point_lookback_days,
    entry_scan_interval_seconds,
)
from .services import async_register_services
from .task_coalescer import CoalescingTaskScheduler
from .tracking import (
    _async_track_room_temperature_sources,
    _async_track_runtime_entity_states,
    _async_track_source_device_relinks,
    _async_track_time_zone_updates,
    _filter_changed_entity_ids,
)
from .url_validation import normalize_api_base

_LOGGER = logging.getLogger(__name__)

PLATFORMS: tuple[Platform, ...] = (
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.DATE,
    Platform.SENSOR,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""

    async_register_services(hass)
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
        scan_interval_seconds=entry_scan_interval_seconds(entry),
    )
    importer = BeestatStatisticsImporter(
        hass,
        client,
        coordinator,
        point_lookback_days=entry_point_lookback_days(entry),
    )
    runtime = BeestatStatisticsRuntime(
        client=client,
        coordinator=coordinator,
        importer=importer,
        scan_interval=timedelta(seconds=entry_scan_interval_seconds(entry)),
    )
    entry.runtime_data = runtime
    _async_track_time_zone_updates(hass, entry, coordinator)

    await coordinator.async_config_entry_first_refresh()
    async_register_service_device(hass, entry)
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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate Beestat Statistics config entries."""

    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Cannot migrate Beestat Statistics config entry from version %s.%s",
            entry.version,
            entry.minor_version,
        )
        return False

    if entry.minor_version < UNIQUE_ID_MIGRATION_MINOR_VERSION:
        _migrate_legacy_unique_ids(hass, entry)

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

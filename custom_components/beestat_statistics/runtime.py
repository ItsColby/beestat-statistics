"""Runtime objects for one Beestat Statistics config entry."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

from .api import BeestatApiError, BeestatAuthError, BeestatClient, exception_fingerprint
from .config_payload import (
    normalize_point_lookback_days,
    normalize_scan_interval_seconds,
)
from .const import CONF_POINT_LOOKBACK_DAYS, CONF_SCAN_INTERVAL_SECONDS, DOMAIN
from .coordinator import BeestatRuntimeDataCoordinator

if TYPE_CHECKING:
    from .importer import BeestatStatisticsImporter

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BeestatStatisticsRuntime:
    """Runtime data attached to a Home Assistant config entry."""

    client: BeestatClient
    coordinator: BeestatRuntimeDataCoordinator
    importer: BeestatStatisticsImporter
    scan_interval: timedelta


type BeestatStatisticsConfigEntry = ConfigEntry[BeestatStatisticsRuntime]


def entry_point_lookback_days(entry: BeestatStatisticsConfigEntry) -> int:
    """Return the entry's supported point-data lookback."""

    return normalize_point_lookback_days(entry.options.get(CONF_POINT_LOOKBACK_DAYS))


def entry_scan_interval_seconds(entry: BeestatStatisticsConfigEntry) -> int:
    """Return the entry's supported acquisition cadence."""

    return normalize_scan_interval_seconds(
        entry.options.get(CONF_SCAN_INTERVAL_SECONDS)
    )


@contextmanager
def beestat_error_boundary(
    coordinator: BeestatRuntimeDataCoordinator,
    failure: str,
    *,
    record_error: bool = False,
    generic_key: str = "beestat_request_failed",
    passthrough: tuple[type[Exception], ...] = (),
) -> Iterator[None]:
    """Map Beestat failures in the block to translated Home Assistant errors.

    Authentication failures start reauth. Other failures are logged by type only.
    Exceptions listed in passthrough propagate untouched for the caller to map.
    """

    def record(err: Exception) -> None:
        if record_error:
            coordinator.async_record_import_error(err)

    try:
        yield
    except passthrough:
        raise
    except BeestatAuthError as err:
        record(err)
        coordinator.beestat_config_entry.async_start_reauth_if_available(
            coordinator.hass
        )
        key = "beestat_auth_failed"
    except BeestatApiError as err:
        record(err)
        key = "beestat_request_failed"
    except Exception as err:  # noqa: BLE001 - sanitize at the HA boundary
        record(err)
        _LOGGER.error("Unexpected %s (%s)", failure, exception_fingerprint(err))
        key = generic_key
    else:
        return
    raise HomeAssistantError(translation_domain=DOMAIN, translation_key=key) from None

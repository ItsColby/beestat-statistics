"""Runtime objects for one Beestat Statistics config entry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

from .api import BeestatClient
from .config_payload import (
    normalize_point_lookback_days,
    normalize_scan_interval_seconds,
)
from .const import CONF_POINT_LOOKBACK_DAYS, CONF_SCAN_INTERVAL_SECONDS
from .coordinator import BeestatRuntimeDataCoordinator

if TYPE_CHECKING:
    from .importer import BeestatStatisticsImporter


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

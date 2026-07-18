"""Runtime objects for one Beestat Statistics config entry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, TypeAlias

from homeassistant.config_entries import ConfigEntry

from .api import BeestatClient
from .coordinator import BeestatRuntimeDataCoordinator

if TYPE_CHECKING:
    from . import BeestatStatisticsImporter


@dataclass(slots=True)
class BeestatStatisticsRuntime:
    """Runtime data attached to a Home Assistant config entry."""

    client: BeestatClient
    coordinator: BeestatRuntimeDataCoordinator
    importer: BeestatStatisticsImporter
    scan_interval: timedelta


BeestatStatisticsConfigEntry: TypeAlias = ConfigEntry[BeestatStatisticsRuntime]

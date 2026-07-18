"""Config-entry option mutation helpers."""

from __future__ import annotations

from datetime import date
import logging

from .api import BeestatApiError
from .config_payload import update_thermostat_override_options
from .const import CONF_FILTER_CHANGED_DATE


_LOGGER = logging.getLogger(__name__)


async def async_set_filter_changed_date(
    coordinator,
    thermostat_id: int,
    changed_date: date,
) -> None:
    """Persist and apply a native filter-changed date for one thermostat."""

    entry = coordinator.config_entry
    new_options = update_thermostat_override_options(
        entry.data,
        entry.options,
        thermostat_id,
        {CONF_FILTER_CHANGED_DATE: changed_date.isoformat()},
    )
    coordinator.hass.config_entries.async_update_entry(entry, options=new_options)
    try:
        dismissed = await coordinator.async_dismiss_filter_alerts(thermostat_id)
    except BeestatApiError as err:
        _LOGGER.warning(
            "Unable to dismiss Beestat filter alerts for thermostat_id=%s: %s",
            thermostat_id,
            err,
        )
    else:
        if dismissed:
            _LOGGER.info(
                "Dismissed %s Beestat filter alert(s) for thermostat_id=%s",
                dismissed,
                thermostat_id,
            )
    await coordinator.async_refresh_runtime(skip_sync=True)

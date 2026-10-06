"""Actionable Home Assistant Repairs owned by Beestat Statistics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .config_model import (
    build_beestat_config,
    configured_mapping_device_conflicts,
    configured_override_entity_domain_errors,
    configured_override_entity_ids,
    configured_unresolved_entity_ids,
)
from .config_payload import entry_runtime_config_data
from .const import DOMAIN
from .entity_reference import (
    configured_entity_references,
    entity_registry_event_matches_references,
)
from .runtime import BeestatStatisticsConfigEntry

INSECURE_API_BASE_ISSUE_ID = "insecure_api_base"
_MISSING_OVERRIDE_ENTITIES_ISSUE_ID = "missing_override_entities"
_INVALID_OVERRIDE_ENTITY_DOMAINS_ISSUE_ID = "invalid_override_entity_domains"
_MAPPING_DEVICE_CONFLICTS_ISSUE_ID = "mapping_device_conflicts"


@callback
def async_set_insecure_api_base_issue(
    hass: HomeAssistant,
    *,
    active: bool,
) -> None:
    """Create or clear the Repair for an invalid credential-bearing API URL."""

    if not active:
        ir.async_delete_issue(hass, DOMAIN, INSECURE_API_BASE_ISSUE_ID)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        INSECURE_API_BASE_ISSUE_ID,
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.ERROR,
        translation_key=INSECURE_API_BASE_ISSUE_ID,
    )


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
        ) and not entity_registry_event_matches_references(
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

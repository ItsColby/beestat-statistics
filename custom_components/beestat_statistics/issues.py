"""Actionable Home Assistant Repairs owned by Beestat Statistics."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

YAML_CONNECTION_CHANGE_ISSUE_ID = "yaml_connection_change_requires_reconfigure"


@callback
def async_set_yaml_connection_change_issue(
    hass: HomeAssistant,
    *,
    active: bool,
) -> None:
    """Create or clear the Repair for a blocked YAML connection replacement."""

    if not active:
        ir.async_delete_issue(hass, DOMAIN, YAML_CONNECTION_CHANGE_ISSUE_ID)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        YAML_CONNECTION_CHANGE_ISSUE_ID,
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.WARNING,
        translation_key=YAML_CONNECTION_CHANGE_ISSUE_ID,
    )

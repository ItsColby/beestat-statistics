"""Config flow for Beestat Statistics."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import BeestatApiError, BeestatAuthError, BeestatClient
from .config_payload import (
    connection_data_from_user_input,
    entry_runtime_config_data,
    merge_import_options,
    options_from_user_input,
    split_entry_payload,
    update_source_scope_options,
    update_sensor_override_options,
    update_thermostat_override_options,
)
from .const import (
    API_BASE,
    CONF_ACCOUNT_FINGERPRINT,
    CONF_API_BASE,
    CONF_CLIMATE_ENTITY_ID,
    CONF_FILTER_CHANGED_ENTITY_ID,
    CONF_FILTER_LIFETIME_RUNTIME_HOURS,
    CONF_FILTER_MAX_AGE_DAYS,
    CONF_FILTER_NOTICE_DAYS,
    CONF_ID,
    CONF_INCLUDE_AIR_QUALITY,
    CONF_INCLUDE_CO2,
    CONF_INCLUDE_TEMPERATURE,
    CONF_INCLUDE_VOC,
    CONF_MOTION_ENTITY_ID,
    CONF_OCCUPANCY_ENTITY_ID,
    CONF_POINT_LOOKBACK_DAYS,
    CONF_SCAN_INTERVAL_SECONDS,
    CONF_SENSORS,
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_THERMOSTAT_ID,
    CONF_THERMOSTATS,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_UNIQUE_ID,
    CONFIG_ENTRY_VERSION,
    CONFIG_TITLE,
    DEFAULT_FILTER_LIFETIME_RUNTIME_HOURS,
    DEFAULT_FILTER_MAX_AGE_DAYS,
    DEFAULT_FILTER_NOTICE_DAYS,
    DEFAULT_POINT_LOOKBACK_DAYS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_FILTER_LIFETIME_RUNTIME_HOURS,
    MAX_FILTER_MAX_AGE_DAYS,
    MAX_FILTER_NOTICE_DAYS,
    MAX_POINT_LOOKBACK_DAYS,
    MIN_SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

API_KEY_SELECTOR = TextSelector(
    TextSelectorConfig(
        type=TextSelectorType.PASSWORD,
        autocomplete="current-password",
    )
)
API_BASE_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
POINT_LOOKBACK_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=1,
        max=MAX_POINT_LOOKBACK_DAYS,
        mode=NumberSelectorMode.BOX,
        step=1,
    )
)
SCAN_INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL_SECONDS,
        mode=NumberSelectorMode.BOX,
        step=1,
    )
)
THERMOSTAT_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain="climate"),
)
TEMPERATURE_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain="sensor", device_class="temperature"),
)
OCCUPANCY_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain="binary_sensor", device_class="occupancy"),
)
MOTION_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain="binary_sensor", device_class="motion"),
)
FILTER_CHANGED_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain="input_datetime"),
)
FILTER_LIFETIME_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=1,
        max=MAX_FILTER_LIFETIME_RUNTIME_HOURS,
        mode=NumberSelectorMode.BOX,
        step=1,
    )
)
FILTER_MAX_AGE_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=1,
        max=MAX_FILTER_MAX_AGE_DAYS,
        mode=NumberSelectorMode.BOX,
        step=1,
    )
)
FILTER_NOTICE_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0,
        max=MAX_FILTER_NOTICE_DAYS,
        mode=NumberSelectorMode.BOX,
        step=1,
    )
)
BOOLEAN_SELECTOR = BooleanSelector()

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): API_KEY_SELECTOR,
        vol.Optional(CONF_API_BASE, default=API_BASE): API_BASE_SELECTOR,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(
            CONF_POINT_LOOKBACK_DAYS,
            default=DEFAULT_POINT_LOOKBACK_DAYS,
        ): POINT_LOOKBACK_SELECTOR,
        vol.Optional(
            CONF_SCAN_INTERVAL_SECONDS,
            default=DEFAULT_SCAN_INTERVAL_SECONDS,
        ): SCAN_INTERVAL_SELECTOR,
    }
)


OPTIONS_MENU = {
    "timing": "Import timing",
    "source_scope": "Choose Beestat sources",
    "thermostat_mapping": "Map a thermostat",
    "sensor_mapping": "Map a room sensor",
}

_CONF_INCLUDED_THERMOSTAT_IDS = "included_thermostat_ids"
_CONF_INCLUDED_SENSOR_IDS = "included_sensor_ids"


class BeestatStatisticsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Beestat Statistics config flow."""

    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION
    _pending_entry: config_entries.ConfigEntry | None = None
    _pending_data: dict[str, Any] | None = None
    _pending_options: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""

        return BeestatStatisticsOptionsFlow()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle setup initiated from the UI."""

        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(CONFIG_ENTRY_UNIQUE_ID)
            self._abort_if_unique_id_configured()
            if not str(user_input.get(CONF_API_KEY, "")).strip():
                errors[CONF_API_KEY] = "api_key_required"
            else:
                data, options = split_entry_payload(user_input)
                try:
                    account_fingerprint = await _async_validate_input(
                        self.hass,
                        data,
                    )
                except BeestatAuthError:
                    errors["base"] = "invalid_auth"
                except BeestatApiError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected exception validating Beestat setup")
                    errors["base"] = "unknown"
                else:
                    if account_fingerprint is not None:
                        data[CONF_ACCOUNT_FINGERPRINT] = account_fingerprint
                    return self.async_create_entry(
                        title=CONFIG_TITLE,
                        data=data,
                        options=options,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle updates to required connection data."""

        entry = self._get_reconfigure_entry()
        return await self._async_update_entry_data_flow(
            "reconfigure",
            entry,
            user_input,
        )

    async def async_step_reauth(
        self,
        _entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Handle Beestat API key reauthentication."""

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Prompt for updated Beestat credentials."""

        entry = self._get_reauth_entry()
        return await self._async_update_entry_data_flow(
            "reauth_confirm",
            entry,
            user_input,
            require_api_key=True,
        )

    async def async_step_account_change_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm an intentional switch to a different Beestat account."""

        if self._pending_entry is None or self._pending_data is None:
            if self.source == config_entries.SOURCE_RECONFIGURE:
                return await self.async_step_reconfigure()
            return await self.async_step_reauth_confirm()

        if user_input is not None:
            entry = self._pending_entry
            data = self._pending_data
            options = self._pending_options or {}
            self._pending_entry = None
            self._pending_data = None
            self._pending_options = None
            await self.async_set_unique_id(CONFIG_ENTRY_UNIQUE_ID)
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                entry,
                data=data,
                options=options,
                reload_even_if_entry_is_unchanged=False,
            )

        return self.async_show_form(
            step_id="account_change_confirm",
            data_schema=vol.Schema({}),
        )

    async def async_step_import(
        self,
        import_config: dict[str, Any],
    ) -> ConfigFlowResult:
        """Import YAML configuration."""

        await self.async_set_unique_id(CONFIG_ENTRY_UNIQUE_ID)
        data, options = split_entry_payload(import_config)
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN,
            CONFIG_ENTRY_UNIQUE_ID,
        )
        if entry is None:
            entries = self.hass.config_entries.async_entries(DOMAIN)
            entry = entries[0] if entries else None
        if entry is not None:
            if _same_connection_data(entry.data, data) and (
                account_fingerprint := entry.data.get(CONF_ACCOUNT_FINGERPRINT)
            ):
                data[CONF_ACCOUNT_FINGERPRINT] = account_fingerprint
            options = merge_import_options(entry.options, data, options)
            return self.async_update_reload_and_abort(
                entry,
                data=data,
                options=options,
                reason="already_configured",
                reload_even_if_entry_is_unchanged=False,
            )
        return self.async_create_entry(
            title=CONFIG_TITLE,
            data=data,
            options=options,
        )

    async def _async_update_entry_data_flow(
        self,
        step_id: str,
        entry: config_entries.ConfigEntry,
        user_input: dict[str, Any] | None,
        *,
        require_api_key: bool = False,
    ) -> ConfigFlowResult:
        """Validate and update config entry data from reconfigure/reauth flows."""

        errors: dict[str, str] = {}
        if user_input is not None:
            if require_api_key and not str(user_input.get(CONF_API_KEY, "")).strip():
                errors[CONF_API_KEY] = "api_key_required"
            else:
                data_updates = connection_data_from_user_input(entry.data, user_input)
                try:
                    account_fingerprint = await _async_validate_input(
                        self.hass,
                        data_updates,
                    )
                except BeestatAuthError:
                    errors["base"] = "invalid_auth"
                except BeestatApiError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected exception validating Beestat setup")
                    errors["base"] = "unknown"
                else:
                    if account_fingerprint is not None:
                        data_updates[CONF_ACCOUNT_FINGERPRINT] = account_fingerprint
                    elif CONF_ACCOUNT_FINGERPRINT in entry.data:
                        data_updates[CONF_ACCOUNT_FINGERPRINT] = entry.data[
                            CONF_ACCOUNT_FINGERPRINT
                        ]
                    if _wrong_account(entry.data, account_fingerprint):
                        self._pending_entry = entry
                        self._pending_data = {
                            key: value
                            for key, value in entry.data.items()
                            if key not in (CONF_THERMOSTATS, CONF_SENSORS)
                        }
                        self._pending_data.update(data_updates)
                        self._pending_options = {
                            key: value
                            for key, value in entry.options.items()
                            if key not in (CONF_THERMOSTATS, CONF_SENSORS)
                        }
                        return await self.async_step_account_change_confirm()
                    self._pending_entry = None
                    self._pending_data = None
                    self._pending_options = None
                    await self.async_set_unique_id(CONFIG_ENTRY_UNIQUE_ID)
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates=data_updates,
                        reload_even_if_entry_is_unchanged=False,
                    )

        return self.async_show_form(
            step_id=step_id,
            data_schema=_connection_data_schema(
                entry.data,
                allow_blank_api_key=not require_api_key,
            ),
            errors=errors,
        )


class BeestatStatisticsOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle Beestat Statistics options."""

    _thermostat_id: int | None = None
    _sensor_id: int | None = None
    _pending_scope_options: dict[str, Any] | None = None
    _pending_scope_removed_thermostats = 0
    _pending_scope_removed_sensors = 0

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage integration options."""

        return self.async_show_menu(
            step_id="init",
            menu_options=OPTIONS_MENU,
        )

    async def async_step_timing(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage import timing options."""

        if user_input is not None:
            return self.async_create_entry(
                data={
                    **dict(self.config_entry.options),
                    **options_from_user_input(user_input),
                }
            )

        return self.async_show_form(
            step_id="timing",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                self.config_entry.options,
            ),
        )

    async def async_step_source_scope(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose which discovered Beestat resources are exposed and imported."""

        thermostat_options = _thermostat_options(self.config_entry)
        sensor_options = _sensor_options(self.config_entry)
        enabled_thermostats, enabled_sensors = _source_scope_defaults(
            self.config_entry
        )
        if user_input is not None:
            selected_thermostats = {
                int(value)
                for value in user_input.get(_CONF_INCLUDED_THERMOSTAT_IDS, ())
            }
            selected_sensors = {
                int(value)
                for value in user_input.get(_CONF_INCLUDED_SENSOR_IDS, ())
            }
            new_options = update_source_scope_options(
                self.config_entry.data,
                self.config_entry.options,
                known_thermostat_ids=_option_ids(thermostat_options),
                enabled_thermostat_ids=tuple(sorted(selected_thermostats)),
                explicitly_enabled_thermostat_ids=tuple(
                    sorted(
                        selected_thermostats
                        & _inactive_resource_ids(
                            self.config_entry,
                            rows_attribute="thermostat_rows",
                        )
                    )
                ),
                known_sensor_ids=_option_ids(sensor_options),
                enabled_sensor_ids=tuple(sorted(selected_sensors)),
                explicitly_enabled_sensor_ids=tuple(
                    sorted(
                        selected_sensors
                        & _inactive_resource_ids(
                            self.config_entry,
                            rows_attribute="sensor_rows",
                        )
                    )
                ),
            )
            removed_thermostats = len(enabled_thermostats - selected_thermostats)
            removed_sensors = len(enabled_sensors - selected_sensors)
            if removed_thermostats or removed_sensors:
                self._pending_scope_options = new_options
                self._pending_scope_removed_thermostats = removed_thermostats
                self._pending_scope_removed_sensors = removed_sensors
                return await self.async_step_source_scope_confirm()
            return self.async_create_entry(data=new_options)

        return self.async_show_form(
            step_id="source_scope",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        _CONF_INCLUDED_THERMOSTAT_IDS,
                        default=[str(item) for item in sorted(enabled_thermostats)],
                    ): _select_selector(thermostat_options, multiple=True),
                    vol.Required(
                        _CONF_INCLUDED_SENSOR_IDS,
                        default=[str(item) for item in sorted(enabled_sensors)],
                    ): _select_selector(sensor_options, multiple=True),
                }
            ),
            errors=(
                {}
                if thermostat_options or sensor_options
                else {"base": "no_discovered_items"}
            ),
        )

    async def async_step_source_scope_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm removal of currently exposed Beestat resources."""

        if self._pending_scope_options is None:
            return await self.async_step_source_scope()
        if user_input is not None:
            options = self._pending_scope_options
            self._pending_scope_options = None
            return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="source_scope_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "thermostat_count": str(self._pending_scope_removed_thermostats),
                "sensor_count": str(self._pending_scope_removed_sensors),
            },
        )

    async def async_step_thermostat_mapping(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select a Beestat thermostat to map."""

        if user_input is not None:
            self._thermostat_id = int(user_input[CONF_ID])
            return await self.async_step_thermostat_mapping_detail()

        return self.async_show_form(
            step_id="thermostat_mapping",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ID): _select_selector(
                        _thermostat_options(self.config_entry)
                    ),
                }
            ),
            errors=_selection_errors(_thermostat_options(self.config_entry)),
        )

    async def async_step_thermostat_mapping_detail(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Configure one Beestat thermostat mapping."""

        if self._thermostat_id is None:
            return await self.async_step_thermostat_mapping()

        if user_input is not None:
            return self.async_create_entry(
                data=update_thermostat_override_options(
                    self.config_entry.data,
                    self.config_entry.options,
                    self._thermostat_id,
                    user_input,
                )
            )

        defaults = _override_defaults(
            self.config_entry,
            self._thermostat_id,
            thermostats=True,
        )
        defaults = {
            CONF_FILTER_LIFETIME_RUNTIME_HOURS: DEFAULT_FILTER_LIFETIME_RUNTIME_HOURS,
            CONF_FILTER_MAX_AGE_DAYS: DEFAULT_FILTER_MAX_AGE_DAYS,
            CONF_FILTER_NOTICE_DAYS: DEFAULT_FILTER_NOTICE_DAYS,
            **defaults,
        }
        return self.async_show_form(
            step_id="thermostat_mapping_detail",
            description_placeholders=_thermostat_placeholders(
                self.config_entry,
                self._thermostat_id,
            ),
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Optional(CONF_CLIMATE_ENTITY_ID): THERMOSTAT_ENTITY_SELECTOR,
                        vol.Optional(CONF_TEMPERATURE_ENTITY_ID): TEMPERATURE_ENTITY_SELECTOR,
                        vol.Optional(CONF_OCCUPANCY_ENTITY_ID): OCCUPANCY_ENTITY_SELECTOR,
                        vol.Optional(CONF_MOTION_ENTITY_ID): MOTION_ENTITY_SELECTOR,
                        vol.Optional(CONF_FILTER_CHANGED_ENTITY_ID): (
                            FILTER_CHANGED_ENTITY_SELECTOR
                        ),
                        vol.Optional(CONF_FILTER_LIFETIME_RUNTIME_HOURS): (
                            FILTER_LIFETIME_SELECTOR
                        ),
                        vol.Optional(CONF_FILTER_MAX_AGE_DAYS): FILTER_MAX_AGE_SELECTOR,
                        vol.Optional(CONF_FILTER_NOTICE_DAYS): FILTER_NOTICE_SELECTOR,
                    }
                ),
                defaults,
            ),
        )

    async def async_step_sensor_mapping(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select a Beestat room sensor to map."""

        if user_input is not None:
            self._sensor_id = int(user_input[CONF_ID])
            return await self.async_step_sensor_mapping_detail()

        return self.async_show_form(
            step_id="sensor_mapping",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ID): _select_selector(
                        _sensor_options(self.config_entry)
                    ),
                }
            ),
            errors=_selection_errors(_sensor_options(self.config_entry)),
        )

    async def async_step_sensor_mapping_detail(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Configure one Beestat room-sensor mapping."""

        if self._sensor_id is None:
            return await self.async_step_sensor_mapping()

        if user_input is not None:
            return self.async_create_entry(
                data=update_sensor_override_options(
                    self.config_entry.data,
                    self.config_entry.options,
                    self._sensor_id,
                    user_input,
                )
            )

        defaults = _override_defaults(
            self.config_entry,
            self._sensor_id,
            thermostats=False,
        )
        return self.async_show_form(
            step_id="sensor_mapping_detail",
            description_placeholders=_sensor_placeholders(
                self.config_entry,
                self._sensor_id,
            ),
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Optional(CONF_THERMOSTAT_ID): _select_selector(
                            _thermostat_options(self.config_entry),
                            custom_value=True,
                        ),
                        vol.Optional(CONF_TEMPERATURE_ENTITY_ID): TEMPERATURE_ENTITY_SELECTOR,
                        vol.Optional(CONF_OCCUPANCY_ENTITY_ID): OCCUPANCY_ENTITY_SELECTOR,
                        vol.Optional(CONF_MOTION_ENTITY_ID): MOTION_ENTITY_SELECTOR,
                        vol.Optional(CONF_INCLUDE_TEMPERATURE): BOOLEAN_SELECTOR,
                        vol.Optional(CONF_INCLUDE_AIR_QUALITY): BOOLEAN_SELECTOR,
                        vol.Optional(CONF_INCLUDE_CO2): BOOLEAN_SELECTOR,
                        vol.Optional(CONF_INCLUDE_VOC): BOOLEAN_SELECTOR,
                    }
                ),
                defaults,
            ),
        )


async def _async_validate_input(
    hass: HomeAssistant,
    user_input: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate that Beestat accepts the configured API key."""

    client = BeestatClient(
        async_get_clientsession(hass),
        user_input[CONF_API_KEY],
        user_input[CONF_API_BASE],
        timeout=20,
        retries=1,
    )
    thermostat_rows = await client.async_read_id("thermostat")
    return _account_fingerprint(thermostat_rows)


def _account_fingerprint(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a non-reversible fingerprint for the discovered Beestat account."""

    row_ids = sorted(
        {
            row_id
            for row in rows
            if isinstance(row, Mapping) and (row_id := _row_identifier(row)) is not None
        }
    )
    if not row_ids:
        return None
    thermostat_hashes = [_hash_text(row_id) for row_id in row_ids]
    return {
        "thermostat_id_hashes": thermostat_hashes,
        "signature": _hash_text("\x1f".join(row_ids)),
    }


def _row_identifier(row: Mapping[str, Any]) -> str | None:
    """Return the Beestat row identifier used for account fingerprinting."""

    for key in ("thermostat_id", "id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _same_connection_data(
    current_data: Mapping[str, Any],
    new_data: Mapping[str, Any],
) -> bool:
    """Return whether two entry payloads point at the same Beestat connection."""

    return (
        current_data.get(CONF_API_KEY) == new_data.get(CONF_API_KEY)
        and current_data.get(CONF_API_BASE, API_BASE)
        == new_data.get(CONF_API_BASE, API_BASE)
    )


def _wrong_account(
    current_data: Mapping[str, Any],
    account_fingerprint: Any | None,
) -> bool:
    """Return whether validation appears to target a different Beestat account."""

    current_fingerprint = current_data.get(CONF_ACCOUNT_FINGERPRINT)
    if not current_fingerprint or not account_fingerprint:
        return False

    current_hashes = _thermostat_hashes(current_fingerprint)
    proposed_hashes = _thermostat_hashes(account_fingerprint)
    if current_hashes and proposed_hashes:
        return current_hashes.isdisjoint(proposed_hashes)

    return _fingerprint_signature(current_fingerprint) != _fingerprint_signature(
        account_fingerprint
    )


def _thermostat_hashes(fingerprint: Any) -> set[str]:
    """Return per-thermostat hash anchors from a stored account fingerprint."""

    if not isinstance(fingerprint, Mapping):
        return set()
    values = fingerprint.get("thermostat_id_hashes")
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if value not in (None, "")}


def _fingerprint_signature(fingerprint: Any) -> str:
    """Return a comparable fallback signature for older stored fingerprints."""

    if isinstance(fingerprint, Mapping):
        return str(fingerprint.get("signature") or "")
    return str(fingerprint)


def _hash_text(value: str) -> str:
    """Return a SHA-256 hash for config-entry account anchors."""

    return hashlib.sha256(value.encode()).hexdigest()


def _connection_data_schema(
    current_data: Mapping[str, Any],
    *,
    allow_blank_api_key: bool,
) -> vol.Schema:
    """Return a schema for updating required Beestat connection data."""

    api_key_field = (
        vol.Required(CONF_API_KEY, default="")
        if allow_blank_api_key
        else vol.Required(CONF_API_KEY)
    )
    return vol.Schema(
        {
            api_key_field: API_KEY_SELECTOR,
            vol.Optional(
                CONF_API_BASE,
                default=current_data.get(CONF_API_BASE, API_BASE),
            ): API_BASE_SELECTOR,
        }
    )


def _select_selector(
    options: list[SelectOptionDict],
    *,
    custom_value: bool = False,
    multiple: bool = False,
) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=(
                options
                if options or multiple
                else [SelectOptionDict(value="", label="No discovered items")]
            ),
            custom_value=custom_value,
            multiple=multiple,
        )
    )


def _thermostat_options(entry: config_entries.ConfigEntry) -> list[SelectOptionDict]:
    return _resource_options(
        entry,
        rows_attribute="thermostat_rows",
        config_attribute="thermostats",
        config_id_attribute="thermostat_id",
        override_key=CONF_THERMOSTATS,
        fallback_label="Thermostat",
    )


def _sensor_options(entry: config_entries.ConfigEntry) -> list[SelectOptionDict]:
    return _resource_options(
        entry,
        rows_attribute="sensor_rows",
        config_attribute="sensors",
        config_id_attribute="sensor_id",
        override_key=CONF_SENSORS,
        fallback_label="Sensor",
    )


def _resource_options(
    entry: config_entries.ConfigEntry,
    *,
    rows_attribute: str,
    config_attribute: str,
    config_id_attribute: str,
    override_key: str,
    fallback_label: str,
) -> list[SelectOptionDict]:
    """Return discovered plus saved resource choices, including disabled rows."""

    runtime = getattr(entry, "runtime_data", None)
    data = runtime.coordinator.data if runtime is not None else None
    labels: dict[int, str] = {}
    inactive: set[int] = set()
    configured = getattr(data.config, config_attribute, ()) if data is not None else ()
    for item in configured:
        item_id = int(getattr(item, config_id_attribute))
        labels[item_id] = str(item.name)
    rows = getattr(data, rows_attribute, ()) if data is not None else ()
    for row in rows:
        item_id = _resource_row_id(row)
        if item_id is None:
            continue
        label = row.get("name") or f"{fallback_label} {item_id}"
        labels.setdefault(item_id, str(label))
        if _source_flag_enabled(row.get("inactive")):
            inactive.add(item_id)
    config_data = entry_runtime_config_data(entry)
    for item in config_data.get(override_key, ()):
        if not isinstance(item, Mapping):
            continue
        try:
            item_id = int(item.get(CONF_ID))
        except (TypeError, ValueError):
            continue
        labels.setdefault(item_id, f"Saved {fallback_label.lower()} {item_id}")

    return sorted(
        (
            SelectOptionDict(
                value=str(item_id),
                label=(
                    f"{label} ({item_id}, inactive)"
                    if item_id in inactive
                    else f"{label} ({item_id})"
                ),
            )
            for item_id, label in labels.items()
        ),
        key=lambda option: str(option["label"]).casefold(),
    )


def _resource_row_id(row: Mapping[str, Any]) -> int | None:
    for key in ("thermostat_id", "sensor_id", "id"):
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _option_ids(options: list[SelectOptionDict]) -> tuple[int, ...]:
    return tuple(int(option["value"]) for option in options)


def _inactive_resource_ids(
    entry: config_entries.ConfigEntry,
    *,
    rows_attribute: str,
) -> set[int]:
    runtime = getattr(entry, "runtime_data", None)
    data = runtime.coordinator.data if runtime is not None else None
    rows = getattr(data, rows_attribute, ()) if data is not None else ()
    return {
        item_id
        for row in rows
        if (item_id := _resource_row_id(row)) is not None
        and _source_flag_enabled(row.get("inactive"))
    }


def _source_flag_enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _source_scope_defaults(
    entry: config_entries.ConfigEntry,
) -> tuple[set[int], set[int]]:
    runtime = getattr(entry, "runtime_data", None)
    data = runtime.coordinator.data if runtime is not None else None
    if data is None:
        return set(), set()
    return (
        {
            int(thermostat.thermostat_id)
            for thermostat in data.config.thermostats
        },
        {
            int(sensor.sensor_id)
            for sensor in data.config.sensors
        },
    )


def _thermostat_placeholders(
    entry: config_entries.ConfigEntry,
    thermostat_id: int,
) -> dict[str, str]:
    return {
        "item": _option_label(_thermostat_options(entry), thermostat_id),
    }


def _sensor_placeholders(
    entry: config_entries.ConfigEntry,
    sensor_id: int,
) -> dict[str, str]:
    return {
        "item": _option_label(_sensor_options(entry), sensor_id),
    }


def _option_label(options: list[SelectOptionDict], item_id: int) -> str:
    item_value = str(item_id)
    for option in options:
        if option["value"] == item_value:
            return str(option["label"])
    return item_value


def _selection_errors(options: list[SelectOptionDict]) -> dict[str, str]:
    return {} if options else {"base": "no_discovered_items"}


def _override_defaults(
    entry: config_entries.ConfigEntry,
    item_id: int,
    *,
    thermostats: bool,
) -> dict[str, Any]:
    key = "thermostats" if thermostats else "sensors"
    config_data = entry_runtime_config_data(entry)
    for item in config_data.get(key, ()):
        if isinstance(item, Mapping) and int(item.get(CONF_ID, -1)) == item_id:
            return dict(item)
    return {}

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
    CONF_TEMPERATURE_ENTITY_ID,
    CONF_THERMOSTAT_ID,
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
    "thermostat_mapping": "Map a thermostat",
    "sensor_mapping": "Map a room sensor",
}


class BeestatStatisticsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Beestat Statistics config flow."""

    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

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
                    if _wrong_account(entry.data, account_fingerprint):
                        errors["base"] = "wrong_account"
                    else:
                        if account_fingerprint is not None:
                            data_updates[CONF_ACCOUNT_FINGERPRINT] = (
                                account_fingerprint
                            )
                        elif CONF_ACCOUNT_FINGERPRINT in entry.data:
                            data_updates[CONF_ACCOUNT_FINGERPRINT] = entry.data[
                                CONF_ACCOUNT_FINGERPRINT
                            ]
                    if errors:
                        return self.async_show_form(
                            step_id=step_id,
                            data_schema=_connection_data_schema(
                                entry.data,
                                allow_blank_api_key=not require_api_key,
                            ),
                            errors=errors,
                        )
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
) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=options or [SelectOptionDict(value="", label="No discovered items")],
            custom_value=custom_value,
        )
    )


def _thermostat_options(entry: config_entries.ConfigEntry) -> list[SelectOptionDict]:
    runtime = getattr(entry, "runtime_data", None)
    data = runtime.coordinator.data if runtime is not None else None
    thermostats = data.config.thermostats if data is not None else ()
    return [
        SelectOptionDict(
            value=str(thermostat.thermostat_id),
            label=f"{thermostat.name} ({thermostat.thermostat_id})",
        )
        for thermostat in thermostats
    ]


def _sensor_options(entry: config_entries.ConfigEntry) -> list[SelectOptionDict]:
    runtime = getattr(entry, "runtime_data", None)
    data = runtime.coordinator.data if runtime is not None else None
    sensors = data.config.sensors if data is not None else ()
    return [
        SelectOptionDict(
            value=str(sensor.sensor_id),
            label=f"{sensor.name} ({sensor.sensor_id})",
        )
        for sensor in sensors
    ]


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

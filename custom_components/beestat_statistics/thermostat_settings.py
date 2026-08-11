"""Privacy-safe Ecobee settings projection from Beestat-owned source rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Any

# These fields are deliberately explicit. The upstream ecobee_thermostat row also
# contains account, location, billing, utility, management, device-identifier, and
# access-code data. None of those broad/private objects may cross this boundary.
_SETTING_GROUPS: dict[str, tuple[str, ...]] = {
    "comfort_and_schedule": (
        "autoAway",
        "autoHeatCoolFeatureEnabled",
        "disablePreCooling",
        "disablePreHeating",
        "fanMinOnTime",
        "fanSpeed",
        "followMeComfort",
        "holdAction",
        "hvacMode",
        "maxSetBack",
        "maxSetForward",
        "quickSaveSetBack",
        "quickSaveSetForward",
        "smartCirculation",
    ),
    "temperature_and_staging": (
        "auxMaxOutdoorTemp",
        "compressorProtectionMinTemp",
        "compressorProtectionMinTime",
        "coolMaxTemp",
        "coolMinTemp",
        "coolRangeHigh",
        "coolRangeLow",
        "coolStages",
        "coolingLockout",
        "heatCoolMinDelta",
        "heatMaxTemp",
        "heatMinTemp",
        "heatRangeHigh",
        "heatRangeLow",
        "heatStages",
        "randomStartDelayCool",
        "randomStartDelayHeat",
        "stage1CoolingDifferentialTemp",
        "stage1CoolingDissipationTime",
        "stage1HeatingDifferentialTemp",
        "stage1HeatingDissipationTime",
        "tempCorrection",
    ),
    "humidity_and_ventilation": (
        "condensationAvoid",
        "dehumidifierLevel",
        "dehumidifierMode",
        "dehumidifyOvercoolOffset",
        "dehumidifyWhenHeating",
        "dehumidifyWithAC",
        "humidifierMode",
        "humidity",
        "isVentilatorTimerOn",
        "vent",
        "ventilatorDehumidify",
        "ventilatorFreeCooling",
        "ventilatorMinOnTime",
        "ventilatorMinOnTimeAway",
        "ventilatorMinOnTimeHome",
        "ventilatorOffDateTime",
        "ventilatorType",
    ),
    "equipment": (
        "fanControlRequired",
        "hasBoiler",
        "hasDehumidifier",
        "hasElectric",
        "hasErv",
        "hasForcedAir",
        "hasHeatPump",
        "hasHrv",
        "hasHumidifier",
        "hasUVFilter",
        "heatPumpGroundWater",
        "heatPumpReversalOnCool",
        "useZoneController",
    ),
    "alerts_and_reminders": (
        "auxOutdoorTempAlert",
        "auxOutdoorTempAlertNotify",
        "auxOutdoorTempAlertNotifyTechnician",
        "auxRuntimeAlert",
        "auxRuntimeAlertNotify",
        "auxRuntimeAlertNotifyTechnician",
        "coldTempAlert",
        "coldTempAlertEnabled",
        "disableAlertsOnIdt",
        "disableHeatPumpAlerts",
        "hotTempAlert",
        "hotTempAlertEnabled",
        "humidityAlertNotify",
        "humidityAlertNotifyTechnician",
        "humidityHighAlert",
        "humidityLowAlert",
        "lastServiceDate",
        "monthsBetweenService",
        "remindMeDate",
        "serviceRemindMe",
        "serviceRemindTechnician",
        "tempAlertNotify",
        "tempAlertNotifyTechnician",
        "wifiOfflineAlert",
    ),
    "display_and_access": (
        "backlightOffDuringSleep",
        "backlightOffTime",
        "backlightOnIntensity",
        "backlightSleepIntensity",
        "displayAirQuality",
        "drAccept",
        "firstRunComplete",
        "installerCodeRequired",
        "isRentalProperty",
        "locale",
        "soundAlertVolume",
        "soundTickVolume",
        "useCelsius",
        "useTimeFormat12",
        "userAccessSetting",
    ),
}

_AUDIO_FIELDS = (
    "microphoneEnabled",
    "playbackVolume",
    "soundAlertVolume",
    "soundTickVolume",
)

_TENTHS_FAHRENHEIT_FIELDS = frozenset(
    {
        "auxMaxOutdoorTemp",
        "auxOutdoorTempAlert",
        "coldTempAlert",
        "compressorProtectionMinTemp",
        "coolMaxTemp",
        "coolMinTemp",
        "coolRangeHigh",
        "coolRangeLow",
        "dehumidifyOvercoolOffset",
        "heatCoolMinDelta",
        "heatMaxTemp",
        "heatMinTemp",
        "heatRangeHigh",
        "heatRangeLow",
        "hotTempAlert",
        "maxSetBack",
        "maxSetForward",
        "quickSaveSetBack",
        "quickSaveSetForward",
        "stage1CoolingDifferentialTemp",
        "stage1HeatingDifferentialTemp",
        "tempCorrection",
    }
)

_SECOND_FIELDS = frozenset(
    {
        "backlightOffTime",
        "compressorProtectionMinTime",
        "stage1CoolingDissipationTime",
        "stage1HeatingDissipationTime",
    }
)

_MINUTE_FIELDS = frozenset(
    {
        "fanMinOnTime",
        "ventilatorMinOnTime",
        "ventilatorMinOnTimeAway",
        "ventilatorMinOnTimeHome",
    }
)

_PERCENT_FIELDS = frozenset(
    {
        "dehumidifierLevel",
        "humidity",
        "humidityHighAlert",
        "humidityLowAlert",
        "playbackVolume",
    }
)


@dataclass(frozen=True, slots=True)
class ThermostatSettingsSnapshot:
    """One strictly allow-listed settings snapshot keyed to a Beestat thermostat."""

    thermostat_id: int
    source_details: dict[str, Any]
    settings: dict[str, Any]
    audio: dict[str, Any]

    def setting(self, key: str) -> Any:
        """Return one source setting without exposing an arbitrary row lookup."""

        return self.settings.get(key)


def build_thermostat_settings_snapshots(
    thermostat_rows: tuple[dict[str, Any], ...],
    ecobee_thermostat_rows: list[dict[str, Any]],
) -> dict[int, ThermostatSettingsSnapshot]:
    """Map Beestat thermostats to privacy-safe raw Ecobee configuration."""

    raw_by_id = {
        row_id: row
        for row in ecobee_thermostat_rows
        if isinstance(row, dict)
        if not _truthy(row.get("inactive")) and not _truthy(row.get("deleted"))
        if (row_id := _int_or_none(row.get("ecobee_thermostat_id", row.get("id"))))
        is not None
    }
    snapshots: dict[int, ThermostatSettingsSnapshot] = {}
    for thermostat_row in thermostat_rows:
        thermostat_id = _int_or_none(
            thermostat_row.get("thermostat_id", thermostat_row.get("id"))
        )
        ecobee_thermostat_id = _int_or_none(thermostat_row.get("ecobee_thermostat_id"))
        if thermostat_id is None or ecobee_thermostat_id is None:
            continue
        raw = raw_by_id.get(ecobee_thermostat_id)
        if raw is None:
            continue
        settings = _safe_mapping(raw.get("settings"), _all_setting_fields())
        audio = _safe_mapping(raw.get("audio"), _AUDIO_FIELDS)
        groups = {
            group: {
                key: _configuration_value(key, settings[key])
                for key in keys
                if key in settings
            }
            for group, keys in _SETTING_GROUPS.items()
        }
        groups = {group: values for group, values in groups.items() if values}
        if audio:
            groups["audio"] = {
                key: _configuration_value(key, value) for key, value in audio.items()
            }
        snapshots[thermostat_id] = ThermostatSettingsSnapshot(
            thermostat_id=thermostat_id,
            source_details=groups,
            settings=settings,
            audio=audio,
        )
    return snapshots


def temperature_fahrenheit(
    snapshot: ThermostatSettingsSnapshot, key: str
) -> float | None:
    """Return one Ecobee tenths-Fahrenheit setting as Fahrenheit."""

    value = _finite_float_or_none(snapshot.setting(key))
    return round(value / 10, 1) if value is not None else None


def integer_setting(snapshot: ThermostatSettingsSnapshot, key: str) -> int | None:
    """Return one exact integer setting."""

    value = snapshot.setting(key)
    if isinstance(value, bool):
        return None
    return _int_or_none(value)


def boolean_setting(snapshot: ThermostatSettingsSnapshot, key: str) -> bool | None:
    """Return one exact boolean setting."""

    value = snapshot.setting(key)
    return value if isinstance(value, bool) else None


def text_setting(snapshot: ThermostatSettingsSnapshot, key: str) -> str | None:
    """Return one non-empty text setting."""

    value = snapshot.setting(key)
    return value if isinstance(value, str) and value else None


def date_setting(snapshot: ThermostatSettingsSnapshot, key: str) -> date | None:
    """Return one ISO calendar date without inventing timezone semantics."""

    value = text_setting(snapshot, key)
    if value is None:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def audio_boolean_setting(
    snapshot: ThermostatSettingsSnapshot, key: str
) -> bool | None:
    """Return one exact allow-listed audio boolean."""

    value = snapshot.audio.get(key)
    return value if isinstance(value, bool) else None


def audio_integer_setting(snapshot: ThermostatSettingsSnapshot, key: str) -> int | None:
    """Return one exact allow-listed audio integer."""

    value = snapshot.audio.get(key)
    if isinstance(value, bool):
        return None
    return _int_or_none(value)


def _all_setting_fields() -> tuple[str, ...]:
    return tuple(key for keys in _SETTING_GROUPS.values() for key in keys)


def _safe_mapping(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in keys:
        scalar = _safe_scalar(value.get(key))
        if scalar is not None:
            result[key] = scalar
    return result


def _configuration_value(key: str, value: Any) -> Any:
    """Attach units where raw Ecobee scalar semantics otherwise invite mistakes."""

    if (
        key in _TENTHS_FAHRENHEIT_FIELDS
        and (parsed := _finite_float_or_none(value)) is not None
    ):
        return {"value": round(parsed / 10, 1), "unit": "°F"}
    if key in _SECOND_FIELDS:
        return {"value": value, "unit": "s"}
    if key in _MINUTE_FIELDS:
        return {"value": value, "unit": "min"}
    if key in _PERCENT_FIELDS:
        return {"value": value, "unit": "%"}
    return value


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value else None


def _finite_float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except OverflowError, TypeError, ValueError:
        return None
    return parsed if isfinite(parsed) else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except OverflowError, TypeError, ValueError:
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value == 1)

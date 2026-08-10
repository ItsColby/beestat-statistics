"""Pure Ecobee comfort-profile normalization for Beestat source rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any


@dataclass(frozen=True, slots=True)
class ScheduleProfile:
    """One normalized Ecobee comfort profile from the thermostat program."""

    ref: str
    name: str
    is_occupied: bool | None
    sensors: tuple[str, ...]
    heat_temperature: float | None = None
    cool_temperature: float | None = None
    heat_fan: str | None = None
    cool_fan: str | None = None
    is_optimized: bool | None = None
    vent: str | None = None
    ventilator_min_on_time: int | None = None


def schedule_profiles_by_ref(program: Any) -> dict[str, ScheduleProfile]:
    """Return one normalized profile per climate reference."""

    if not isinstance(program, Mapping) or not isinstance(
        climates := program.get("climates"), list
    ):
        return {}
    profiles: dict[str, ScheduleProfile] = {}
    for climate in climates:
        if not isinstance(climate, Mapping):
            continue
        ref = _text_or_none(climate.get("climateRef"))
        if ref is None:
            continue
        sensors = climate.get("sensors")
        sensor_names = (
            tuple(
                name
                for sensor in sensors
                if isinstance(sensor, Mapping)
                if (name := _text_or_none(sensor.get("name"))) is not None
            )
            if isinstance(sensors, list)
            else ()
        )
        profiles[ref] = ScheduleProfile(
            ref=ref,
            name=_text_or_none(climate.get("name")) or ref,
            is_occupied=_bool_or_none(climate.get("isOccupied")),
            sensors=sensor_names,
            heat_temperature=_finite_float_or_none(climate.get("heatTemp")),
            cool_temperature=_finite_float_or_none(climate.get("coolTemp")),
            heat_fan=_enum_or_none(climate.get("heatFan"), {"auto", "on"}),
            cool_fan=_enum_or_none(climate.get("coolFan"), {"auto", "on"}),
            is_optimized=_bool_or_none(climate.get("isOptimized")),
            vent=_enum_or_none(climate.get("vent"), {"auto", "on", "off"}),
            ventilator_min_on_time=_nonnegative_int_or_none(
                climate.get("ventilatorMinOnTime")
            ),
        )
    return profiles


def schedule_profile_payload(
    profile: ScheduleProfile,
    *,
    include_none: bool,
) -> dict[str, Any]:
    """Return the shared JSON-safe projection for one comfort profile."""

    payload: dict[str, Any] = {
        "ref": profile.ref,
        "name": profile.name,
        "is_occupied": profile.is_occupied,
        "sensors": list(profile.sensors),
        "heat_temperature": profile.heat_temperature,
        "cool_temperature": profile.cool_temperature,
        "heat_fan": profile.heat_fan,
        "cool_fan": profile.cool_fan,
        "is_optimized": profile.is_optimized,
        "vent": profile.vent,
        "ventilator_min_on_time": profile.ventilator_min_on_time,
    }
    if include_none:
        return payload
    return {
        key: value
        for key, value in payload.items()
        if value is not None and (key != "sensors" or value)
    }


def _text_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value else None


def _finite_float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except OverflowError, TypeError, ValueError:
        return None
    return parsed if isfinite(parsed) else None


def _nonnegative_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except OverflowError, TypeError, ValueError:
        return None
    return parsed if parsed >= 0 else None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in {"true", "1", "yes", "on"}:
            return True
        if value.lower() in {"false", "0", "no", "off"}:
            return False
    if value in (0, 1):
        return bool(value)
    return None


def _enum_or_none(value: Any, allowed: set[str]) -> str | None:
    text = _text_or_none(value)
    if text is None:
        return None
    normalized = text.lower()
    return normalized if normalized in allowed else None

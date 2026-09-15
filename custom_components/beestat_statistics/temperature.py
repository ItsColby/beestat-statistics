"""Quantity validation for absolute temperatures with known canonical units."""

from math import isfinite

_ABSOLUTE_ZERO = {"°C": -273.15, "°F": -459.67, "K": 0.0}
_ROUND_OFF_TOLERANCE = 1e-9


def absolute_temperature_value(
    value: float | None, unit: str, *, tenth_fahrenheit_source: bool = False
) -> float | None:
    """Reject impossible observations without clipping or limiting valid extremes."""

    minimum = _ABSOLUTE_ZERO.get(unit)
    # Qualified Ecobee/Beestat tenths-F sources can encode absolute zero as -459.7.
    source_tolerance = 0.05 if tenth_fahrenheit_source and unit == "°F" else 0.0
    if (
        value is None
        or not isfinite(value)
        or minimum is None
        or value < minimum - source_tolerance - _ROUND_OFF_TOLERANCE
    ):
        return None
    return value

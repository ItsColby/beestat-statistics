"""Shared Beestat/Ecobee alert classification helpers."""

from __future__ import annotations

from typing import Any

MAX_STATE_ALERT_EXAMPLES = 3
MAX_STATE_ALERT_VALUE_LENGTH = 96
_STATE_ALERT_FIELDS = ("code", "type", "severity", "timestamp")


def active_alert_examples(
    alerts: tuple[dict[str, Any], ...],
) -> list[dict[str, str]]:
    """Return bounded, identifier-free alert examples for entity state."""

    examples: list[dict[str, str]] = []
    for alert in alerts[:MAX_STATE_ALERT_EXAMPLES]:
        example = {"category": classify_active_alerts((alert,))}
        for field in _STATE_ALERT_FIELDS:
            if (value := _bounded_scalar(alert.get(field))) is not None:
                example[field] = value
        examples.append(example)
    return examples


def _bounded_scalar(value: Any) -> str | None:
    """Return one compact scalar without retaining arbitrary remote payloads."""

    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:MAX_STATE_ALERT_VALUE_LENGTH]


def classify_active_alerts(alerts: tuple[dict[str, Any], ...]) -> str:
    """Return a compact category for active thermostat alerts."""

    if not alerts:
        return "none"
    text = " ".join(
        str(alert.get(field) or "").lower()
        for alert in alerts
        for field in ("code", "type", "severity", "text")
    )
    equipment_terms = (
        "compressor",
        "cooling",
        "furnace",
        "heating",
        "high temp",
        "high temperature",
        "low temp",
        "low temperature",
        "not cooling",
        "not heating",
        "system fault",
        "temperature alert",
    )
    maintenance_terms = (
        "clean",
        "filter",
        "inspection",
        "inspect",
        "maintenance",
        "replace",
        "service",
        "tune",
    )
    if any(term in text for term in equipment_terms):
        return "equipment"
    if any(term in text for term in maintenance_terms):
        return "maintenance"
    return "unknown"

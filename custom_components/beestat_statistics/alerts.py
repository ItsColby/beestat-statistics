"""Shared Beestat/Ecobee alert classification helpers."""

from __future__ import annotations

from typing import Any

MAX_STATE_ALERT_EXAMPLES = 3


def active_alert_examples(
    alerts: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    """Return bounded alert examples for ordinary entity state."""

    return [dict(alert) for alert in alerts[:MAX_STATE_ALERT_EXAMPLES]]


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

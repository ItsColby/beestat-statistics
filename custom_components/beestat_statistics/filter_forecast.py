"""Shared HVAC filter forecast calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config_model import ConfiguredThermostat
    from .coordinator import ThermostatRuntimeSummary


@dataclass(frozen=True, slots=True)
class FilterForecast:
    """Derived replacement forecast for one thermostat filter."""

    changed_date: date | None
    changed_source: str | None
    runtime_hours: float | None
    recent_runtime_hours_per_day: float | None
    lifetime_runtime_hours: float
    max_age_days: int
    notice_days: int
    remaining_runtime_hours: float | None
    runtime_due_date: date | None
    max_age_due_date: date | None
    due_date: date | None
    days_remaining: int | None
    due: bool | None
    due_soon: bool | None


def build_filter_forecast(
    thermostat: ConfiguredThermostat,
    summary: ThermostatRuntimeSummary | None,
    *,
    today: date,
) -> FilterForecast:
    """Return the generic filter replacement forecast for a thermostat."""

    changed_date = summary.filter_changed_date if summary is not None else None
    changed_source = summary.filter_changed_source if summary is not None else None
    runtime_hours = summary.filter_runtime_hours if summary is not None else None
    if changed_date is not None and changed_date >= today:
        runtime_hours = 0.0
    recent_runtime_hours_per_day = (
        summary.recent_runtime_hours_per_day if summary is not None else None
    )
    remaining_runtime_hours = _remaining_runtime_hours(
        runtime_hours,
        thermostat.filter_lifetime_runtime_hours,
    )
    runtime_due_date = _runtime_due_date(
        today,
        remaining_runtime_hours,
        recent_runtime_hours_per_day,
    )
    max_age_due_date = (
        changed_date + timedelta(days=thermostat.filter_max_age_days)
        if changed_date is not None
        else None
    )
    due_date = _earliest_date(runtime_due_date, max_age_due_date)
    days_remaining = (due_date - today).days if due_date is not None else None
    due = days_remaining <= 0 if days_remaining is not None else None
    due_soon = (
        days_remaining <= thermostat.filter_notice_days
        if days_remaining is not None
        else None
    )
    return FilterForecast(
        changed_date=changed_date,
        changed_source=changed_source,
        runtime_hours=runtime_hours,
        recent_runtime_hours_per_day=recent_runtime_hours_per_day,
        lifetime_runtime_hours=thermostat.filter_lifetime_runtime_hours,
        max_age_days=thermostat.filter_max_age_days,
        notice_days=thermostat.filter_notice_days,
        remaining_runtime_hours=remaining_runtime_hours,
        runtime_due_date=runtime_due_date,
        max_age_due_date=max_age_due_date,
        due_date=due_date,
        days_remaining=days_remaining,
        due=due,
        due_soon=due_soon,
    )


def _remaining_runtime_hours(
    runtime_hours: float | None,
    lifetime_runtime_hours: float,
) -> float | None:
    if runtime_hours is None:
        return None
    return round(max(lifetime_runtime_hours - runtime_hours, 0.0), 1)


def _runtime_due_date(
    today: date,
    remaining_runtime_hours: float | None,
    recent_runtime_hours_per_day: float | None,
) -> date | None:
    if remaining_runtime_hours is None or recent_runtime_hours_per_day is None:
        return None
    if recent_runtime_hours_per_day <= 0:
        return None
    days_until_due = int(remaining_runtime_hours / recent_runtime_hours_per_day)
    return today + timedelta(days=max(days_until_due, 0))


def _earliest_date(*values: date | None) -> date | None:
    valid = [value for value in values if value is not None]
    return min(valid) if valid else None

"""Shared HVAC filter forecast calculations."""

from __future__ import annotations

from dataclasses import astuple, dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from math import ceil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config_model import ConfiguredThermostat
    from .coordinator import ThermostatRuntimeSummary

FILTER_FORECAST_QUALITY_FIELDS = frozenset(
    {
        "runtime_observed_hours",
        "runtime_coverage",
        "runtime_is_lower_bound",
        "runtime_source_data_end",
        "runtime_unknown_interval_minutes",
        "runtime_boundary_uncertainty_minutes",
        "runtime_boundary_precision_minutes",
        "runtime_threshold_reached",
        "boundary_status",
        "runtime_forecast_basis",
        "remaining_runtime_hours_is_upper_bound",
        "runtime_due_date_is_projection",
        "recent_runtime_window_start",
        "recent_runtime_window_end",
        "recent_runtime_complete_days",
        "recent_runtime_excluded_days",
    }
)


@dataclass(frozen=True, slots=True)
class FilterForecast:
    """Derived replacement forecast for one thermostat filter."""

    changed_date: date | None
    changed_at: datetime | None
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
    runtime_observed_hours: float | None = None
    runtime_coverage: str = "unknown"
    runtime_is_lower_bound: bool = True
    runtime_source_data_end: datetime | None = None
    runtime_unknown_interval_minutes: float | None = None
    runtime_boundary_uncertainty_minutes: float = 0.0
    runtime_boundary_precision_minutes: int = 5
    runtime_threshold_reached: bool | None = None
    boundary_status: str = "pending_data"
    runtime_forecast_basis: str = "unavailable"
    remaining_runtime_hours_is_upper_bound: bool = True
    runtime_due_date_is_projection: bool = True
    recent_runtime_window_start: date | None = None
    recent_runtime_window_end: date | None = None
    recent_runtime_complete_days: int = 0
    recent_runtime_excluded_days: int = 0


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
    observation = getattr(summary, "filter_runtime_observation", None)
    rate = getattr(summary, "recent_runtime_rate", None)
    recent_runtime_hours_per_day = (
        summary.recent_runtime_hours_per_day if summary is not None else None
    )
    threshold_reached = (
        observation.threshold_reached(thermostat.filter_lifetime_runtime_hours)
        if observation is not None
        else None
    )
    remaining_runtime_hours = _remaining_runtime_hours(
        runtime_hours,
        thermostat.filter_lifetime_runtime_hours,
    )
    if threshold_reached is True:
        remaining_runtime_hours = 0.0
    runtime_due_date = _runtime_due_date(
        today,
        remaining_runtime_hours,
        recent_runtime_hours_per_day,
        threshold_date=(
            getattr(summary, "filter_runtime_threshold_date", None)
            if summary is not None
            else None
        ),
    )
    max_age_due_date = (
        _date_after_days(changed_date, thermostat.filter_max_age_days)
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
    if observation is not None:
        calendar_due = max_age_due_date is not None and max_age_due_date <= today
        due = (
            True
            if calendar_due or threshold_reached is True
            else False
            if threshold_reached is False
            else None
        )
    lower_bound = observation.is_lower_bound if observation is not None else True
    forecast_basis = (
        "unavailable"
        if runtime_due_date is None
        else "observed_lower_bound_projection"
        if lower_bound
        else "complete_source_projection"
    )
    return FilterForecast(
        changed_date=changed_date,
        changed_at=thermostat.filter_changed_at,
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
        runtime_observed_hours=runtime_hours,
        runtime_coverage=observation.coverage if observation is not None else "unknown",
        runtime_is_lower_bound=lower_bound,
        runtime_source_data_end=observation.source_data_end
        if observation is not None
        else None,
        runtime_unknown_interval_minutes=(
            observation.unknown_interval_seconds / 60
            if observation is not None
            and observation.unknown_interval_seconds is not None
            else None
        ),
        runtime_boundary_uncertainty_minutes=(
            observation.boundary_uncertainty_seconds / 60
            if observation is not None
            else 0.0
        ),
        runtime_threshold_reached=threshold_reached,
        boundary_status=observation.boundary_status
        if observation is not None
        else "pending_data",
        runtime_forecast_basis=forecast_basis,
        remaining_runtime_hours_is_upper_bound=lower_bound,
        runtime_due_date_is_projection=threshold_reached is not True,
        recent_runtime_window_start=rate.window_start if rate is not None else None,
        recent_runtime_window_end=rate.window_end if rate is not None else None,
        recent_runtime_complete_days=rate.complete_days if rate is not None else 0,
        recent_runtime_excluded_days=rate.excluded_days if rate is not None else 0,
    )


def filter_forecast_revision(forecast: FilterForecast) -> str:
    """Return a stable content revision for one coherent forecast snapshot."""

    payload = "\x1f".join(_revision_value(value) for value in astuple(forecast))
    return sha256(payload.encode()).hexdigest()[:16]


def filter_forecast_quality_attributes(forecast: FilterForecast) -> dict[str, object]:
    """Use the same allow-listed quality projection on every native surface."""
    return {
        key: value.isoformat()
        if isinstance(value := getattr(forecast, key), (date, datetime))
        else value
        for key in sorted(FILTER_FORECAST_QUALITY_FIELDS)
    }


def _revision_value(value: object) -> str:
    """Return one unambiguous, stable revision component."""

    if value is None:
        return "<none>"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _remaining_runtime_hours(
    runtime_hours: float | None,
    lifetime_runtime_hours: float,
) -> float | None:
    if runtime_hours is None:
        return None
    return ceil(max(lifetime_runtime_hours - runtime_hours, 0.0) * 10) / 10


def _runtime_due_date(
    today: date,
    remaining_runtime_hours: float | None,
    recent_runtime_hours_per_day: float | None,
    *,
    threshold_date: date | None,
) -> date | None:
    if remaining_runtime_hours == 0:
        return threshold_date or today
    if remaining_runtime_hours is None or recent_runtime_hours_per_day is None:
        return None
    if recent_runtime_hours_per_day <= 0:
        return None
    try:
        days_until_due = int(remaining_runtime_hours / recent_runtime_hours_per_day)
    except OverflowError, ValueError:
        return None
    return _date_after_days(today, max(days_until_due, 0))


def _date_after_days(start: date, days: int) -> date | None:
    """Leave an unrepresentable forecast unknown while retaining other limits."""

    try:
        return start + timedelta(days=days)
    except OverflowError:
        return None


def _earliest_date(*values: date | None) -> date | None:
    valid = [value for value in values if value is not None]
    return min(valid) if valid else None

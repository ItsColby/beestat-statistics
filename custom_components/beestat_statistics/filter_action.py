"""Bounded provenance for the latest locally recorded filter boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, cast


@dataclass(frozen=True, slots=True)
class FilterChangeEvent:
    """One saved mutation, not a history or proof of physical work by itself."""

    action: Literal["replacement", "correction"]
    source: Literal["button", "service", "repair", "date", "configuration"]
    request_id: str
    prior_request_id: str | None
    prior_changed_at: str | None
    prior_changed_date: str | None
    changed_at: str | None
    changed_date: str
    recorded_at: str
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        """Return a detached public entity/action projection."""

        return asdict(self)


def parse_filter_change_event(value: Any) -> FilterChangeEvent | None:
    """Accept only a complete, bounded current-version provenance record."""

    if (
        not isinstance(value, Mapping)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not set(FilterChangeEvent.__dataclass_fields__).issubset(value)
    ):
        return None
    source = value.get("source")
    if not isinstance(source, str) or source not in {
        "button",
        "service",
        "repair",
        "date",
        "configuration",
    }:
        return None
    action: Literal["replacement", "correction"] = (
        "replacement" if source in {"button", "service"} else "correction"
    )
    if value.get("action") != action:
        return None
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        return None
    prior_request_id = value.get("prior_request_id")
    if prior_request_id is not None and (
        not isinstance(prior_request_id, str) or not 1 <= len(prior_request_id) <= 128
    ):
        return None
    try:
        changed_date = date.fromisoformat(value["changed_date"]).isoformat()
        prior_date = value.get("prior_changed_date")
        if prior_date is not None:
            prior_date = date.fromisoformat(prior_date).isoformat()
        changed_at = _timestamp(value.get("changed_at"))
        prior_at = _timestamp(value.get("prior_changed_at"))
        recorded_at = _timestamp(value["recorded_at"])
    except KeyError, TypeError, ValueError, OverflowError:
        return None
    if recorded_at is None or (
        source not in {"date", "configuration"} and changed_at is None
    ):
        return None
    return FilterChangeEvent(
        action=action,
        source=cast(
            Literal["button", "service", "repair", "date", "configuration"], source
        ),
        request_id=request_id,
        prior_request_id=prior_request_id,
        prior_changed_at=prior_at,
        prior_changed_date=prior_date,
        changed_at=changed_at,
        changed_date=changed_date,
        recorded_at=recorded_at,
    )


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC).isoformat()

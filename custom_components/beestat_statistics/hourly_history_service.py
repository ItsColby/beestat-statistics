"""Native action schemas for the additive qualified-history contract."""

from __future__ import annotations

from typing import Any

import probatio
from homeassistant.helpers import config_validation as cv

from .hourly_history_contract import (
    DAILY_POLICY,
    MAX_BUCKETS,
    MAX_QUANTITIES,
    MAX_SOURCE_CHUNKS,
    require_digest,
)


def integer(value: Any) -> int:
    if type(value) is not int:
        raise probatio.Invalid("An integer is required")
    return value


def distinct(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise probatio.Invalid("Values must be distinct")
    return values


_QUANTITIES = probatio.All(
    [cv.string], probatio.Length(min=1, max=MAX_QUANTITIES), distinct
)
_REVISION = probatio.All(integer, probatio.Range(min=0))
_SHORT_TEXT = probatio.All(cv.string, probatio.Length(min=1, max=256))
_CONSUMERS = probatio.Schema(
    {
        probatio.Required("contract_version"): 3,
        probatio.Required("consumers"): probatio.All(
            [
                {
                    probatio.Required("consumer_id"): _SHORT_TEXT,
                    probatio.Required("version"): _SHORT_TEXT,
                    probatio.Required("history_contract"): 3,
                    probatio.Required("daily_policy"): DAILY_POLICY,
                }
            ],
            probatio.Length(min=1, max=16),
        ),
    }
)

STAGE_HISTORY_SCHEMA = probatio.Schema(
    {
        probatio.Required("config_entry_id"): cv.string,
        probatio.Required("file_id"): probatio.All(
            cv.string, probatio.Length(min=1, max=128)
        ),
        probatio.Required("sha256"): require_digest,
        probatio.Required("manifest"): dict,
    }
)

PLAN_HISTORY_SCHEMA = probatio.Schema(
    {
        probatio.Required("config_entry_id"): cv.string,
        probatio.Required("source_ids"): probatio.All(
            [require_digest], probatio.Length(min=1, max=MAX_SOURCE_CHUNKS), distinct
        ),
        probatio.Required("quantity_ids"): _QUANTITIES,
        probatio.Required("start"): cv.datetime,
        probatio.Required("end"): cv.datetime,
        probatio.Required("archive_policy"): probatio.In(
            ("reject", "before_first_provider")
        ),
        probatio.Required("daily_policy"): DAILY_POLICY,
        probatio.Required("operation_id"): _SHORT_TEXT,
        probatio.Required("expected_revision"): _REVISION,
        probatio.Required("consumer_contract"): _CONSUMERS,
        probatio.Required("recovery_reference"): _SHORT_TEXT,
        probatio.Optional("evaluated_at"): cv.datetime,
        probatio.Optional("detail_offset", default=0): _REVISION,
        probatio.Optional("detail_limit", default=100): probatio.All(
            integer, probatio.Range(min=1, max=100)
        ),
    }
)

APPLY_HISTORY_SCHEMA = probatio.Schema(
    {
        probatio.Required("config_entry_id"): cv.string,
        probatio.Required("plan_digest"): require_digest,
        probatio.Required("plan"): PLAN_HISTORY_SCHEMA,
    }
)

COVERAGE_HISTORY_SCHEMA = probatio.Schema(
    {
        probatio.Required("config_entry_id"): cv.string,
        probatio.Required("contract_version"): 3,
        probatio.Required("quantity_ids"): _QUANTITIES,
        probatio.Required("start"): cv.datetime,
        probatio.Required("end"): cv.datetime,
        probatio.Optional("period", default="hour"): probatio.In(("hour", "day")),
        probatio.Optional("daily_policy", default=DAILY_POLICY): DAILY_POLICY,
        probatio.Optional("page_size", default=MAX_BUCKETS): probatio.All(
            integer, probatio.Range(min=1, max=MAX_BUCKETS)
        ),
        probatio.Optional("offset", default=0): _REVISION,
        probatio.Optional("view_token"): require_digest,
        probatio.Optional("evaluated_at"): cv.datetime,
    }
)

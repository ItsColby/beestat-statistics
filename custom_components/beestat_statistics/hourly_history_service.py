"""Native action schemas for the additive qualified-history contract."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
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
        raise vol.Invalid("An integer is required")
    return value


def distinct(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise vol.Invalid("Values must be distinct")
    return values


_QUANTITIES = vol.All([cv.string], vol.Length(min=1, max=MAX_QUANTITIES), distinct)
_REVISION = vol.All(integer, vol.Range(min=0))
_SHORT_TEXT = vol.All(cv.string, vol.Length(min=1, max=256))
_CONSUMERS = vol.Schema(
    {
        vol.Required("contract_version"): 3,
        vol.Required("consumers"): vol.All(
            [
                {
                    vol.Required("consumer_id"): _SHORT_TEXT,
                    vol.Required("version"): _SHORT_TEXT,
                    vol.Required("history_contract"): 3,
                    vol.Required("daily_policy"): DAILY_POLICY,
                }
            ],
            vol.Length(min=1, max=16),
        ),
    }
)

STAGE_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("file_id"): vol.All(cv.string, vol.Length(min=1, max=128)),
        vol.Required("sha256"): require_digest,
        vol.Required("manifest"): dict,
    }
)

PLAN_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("source_ids"): vol.All(
            [require_digest], vol.Length(min=1, max=MAX_SOURCE_CHUNKS), distinct
        ),
        vol.Required("quantity_ids"): _QUANTITIES,
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
        vol.Required("archive_policy"): vol.In(("reject", "before_first_provider")),
        vol.Required("daily_policy"): DAILY_POLICY,
        vol.Required("operation_id"): _SHORT_TEXT,
        vol.Required("expected_revision"): _REVISION,
        vol.Required("consumer_contract"): _CONSUMERS,
        vol.Required("recovery_reference"): _SHORT_TEXT,
        vol.Optional("evaluated_at"): cv.datetime,
        vol.Optional("detail_offset", default=0): _REVISION,
        vol.Optional("detail_limit", default=100): vol.All(
            integer, vol.Range(min=1, max=100)
        ),
    }
)

APPLY_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("plan_digest"): require_digest,
        vol.Required("plan"): PLAN_HISTORY_SCHEMA,
    }
)

COVERAGE_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("contract_version"): 3,
        vol.Required("quantity_ids"): _QUANTITIES,
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
        vol.Optional("period", default="hour"): vol.In(("hour", "day")),
        vol.Optional("daily_policy", default=DAILY_POLICY): DAILY_POLICY,
        vol.Optional("page_size", default=MAX_BUCKETS): vol.All(
            integer, vol.Range(min=1, max=MAX_BUCKETS)
        ),
        vol.Optional("offset", default=0): _REVISION,
        vol.Optional("view_token"): require_digest,
        vol.Optional("evaluated_at"): cv.datetime,
    }
)

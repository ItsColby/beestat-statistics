"""Contract-v3 history response key sets, as docs/hourly-history-v3.md lists them.

Consumers read these keys by name and validate them, so contract_version 3 must
not gain or lose one silently. Optional keys (a blocked descriptor's
``blocked_reason``, a day bucket's ``legacy_reason``) are added per case.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from unittest import TestCase

RESPONSE = frozenset(
    {
        "contract_version",
        "identity",
        "config_revision",
        "root_revision",
        "source_revision",
        "coverage_revision",
        "method_version",
        "daily_policy",
        "period",
        "start",
        "end",
        "evaluated_at",
        "timezone",
        "native_verification",
        "operation",
        "view_token",
        "pagination",
        "series",
    }
)
UNSELECTED_OPERATION = frozenset({"status", "root_revision", "has_pending"})
OPERATION = UNSELECTED_OPERATION | {
    "contract_version",
    "operation_id",
    "plan_digest",
    "cursor",
    "batch_count",
    "verified_batches",
    "remaining_batches",
    "error",
}
PAGINATION = frozenset({"offset", "next_offset", "has_more", "total_buckets"})
SERIES = frozenset({"descriptor", "buckets", "summary"})
DESCRIPTOR = frozenset(
    {
        "quantity_id",
        "thermostat_id",
        "sensor_id",
        "quantity",
        "kind",
        "logical_unit",
        "method_version",
        "statistic_id",
        "legacy_statistic_ids",
        "representation",
        "admission",
        "writer_status",
    }
)
REPRESENTATION = frozenset(
    {
        "kind",
        "native_field",
        "unit_of_measurement",
        "unit_class",
        "logical_multiplier",
        "v2_statistic_id",
    }
)
_SUMMARY = frozenset(
    {
        "scope",
        "start",
        "end",
        "verified_hours",
        "expected_hours",
        "closed_hours",
        "eligible_hours",
        "eligible_buckets",
        "legacy_days",
        "coverage_reasons",
    }
)
AMOUNT_SUMMARY = _SUMMARY | {"observed_amount", "complete_total", "eligible_amount"}
MEASUREMENT_SUMMARY = _SUMMARY | {
    "observed_mean",
    "observed_min",
    "observed_max",
    "summary_basis",
}
BUCKET = frozenset(
    {
        "start",
        "end",
        "value",
        "min",
        "max",
        "reason",
        "failure_reason",
        "valid_slots",
        "expected_slots",
        "verified_hours",
        "expected_hours",
        "closed_hours",
        "observed_amount",
        "complete_total",
        "source_basis",
        "confidence",
        "source_ids",
        "method_basis",
        "eligible_intervals",
    }
)
DAY_BUCKET = BUCKET | {
    "calendar_start",
    "calendar_end",
    "calendar_hours",
    "point_observed_amount",
    "observed_intervals",
    "coverage_reasons",
}


# "Discover identities" lists what get_configuration's history_v3 contains,
# not every key it carries, so only a dropped key fails.
CONFIGURATION_REQUIRED = frozenset(
    {
        "contract_version",
        "method_version",
        "daily_policy",
        "services",
        "limits",
        "identity",
        "root_revision",
        "source_revision",
        "coverage_revision",
        "operation",
        "quantities",
    }
)


def changed(name: str, payload: str = "history response") -> str:
    """The failure message for a consumer-visible key set change."""
    return (
        f"The {name} key set of the contract-v3 {payload} changed. This is a "
        "consumer-visible contract change: bump contract_version, or update "
        "docs/hourly-history-v3.md and every consumer that validates the "
        "response first."
    )


def assert_key_sets(
    test: TestCase,
    cases: Iterable[tuple[str, Iterable[Mapping[str, Any]], frozenset[str]]],
) -> None:
    """Assert each object's exact key set; an empty case fails, not passes."""
    for name, produced, keys in cases:
        items = list(produced)
        test.assertTrue(items, f"No {name} object was produced to check.")
        for index, item in enumerate(items):
            with test.subTest(object=name, index=index):
                test.assertEqual(set(item), keys, changed(name))

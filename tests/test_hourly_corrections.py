"""Reviewed corrections retain source evidence and fail closed on changes."""

from copy import deepcopy

import pytest

from custom_components.beestat_statistics.hourly_corrections import (
    project,
    raw_witness,
    resolution_witness,
    validate_document,
    validate_points,
)
from custom_components.beestat_statistics.hourly_history_contract import digest
from custom_components.beestat_statistics.hourly_history_values import (
    build_history_series,
)
from tests.test_hourly_history_values import HourlyHistoryValuesTest


@pytest.fixture
def scenario():
    helper = HourlyHistoryValuesTest()
    helper.setUp()
    series, identity = helper.build()
    items = build_history_series(series, identity)
    item = helper.item(items, "indoor_humidity")
    descriptor = item.descriptor
    qid = descriptor["quantity_id"]
    rows = helper.thermostat_rows()
    source = {
        "manifest": {
            "resource": "runtime_thermostat",
            "resource_id": 1,
            "source_kind": "provider",
        },
        "rows": rows,
    }
    resource = {
        "rows": rows,
        "slots": {
            row["timestamp"]: {
                "basis": "provider",
                "source_ids": ["a"],
                "confidence": [],
            }
            for row in rows
        },
        "first_provider": helper.start.isoformat(),
        "conflict_hours": [],
        "blocked_windows": [],
    }
    merged = {"resources": {"runtime_thermostat:1": resource}}
    ref = {"sha256": "a" * 64, "locator": "block 1"}
    entry = {
        "descriptor": descriptor,
        "start": helper.start.isoformat(),
        "action": "replace",
        "dependency_start": helper.start.isoformat(),
        "dependency_end": helper.end.isoformat(),
        "raw_witness": raw_witness(merged, descriptor, helper.start, helper.end),
        "resolution_witness": resolution_witness(
            merged, [source], descriptor, helper.start, helper.end
        ),
        "qualification": [ref],
        "points": helper.thermostat_rows(indoor_humidity=55),
        "predecessor": None,
        "reason": None,
        "interpretation": {
            "timezone": "UTC",
            "fold": None,
            "unit_conversion": "percent unchanged",
        },
    }
    binding = {"timezone": "UTC", "method_version": descriptor["method_version"]}
    doc = {
        "format": "reviewed_quantity_correction_v1",
        "binding": binding,
        "converter_version": "1",
        "originals": [ref],
        "entries": [entry],
    }
    context = {
        "config": helper.config,
        "identity": identity,
        "evaluated_at": helper.end.isoformat(),
    }
    return helper, item, qid, merged, [source], doc, binding, context


def apply(scenario):
    _, item, qid, merged, sources, doc, binding, context = scenario
    validate_document(doc, binding, {qid: item.descriptor})
    return project(
        {qid: item},
        merged,
        sources,
        [{"correction_id": digest(doc), "document": doc}],
        context,
    )


def test_replacement_and_equal_reacquisition(scenario):
    helper, _, qid, merged, sources, doc, _, _ = scenario
    before = deepcopy(merged)
    output, resources = apply(scenario)
    assert output[qid].hours[0].values["mean"] == 55
    assert resources[qid]["correction_ids"][helper.start.isoformat()] == digest(doc)
    assert merged == before
    second = deepcopy(sources[0])
    for row in second["rows"]:
        row["id"] = 999
    sources.append(second)
    assert apply(scenario)[0][qid].hours[0].values["mean"] == 55


@pytest.mark.parametrize(
    "change", ["null", "deleted", "unknown", "absence", "conflict", "unplaceable"]
)
def test_changed_evidence_holds_correction(scenario, change):
    helper, _, qid, merged, _, _, _, _ = scenario
    resource = merged["resources"]["runtime_thermostat:1"]
    if change == "null":
        resource["rows"][0]["indoor_humidity"] = None
    elif change == "deleted":
        resource["rows"][0]["deleted"] = True
    elif change == "unknown":
        resource["rows"][0]["new_field"] = 1
    elif change == "absence":
        resource["rows"].pop()
    elif change == "conflict":
        resource["conflict_hours"].append(helper.start.isoformat())
    else:
        resource["blocked_windows"].append(
            {
                "start": helper.start.isoformat(),
                "end": helper.end.isoformat(),
                "reason": "unplaceable_timestamp",
            }
        )
    hour = apply(scenario)[0][qid].hours[0]
    assert hour.reason == "source_timestamp_unplaced"
    assert (hour.values, hour.valid_slots, hour.invalid_slots, hour.missing_slots) == (
        None,
        0,
        12,
        0,
    )


def test_hold_and_strict_contract(scenario):
    _, _, qid, _, _, doc, _, _ = scenario
    entry = doc["entries"][0]
    entry.update(action="hold", points=[], reason="ambiguous occurrence")
    assert apply(scenario)[0][qid].hours[0].reason == "source_timestamp_unplaced"
    doc["unexpected"] = True
    with pytest.raises(ValueError):
        apply(scenario)


def test_invalid_samples_cannot_be_staged(scenario):
    _, _, _, _, _, doc, _, context = scenario
    doc["entries"][0]["points"][0]["indoor_humidity"] = None
    with pytest.raises(ValueError, match="points_unqualified"):
        validate_points(doc, context)


@pytest.mark.parametrize(
    "quantity,expected", [("fan_runtime_hours", 50), ("heating_degree_days", 25)]
)
def test_runtime_and_degree_day_samples_use_native_builder(
    scenario, quantity, expected
):
    helper, _, _, merged, sources, doc, _, context = scenario
    series, identity = helper.build()
    item = helper.item(build_history_series(series, identity), quantity)
    doc["entries"][0]["descriptor"] = item.descriptor
    doc["entries"][0]["points"] = helper.thermostat_rows(outdoor_temperature=40)
    validate_points(doc, context)
    qid = item.descriptor["quantity_id"]
    output, _ = project(
        {qid: item},
        merged,
        sources,
        [{"correction_id": digest(doc), "document": doc}],
        context,
    )
    assert output[qid].hours[0].values["mean"] == pytest.approx(expected)


@pytest.mark.parametrize("change", ["timezone", "fold", "conversion", "start"])
def test_interpretation_and_canonical_start_are_required(scenario, change):
    _, item, qid, _, _, doc, binding, _ = scenario
    entry = doc["entries"][0]
    if change == "timezone":
        entry["interpretation"]["timezone"] = "America/New_York"
    elif change == "fold":
        entry["interpretation"]["fold"] = True
    elif change == "conversion":
        entry["interpretation"]["unit_conversion"] = ""
    else:
        entry["start"] = entry["start"].replace("+00:00", "Z")
    with pytest.raises(ValueError):
        validate_document(doc, binding, {qid: item.descriptor})

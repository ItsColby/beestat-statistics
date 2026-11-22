"""Reviewed evidence uses the real plan, journal, query fence and raw baseline."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest

from custom_components.beestat_statistics import hourly_corrections as correction
from custom_components.beestat_statistics import hourly_history_root as history_root
from custom_components.beestat_statistics import (
    hourly_history_source_view as source_view,
)
from custom_components.beestat_statistics.config_model import ConfiguredSensor
from custom_components.beestat_statistics.hourly_history_contract import digest
from custom_components.beestat_statistics.hourly_history_query import history_response
from custom_components.beestat_statistics.hourly_import_core import HourlyImportError
from tests import test_hourly_history_writer as fixture


@pytest.fixture
async def adopted():
    helper = fixture.HistoryWriterTest()
    helper.setUp()
    await helper.accept()
    await helper.complete()
    try:
        yield helper
    finally:
        await helper.asyncTearDown()


async def prepare(
    helper, *, actions=("replace", "hold"), predecessor=None, source_ids=None
):
    request = await helper.request(
        expected_revision=helper.store.value["revision"], operation_id="reviewed-repair"
    )
    if source_ids is not None:
        request["source_ids"] = source_ids
    writer = helper.writer._history_writer()
    sources, merged = await writer.source_view(
        request, helper.store.value["history"], helper.context
    )
    descriptor = helper.context["descriptors"][0]
    original = b"synthetic UTC-qualified original; two repeated local hours"
    reference = {"sha256": sha256(original).hexdigest(), "locator": "rows 1-24"}
    entries = []
    for number, action in enumerate(actions):
        start = fixture.START + number * fixture.HOUR
        entries.append(
            {
                "descriptor": descriptor,
                "start": start.isoformat(),
                "action": action,
                "dependency_start": request["start"],
                "dependency_end": request["end"],
                "raw_witness": correction.raw_witness(
                    merged, descriptor, request["start"], request["end"]
                ),
                "resolution_witness": correction.resolution_witness(
                    merged, sources, descriptor, request["start"], request["end"]
                ),
                "qualification": [reference],
                "points": [
                    {
                        "timestamp": (start + timedelta(minutes=5 * index)).isoformat(),
                        "thermostat_id": 1,
                        "fan": 75,
                    }
                    for index in range(12)
                ]
                if action == "replace"
                else [],
                "predecessor": predecessor[number]
                if isinstance(predecessor, tuple)
                else predecessor,
                "reason": "UTC occurrence is unqualified" if action == "hold" else None,
                "interpretation": {
                    "timezone": helper.context["timezone"],
                    "fold": None,
                    "unit_conversion": "Runtime seconds per five-minute interval; native mean percent",
                },
            }
        )
    document = {
        "format": "reviewed_quantity_correction_v1",
        "binding": writer.binding(helper.context),
        "converter_version": "fixture-1",
        "originals": [reference],
        "entries": entries,
    }
    bundle = json.dumps(
        {
            "document": document,
            "originals": [
                {
                    "sha256": reference["sha256"],
                    "content_base64": base64.b64encode(original).decode(),
                }
            ],
        }
    ).encode()
    receipt = await writer.stage_correction(
        bundle, sha256(bundle).hexdigest(), helper.context
    )
    request["correction_ids"] = [receipt["correction_id"]]
    return request, receipt, document


async def apply(helper, request):
    plan = await helper.writer.async_plan_history(request, context=helper.context)
    assert plan["status"] == "planned", plan
    await helper.writer.async_accept_history(
        {**plan["request"], "plan_digest": plan["plan_digest"]}, context=helper.context
    )
    return plan


async def test_stage_plan_accept_restart_holds_and_raw_replay(adopted):
    helper = adopted
    before_root = deepcopy(helper.store.value)
    before_rows = deepcopy(helper.recorder.rows)
    raw_before = await helper.writer.async_history_source_points(
        {
            "start": fixture.START.isoformat(),
            "end": (fixture.START + 2 * fixture.HOUR).isoformat(),
        },
        context=helper.context,
    )
    request, receipt, _ = await prepare(helper)
    assert receipt["holds"] == 1
    assert helper.store.value == before_root
    assert helper.recorder.rows == before_rows
    plan = await apply(helper, request)
    assert (
        sum(
            batch["dispositions"].get("source_timestamp_unplaced", 0)
            for batch in plan["batches"]
        )
        == 1
    )
    assert helper.store.value["version"] == 4
    assert helper.entry.data["hourly_import_contract"]["version"] == 4
    unfinished = await helper.writer._history_writer().unfinished_windows(
        helper.store.value, helper.context
    )
    assert fixture.KEY in unfinished
    helper.writer = helper.fresh()
    await helper.writer._load()
    await helper.complete()
    assert helper.writer.history_status()["status"] == "completed"
    raw_after = await helper.writer.async_history_source_points(
        {"start": request["start"], "end": request["end"]}, context=helper.context
    )
    assert raw_before["resources"] == raw_after["resources"]
    rows = helper.recorder.rows[fixture.NATIVE]
    assert rows[fixture.START].mean == 25
    assert rows[fixture.START + fixture.HOUR].mean is None
    index = helper.store.value["history"]["corrections"]
    assert len(index[fixture.KEY]) == 2
    for month in ("2026-01", "2026-02"):
        proof = await helper.writer._history_writer().proof(
            helper.store.value["history"]["selections"][fixture.KEY],
            month,
            helper.context,
        )
        assert proof["hours"][0]["correction_id"] == receipt["correction_id"]
    # An unchanged authenticated acquisition generates no inverse correction delta.
    delta = await helper.delta_request(fan=150, acquisition="equal-after-repair")
    assert delta["source_ids"] == []
    replay = {
        **request,
        "correction_ids": [],
        "operation_id": "replay",
        "expected_revision": helper.store.value["revision"],
    }
    manifest, batches, _ = await helper.writer._history_writer().plan(
        replay, helper.context, changed_only=True
    )
    assert not batches
    assert manifest["corrections"] == index


async def test_dependency_change_fails_closed_and_partial_plan_cannot_admit_it(adopted):
    helper = adopted
    request, _, _ = await prepare(helper)
    await apply(helper, request)
    await helper.complete()
    fresh = await helper.stage(fan=200, acquisition="new-conflicting-provider")
    changed = {
        **request,
        "source_ids": [fresh],
        "correction_ids": [],
        "expected_revision": helper.store.value["revision"],
        "operation_id": "changed-evidence",
    }
    manifest, batches, _ = await helper.writer._history_writer().plan(
        changed, helper.context
    )
    assert (
        sum(
            batch["dispositions"].get("source_timestamp_unplaced", 0)
            for batch in batches
        )
        == 2
    )
    assert all(row["mean"] is None for batch in batches for row in batch["rows"])
    partial = {**changed, "start": (fixture.START + fixture.HOUR).isoformat()}
    with pytest.raises(HourlyImportError, match="correction_replan_required"):
        await helper.writer._history_writer().plan(partial, helper.context)
    await helper.writer.async_accept_history(
        {**changed, "plan_digest": digest(manifest)}, context=helper.context
    )
    helper.writer = helper.fresh()
    await helper.writer._load()
    assert fixture.KEY in await helper.writer._history_writer().unfinished_windows(
        helper.store.value, helper.context
    )
    await helper.complete()
    assert all(
        row.mean is None for row in helper.recorder.rows[fixture.NATIVE].values()
    )


async def test_active_correction_survives_unrelated_sensor_rename(adopted):
    helper = adopted
    sensor = ConfiguredSensor(
        sensor_id=2,
        slug="room",
        name="Old Room",
        thermostat_id=1,
        thermostat_slug="zone",
        include_temperature=True,
        include_air_quality=False,
        include_co2=False,
        include_voc=False,
    )
    config = replace(helper.context["config"], sensors=(sensor,))
    helper.context["config"] = config
    request, receipt, document = await prepare(helper)
    await apply(helper, request)
    await helper.complete()
    index = deepcopy(helper.store.value["history"]["corrections"])
    rows = deepcopy(helper.recorder.rows)
    helper.context["config"] = replace(
        config, sensors=(replace(sensor, name="New Room"),)
    )

    writer = helper.writer._history_writer()
    assert writer.binding(helper.context) != document["binding"]
    replay = {
        **request,
        "operation_id": "after-sensor-rename",
        "expected_revision": helper.store.value["revision"],
    }
    with pytest.raises(ValueError, match="history_correction_document_invalid"):
        await writer.plan(replay, helper.context)

    result = await helper.writer.async_refresh_history(
        {"start": request["start"], "end": request["end"], "source_ids": []},
        context=helper.context,
    )
    assert result == {"status": "unchanged", "changed_rows": 0}
    assert helper.store.value["history"]["corrections"] == index
    assert helper.recorder.rows == rows
    assert (
        await writer.read_object("correction", receipt["correction_id"], helper.context)
        == document
    )

    changed = deepcopy(helper.context["descriptors"])
    changed[0]["logical_unit"] = "incompatible"
    with pytest.raises(ValueError, match="history_correction_descriptor_changed"):
        correction.validate_document(
            document,
            writer.binding(helper.context),
            {item["quantity_id"]: item for item in changed},
            active=True,
        )

    later = fixture.START + 2 * fixture.HOUR
    incoming = await helper.stage(start=later, hours=1, acquisition="new-hour")
    result = await helper.writer.async_refresh_history(
        {
            "start": later.isoformat(),
            "end": (later + fixture.HOUR).isoformat(),
            "source_ids": [incoming],
        },
        context=helper.context,
    )
    assert result["status"] == "accepted", result
    await helper.complete()
    assert helper.recorder.rows[fixture.NATIVE][later].mean == 50
    assert helper.store.value["history"]["corrections"] == index


async def test_stale_plan_conflicting_corrections_and_explicit_withdrawal(adopted):
    helper = adopted
    request, receipt, _ = await prepare(helper)
    await apply(helper, request)
    await helper.complete()
    conflicting, _, _ = await prepare(helper, actions=("hold", "hold"))
    with pytest.raises(HourlyImportError, match="predecessor_changed"):
        await helper.writer._history_writer().plan(conflicting, helper.context)
    withdrawal, _, _ = await prepare(
        helper, actions=("withdraw", "withdraw"), predecessor=receipt["correction_id"]
    )
    withdrawal["operation_id"] = "explicit-withdrawal"
    await apply(helper, withdrawal)
    await helper.complete()
    assert helper.store.value["history"]["corrections"] == {}
    assert helper.store.value["version"] == 4
    assert all(row.mean == 50 for row in helper.recorder.rows[fixture.NATIVE].values())
    downgraded = deepcopy(helper.store.value)
    downgraded["version"] = 3
    with pytest.raises(HourlyImportError, match="unsupported"):
        history_root.validate_root(helper.writer, history_root.sealed(downgraded))


async def test_earlier_provider_anchor_and_policy_changes_require_full_replan(adopted):
    helper = adopted
    request, _, _ = await prepare(helper)
    await apply(helper, request)
    await helper.complete()
    root = deepcopy(helper.store.value)
    earlier = await helper.stage(
        start=fixture.START - 3 * fixture.HOUR, hours=1, acquisition="earlier-provider"
    )
    partial = {
        **request,
        "correction_ids": [],
        "source_ids": [earlier],
        "start": (fixture.START - 3 * fixture.HOUR).isoformat(),
        "end": (fixture.START - 2 * fixture.HOUR).isoformat(),
        "operation_id": "earlier-anchor",
        "expected_revision": root["revision"],
    }
    with pytest.raises(HourlyImportError, match="correction_replan_required"):
        await helper.writer._history_writer().plan(partial, helper.context)
    partial = {
        **request,
        "correction_ids": [],
        "start": (fixture.START + fixture.HOUR).isoformat(),
        "archive_policy": "provider_gaps",
        "expected_revision": root["revision"],
    }
    with pytest.raises(HourlyImportError, match="correction_replan_required"):
        await helper.writer._history_writer().plan(partial, helper.context)
    assert helper.store.value == root
    helper.writer = helper.fresh()
    await helper.writer._load()
    assert helper.writer.history_status()["status"] == "completed"


async def test_first_activation_protects_dependencies_across_restart(
    adopted, monkeypatch
):
    helper = adopted
    request, _, _ = await prepare(helper)
    original = source_view.source_redundancy
    protected = []

    def assess(sources, **kwargs):
        protected.append(set(kwargs["protected"]))
        return original(sources, **kwargs)

    monkeypatch.setattr(source_view, "source_redundancy", assess)
    await apply(helper, request)
    assert protected
    # Every existing acquisition in the reviewed dependency remains protected,
    # including cataloged candidates that are not a newly requested source.
    all_ids = {
        item["source_id"]
        for item in (
            await helper.writer._history_writer().load_sources(
                request["source_ids"],
                fixture.START,
                fixture.START + 2 * fixture.HOUR,
                helper.context,
            )
        )[1]
    }
    assert all(all_ids <= ids for ids in protected)
    await helper.complete()
    helper.writer = helper.fresh()
    await helper.writer._load()
    request = {
        **request,
        "correction_ids": [],
        "operation_id": "after-restart",
        "expected_revision": helper.store.value["revision"],
    }
    manifest, batches, _ = await helper.writer._history_writer().plan(
        request, helper.context, changed_only=True
    )
    assert not batches
    assert manifest["corrections"] == helper.store.value["history"]["corrections"]


@pytest.mark.parametrize("second_action", ["replace", "hold"])
async def test_routine_stale_correction_without_conflict_preserves_native_rows(
    adopted, second_action
):
    helper = adopted
    request, _, _ = await prepare(helper, actions=("replace", second_action))
    await apply(helper, request)
    await helper.complete()
    incoming = await helper.stage(fan=300, acquisition="changed-reviewed-dependency")
    window = {"start": request["start"], "end": request["end"]}
    before = deepcopy(helper.recorder.rows)

    result = await helper.writer.async_refresh_history(
        {**window, "source_ids": [incoming]}, context=helper.context
    )

    assert result["status"] == "blocked", result
    assert result["error"] == "source_timestamp_unplaced"
    assert not result["has_pending"]
    assert helper.recorder.rows == before

    helper.writer = helper.fresh()
    retained = await helper.writer.async_refresh_history(
        {**window, "source_ids": []}, context=helper.context
    )
    assert retained["status"] == "blocked", retained
    assert retained["error"] == "source_timestamp_unplaced"
    assert retained["root_revision"] == result["root_revision"]
    assert helper.recorder.rows == before

    query = {**window, "quantity_ids": [fixture.KEY], "contract_version": 3}
    material = await helper.writer.async_history_material(query, context=helper.context)
    holds = material["source_holds"][fixture.KEY]
    expected_starts = [fixture.START.isoformat()]
    if second_action == "replace":
        expected_starts.append((fixture.START + fixture.HOUR).isoformat())
    assert [hold["start"] for hold in holds] == expected_starts
    assert {hold["reason"] for hold in holds} == {"source_timestamp_unplaced"}


async def test_routine_conflict_and_stale_correction_fence_survives_restart(adopted):
    helper = adopted
    request, receipt, _ = await prepare(helper, actions=("replace",))
    await apply(helper, request)
    await helper.complete()
    incoming = await helper.stage(fan=300, acquisition="unordered-conflict")
    window = {"start": request["start"], "end": request["end"]}
    before = deepcopy(helper.recorder.rows)
    result = await helper.writer.async_refresh_history(
        {**window, "source_ids": [incoming]}, context=helper.context
    )
    assert result["error"] == "source_conflict"
    assert helper.recorder.rows == before
    assert not result["has_pending"]

    helper.writer = helper.fresh()
    await helper.writer._load()
    query = {**window, "quantity_ids": [fixture.KEY], "contract_version": 3}
    material = await helper.writer.async_history_material(query, context=helper.context)
    holds = material["source_holds"][fixture.KEY]
    assert [(hold["start"], hold["reason"]) for hold in holds] == [
        (fixture.START.isoformat(), "source_timestamp_unplaced"),
        ((fixture.START + fixture.HOUR).isoformat(), "source_conflict"),
    ]
    buckets = history_response(query, material, helper.context)["series"][0]["buckets"]
    assert [bucket["value"] for bucket in buckets] == [None, None]
    assert [bucket["failure_reason"] for bucket in buckets] == [
        "source_timestamp_unplaced",
        "source_conflict",
    ]

    reviewed, _, _ = await prepare(
        helper,
        actions=("replace", "hold"),
        predecessor=(receipt["correction_id"], None),
        source_ids=[incoming],
    )
    reviewed["operation_id"] = "reviewed-recovery"
    await apply(helper, reviewed)
    await helper.complete()
    helper.writer = helper.fresh()
    material = await helper.writer.async_history_material(query, context=helper.context)
    assert not material["source_holds"][fixture.KEY]
    buckets = history_response(query, material, helper.context)["series"][0]["buckets"]
    assert [bucket["value"] for bucket in buckets] == [0.25, None]
    assert buckets[1]["failure_reason"] == "source_timestamp_unplaced"

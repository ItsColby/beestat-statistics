"""Correction dependencies fence only relevant provider resolution changes."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from custom_components.beestat_statistics import hourly_history_delta as delta
from custom_components.beestat_statistics import hourly_sources as sources
from custom_components.beestat_statistics.hourly_import_core import HourlyImportError
from tests import test_hourly_history_writer as fixture
from tests.test_hourly_correction_writer import apply, prepare

pytest_plugins = ("tests.test_hourly_correction_writer",)


async def _delta(helper, start, end):
    request = {"start": start.isoformat(), "end": end.isoformat()}
    baseline = await helper.writer.async_history_source_points(
        request, context=helper.context
    )
    source_id = await helper.stage(
        start=start, hours=1, fan=300, acquisition="later-delta"
    )
    source = (
        await sources.async_load_source_bundle(
            helper.store, [source_id], helper.context["identity"]
        )
    )[0]
    raw = await helper.store.async_read_object("source", source["sha256"])
    prepared = delta.prepare_history_delta(
        [{"manifest": source["manifest"], "original_bytes": raw}],
        baseline,
        identity=helper.context["identity"],
        start=request["start"],
        end=request["end"],
        evaluated_at=helper.context["evaluated_at"],
        acquisition_id="later-delta",
    )
    assert prepared is not None
    ids = []
    for chunk in prepared["chunks"]:
        receipt = await sources.async_stage_source(
            helper.store,
            chunk["content"],
            chunk["manifest"],
            chunk["sha256"],
            helper.context["identity"],
        )
        ids.append(receipt["source_id"])
    return {**request, "source_ids": ids}


async def test_routine_delta_outside_corrected_dependency(adopted):
    helper = adopted
    request, _, _ = await prepare(helper, actions=("hold",))
    await apply(helper, request)
    await helper.complete()
    later = fixture.START + timedelta(days=60)
    helper.context["evaluated_at"] = (later + 3 * fixture.HOUR).isoformat()
    later_source = await helper.stage(
        start=later, hours=1, acquisition="later-original"
    )
    request = await helper.request(
        source_ids=[later_source],
        start=later.isoformat(),
        end=(later + fixture.HOUR).isoformat(),
        operation_id="adopt-later",
        expected_revision=helper.store.value["revision"],
    )
    await apply(helper, request)
    await helper.complete()
    update = await _delta(helper, later, later + fixture.HOUR)
    result = await helper.writer.async_refresh_history(update, context=helper.context)
    assert result["status"] == "accepted", result
    await helper.complete()
    assert helper.recorder.rows[fixture.NATIVE][later].mean == 100
    assert helper.recorder.rows[fixture.NATIVE][fixture.START].mean is None


async def test_unrelated_resource_controls_do_not_fence_but_policy_change_does(adopted):
    helper = adopted
    request, _, _ = await prepare(helper, actions=("hold",))
    await apply(helper, request)
    await helper.complete()
    root = helper.store.value
    writer = helper.writer._history_writer()
    request = {
        **request,
        "start": (fixture.START + 10 * fixture.HOUR).isoformat(),
        "end": (fixture.START + 11 * fixture.HOUR).isoformat(),
        "correction_ids": [],
        "evaluated_at": helper.context["evaluated_at"],
    }
    controls = {"runtime_thermostat:2": {"new": ["old"]}}
    await writer.built(request, root, helper.context, None, controls)
    with pytest.raises(HourlyImportError, match="correction_replan_required"):
        await writer.built(
            {**request, "archive_policy": "provider_gaps"},
            root,
            helper.context,
            None,
            controls,
        )


async def test_relevant_precedence_change_fences_omitted_correction(adopted):
    helper = adopted
    request, _, _ = await prepare(helper, actions=("hold",))
    await apply(helper, request)
    await helper.complete()
    writer = helper.writer._history_writer()
    other = await helper.stage(hours=1, fan=300, acquisition="competing")
    state = deepcopy(helper.store.value)
    history = state["history"]
    month = fixture.START.strftime("%Y-%m")
    catalog = await writer.catalog(history, month, helper.context)
    catalog["source_ids"].append(other)
    history["source_catalog"][month] = await writer.write_object(
        "operation", catalog, helper.context
    )
    history["provider_order"] = {"runtime_thermostat:1": ["read-one", "competing"]}
    request = {
        **request,
        "start": (fixture.START + 10 * fixture.HOUR).isoformat(),
        "end": (fixture.START + 11 * fixture.HOUR).isoformat(),
        "correction_ids": [],
    }
    with pytest.raises(HourlyImportError, match="correction_replan_required"):
        await writer.built(
            request,
            state,
            helper.context,
            {"runtime_thermostat:1": ["competing", "read-one"]},
            None,
        )

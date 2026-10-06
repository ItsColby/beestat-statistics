"""Fetch Beestat data and write it to Home Assistant external statistics."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from datetime import date as dt_date
from functools import partial
from typing import Any, cast
from zoneinfo import ZoneInfo

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.components.recorder.models.statistics import (
    StatisticData,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
)
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.recorder import get_instance as get_recorder_instance

from .api import (
    BeestatApiError,
    BeestatAuthError,
    BeestatClient,
    BeestatPermanentError,
    exception_fingerprint,
)
from .config_model import (
    build_sensor_statistics as build_sensor_specs,
)
from .configuration import configuration_response
from .const import (
    DEFAULT_SUMMARY_OVERLAP_DAYS,
    DOMAIN,
    STATISTIC_SOURCE,
    THERMOSTAT_POINT_STATISTICS,
)
from .coordinator import (
    BeestatRuntimeData,
    BeestatRuntimeDataCoordinator,
    TemporalContext,
)
from .hourly_history_contract import MAX_SOURCE_BYTES
from .hourly_history_contract import (
    digest as history_digest,
)
from .hourly_history_contract import (
    quantity_id as history_quantity_id,
)
from .hourly_history_query import history_response
from .hourly_history_runtime import async_refresh_history
from .hourly_history_values import build_history_series
from .hourly_import import HourlyImportManager, HourlyReconciliationError
from .hourly_recorder import HourlyRecorderError
from .hourly_sources import stage_source
from .hourly_statistics import build_hourly_statistics
from .hourly_storage import HourlyStorageError
from .import_evidence import SkippedWindowEvidence, SkippedWindowResource
from .import_support import (
    ImportResult,
    PreparedHourlyImport,
    PreparedImport,
    SummaryImportPlan,
    _combined_import_result,
    _cumulative_seeds_during_period,
    _dedupe_rows,
    _filter_series_statistics,
    _filter_summary_rows_by_thermostat,
    _format_beestat_time,
    _format_day,
    _format_start,
    _hourly_identity,
    _hourly_resource_identities,
    _hourly_retained_ids,
    _hourly_utc_hour,
    _hourly_window,
    _iter_windows,
    _latest_cumulative_starts,
    _latest_summary_day,
    _local_midnight,
    _observed_hourly_horizons,
    _point_window,
    _sensor_thermostat_map,
    _thermostat_data_end_map,
    _validate_hourly_window,
    _validate_thermostat_id,
    _writer_identity,
)
from .raw_points import (
    RawPointIdentity,
    RawPointRequest,
    async_read_raw_points,
    validate_raw_point_identity,
)
from .runtime import BeestatStatisticsRuntime, entry_scan_interval_seconds
from .statistics_builder import (
    CumulativeStatisticSeed,
    apply_cumulative_seeds,
    build_statistics,
    cumulative_statistic_ids,
    detailed_runtime_statistic_ids,
)

_LOGGER = logging.getLogger(__name__)

_IMPORT_TEMPORAL_CONTEXT_ATTEMPTS = 3


class BeestatStatisticsImporter:
    """Fetch Beestat data and import derived daily statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: BeestatClient,
        coordinator: BeestatRuntimeDataCoordinator,
        *,
        point_lookback_days: int,
    ) -> None:
        self._hass = hass
        self._client = client
        self._coordinator = coordinator
        self._point_lookback_days = point_lookback_days
        self._lock = asyncio.Lock()
        self._unloaded = False
        self._history_worker: asyncio.Task[None] | None = None
        self.hourly = HourlyImportManager(hass, coordinator.beestat_config_entry)
        coordinator.beestat_config_entry.async_on_unload(self._async_unload)

    @callback
    def _async_unload(self) -> None:
        """Prevent old service references from starting work after unload."""

        self._unloaded = True
        self.hourly.close()

    def _history_context(self) -> dict[str, Any]:
        """Detach identity/configuration without acquiring or changing anything."""
        entry = self._coordinator.beestat_config_entry
        runtime = getattr(entry, "runtime_data", None)
        data = self._coordinator.data
        if (
            self._unloaded
            or data is None
            or entry.state is not ConfigEntryState.LOADED
            or runtime is None
            or runtime.importer is not self
            or runtime.client is not self._client
        ):
            raise ValueError("history_entry_unavailable")
        temporal = self._coordinator.capture_temporal_context()
        identity = _writer_identity(entry, data)
        configuration = configuration_response(
            entry_id=entry.entry_id,
            entry_data=entry.data,
            entry_options=entry.options,
            config=data.config,
            point_lookback_days=self._point_lookback_days,
            scan_interval_seconds=entry_scan_interval_seconds(entry),
        )
        revision = history_digest(configuration)
        stop = temporal.evaluated_at.replace(minute=0, second=0, microsecond=0)
        inventory = build_hourly_statistics(
            {},
            {},
            data.config,
            start=stop - timedelta(hours=1),
            end=stop,
            evaluated_at=temporal.evaluated_at,
            source_end_by_thermostat={},
            existing_statistic_ids=(
                *cumulative_statistic_ids(data.config, list(data.summary_rows)),
                *(
                    base.removesuffix("_hourly_v2")
                    for base, resource in identity["resources"].items()
                    if history_quantity_id(resource)
                    in self.hourly.history_quantity_ids()
                ),
            ),
        )
        # Inventory describes method eligibility; it does not claim source data.
        inventory = tuple(
            replace(
                item,
                hours=(),
                blocked_reason=(
                    item.blocked_reason
                    if item.blocked_reason == "voc_unit_unresolved"
                    else None
                ),
            )
            for item in inventory
        )

        def check_current() -> None:
            current = self._coordinator.data
            if (
                self._unloaded
                or entry.state is not ConfigEntryState.LOADED
                or getattr(entry, "runtime_data", None) is not runtime
                or current is None
                or current.config != data.config
                or _writer_identity(entry, current) != identity
                or not self._coordinator.temporal_context_is_current(temporal)
                or history_digest(
                    configuration_response(
                        entry_id=entry.entry_id,
                        entry_data=entry.data,
                        entry_options=entry.options,
                        config=current.config,
                        point_lookback_days=self._point_lookback_days,
                        scan_interval_seconds=entry_scan_interval_seconds(entry),
                    )
                )
                != revision
            ):
                raise ValueError("history_context_changed")

        return {
            "identity": deepcopy(identity),
            "config": deepcopy(data.config),
            "config_revision": revision,
            "timezone": temporal.local_tz.key,
            "timezone_revision": temporal.timezone_revision,
            "evaluated_at": temporal.evaluated_at.isoformat(),
            "descriptors": [
                item.descriptor for item in build_history_series(inventory, identity)
            ],
            "check_current": check_current,
        }

    def history_configuration(self) -> dict[str, Any]:
        """Project cached capability and status without a provider/Recorder read."""
        return self.hourly.history_configuration(self._history_context())

    async def async_stage_hourly_source(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            context = self._history_context()
            receipt = await self._hass.async_add_executor_job(
                self._stage_uploaded_source, deepcopy(request), context["identity"]
            )
            context["check_current"]()
            return receipt

    def _stage_uploaded_source(
        self, request: dict[str, Any], identity: dict[str, Any]
    ) -> dict[str, Any]:
        # The native context includes cleanup and must remain off the event loop.
        # Both immutable objects are durably verified before the upload is freed.
        with process_uploaded_file(self._hass, request["file_id"]) as path:
            with path.open("rb") as source:
                content = source.read(MAX_SOURCE_BYTES + 1)
            return stage_source(
                self.hourly._store,
                content,
                request["manifest"],
                request["sha256"],
                identity,
            )

    async def async_plan_hourly_history(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            context = self._history_context()
            response = await self.hourly.async_plan_history(request, context=context)
            context["check_current"]()
            return response

    async def async_apply_hourly_history(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        if request["plan"]["config_entry_id"] != request["config_entry_id"]:
            raise ValueError("history_plan_entry_mismatch")
        # Service cancellation cannot cancel durably accepted work or prevent its
        # entry worker from being scheduled after acceptance.
        task = self._coordinator.beestat_config_entry.async_create_background_task(
            self._hass,
            self._async_accept_hourly_history(request),
            f"{DOMAIN}_accept_hourly_history",
        )
        return await asyncio.shield(task)

    async def _async_accept_hourly_history(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            context = self._history_context()
            response = await self.hourly.async_accept_history(
                {**request["plan"], "plan_digest": request["plan_digest"]},
                context=context,
            )
            self._ensure_history_worker()
            self._notify_history_changed()
            return response

    def _ensure_history_worker(self) -> None:
        if self._unloaded or not self.hourly.has_pending_history:
            return
        if self._history_worker is None or self._history_worker.done():
            self._history_worker = (
                self._coordinator.beestat_config_entry.async_create_background_task(
                    self._hass,
                    self._async_run_hourly_history_operation(),
                    f"{DOMAIN}_hourly_history",
                )
            )

    def _notify_history_changed(self) -> None:
        """Reuse status-entity updates for corrections to closed history."""
        status = self.hourly.status().get("history_v3", {})
        self._coordinator.hourly_history_revision = status.get("root_revision", 0)
        self._coordinator.hourly_history_status = status.get("status", "legacy")
        self._coordinator.async_update_listeners()

    async def _async_run_hourly_history_operation(self) -> None:
        try:
            while not self._unloaded and self.hourly.has_pending_history:
                async with self._lock:
                    context = self._history_context()
                    result = await self.hourly.async_advance_history(context=context)
                    self._notify_history_changed()
                if result.get("status") in {"blocked", "completed"}:
                    break
                await asyncio.sleep(0)
        except Exception as err:  # noqa: BLE001 - journal retains exact recovery intent
            _LOGGER.warning("History operation paused (%s)", exception_fingerprint(err))
            self._notify_history_changed()

    async def async_get_hourly_history(self, request: dict[str, Any]) -> dict[str, Any]:
        # A read must see pending suppression while a writer awaits native work.
        request = deepcopy(request)
        context = self._history_context()
        material = await self.hourly.async_history_material(request, context=context)
        context["check_current"]()
        # Material is a detached readback. Keep the pure, potentially large
        # digest/projection off the event loop without passing runtime owners.
        projection_context = {
            key: context[key]
            for key in (
                "identity",
                "config_revision",
                "timezone",
                "timezone_revision",
                "evaluated_at",
            )
        }
        response = await self._hass.async_add_executor_job(
            history_response, request, material, projection_context
        )
        context["check_current"]()
        await self.hourly.async_check_history_material(material, context=context)
        context["check_current"]()
        return response

    async def _async_refresh_hourly_history(
        self, *, lookback_days: int, rebuilding: bool
    ) -> str | None:
        """Refresh adopted v3 quantities on the existing locked import cadence."""
        status = self.hourly.history_status()
        if status["status"] == "unselected":
            return None
        if rebuilding:
            # An explicit historical repair needs its own sealed plan and bounds.
            return "history_rebuild_requires_plan"
        if status["status"] != "completed":
            self._ensure_history_worker()
            return "history_operation_pending"
        try:
            result = await async_refresh_history(
                self, self._history_context(), lookback_days=lookback_days
            )
        except (ValueError, BeestatApiError) as err:
            _LOGGER.warning("History refresh paused (%s)", exception_fingerprint(err))
            return "history_refresh_unverified"
        finally:
            self._ensure_history_worker()
            self._notify_history_changed()
        return (
            "history_operation_pending"
            if result.get("status") in {"accepted", "in_progress", "blocked"}
            else None
        )

    async def async_get_raw_points(self, request: RawPointRequest) -> dict[str, Any]:
        """Run a bounded source read under the loaded entry's task lifecycle."""

        entry = self._coordinator.beestat_config_entry
        runtime = entry.runtime_data
        self._raw_point_identity(request, runtime)
        return await entry.async_create_background_task(
            self._hass,
            self._async_get_raw_points(request, runtime),
            f"{DOMAIN}_get_raw_points",
        )

    def _raw_point_identity(
        self, request: RawPointRequest, runtime: BeestatStatisticsRuntime
    ) -> RawPointIdentity:
        entry = self._coordinator.beestat_config_entry
        if (
            self._unloaded
            or entry.state is not ConfigEntryState.LOADED
            or entry.runtime_data is not runtime
            or runtime.importer is not self
            or runtime.client is not self._client
            or runtime.coordinator is not self._coordinator
        ):
            raise RuntimeError(
                "Beestat Statistics config entry is unloaded or replaced"
            )
        data = self._coordinator.data
        return validate_raw_point_identity(
            request,
            data.config,
            data.thermostat_rows,
            data.sensor_rows,
            config_entry_id=entry.entry_id,
            metadata_fetched_at=data.fetched_at,
        )

    async def _async_get_raw_points(
        self, request: RawPointRequest, runtime: BeestatStatisticsRuntime
    ) -> dict[str, Any]:
        async with self._lock:
            identity = self._raw_point_identity(request, runtime)
            response = await async_read_raw_points(runtime.client, request, identity)
            if self._raw_point_identity(request, runtime) != identity:
                raise ValueError("Cached resource identity changed during source read")
            return response

    async def async_import_statistics(
        self,
        *,
        point_lookback_days: int | None = None,
        skip_sync: bool = False,
        force_full_summary: bool = False,
        rebuild_start: dt_date | None = None,
        rebuild_end: dt_date | None = None,
        thermostat_id: int | None = None,
    ) -> ImportResult:
        """Sync Beestat and import external statistics."""

        if self._unloaded:
            raise RuntimeError("Beestat Statistics config entry is unloaded")
        return (
            await self._coordinator.beestat_config_entry.async_create_background_task(
                self._hass,
                self._async_import_statistics(
                    point_lookback_days=point_lookback_days,
                    skip_sync=skip_sync,
                    force_full_summary=force_full_summary,
                    rebuild_start=rebuild_start,
                    rebuild_end=rebuild_end,
                    thermostat_id=thermostat_id,
                ),
                f"{DOMAIN}_import_statistics",
            )
        )

    async def _async_import_statistics(
        self,
        *,
        point_lookback_days: int | None,
        skip_sync: bool,
        force_full_summary: bool,
        rebuild_start: dt_date | None,
        rebuild_end: dt_date | None,
        thermostat_id: int | None,
    ) -> ImportResult:
        """Run both disjoint writers under the existing config-entry lock."""

        async with self._lock:
            if self._unloaded:
                raise RuntimeError("Beestat Statistics config entry is unloaded")
            partition = await self.hourly.async_writer_partition(
                _writer_identity(
                    self._coordinator.beestat_config_entry, self._coordinator.data
                )
            )
            self._ensure_history_worker()
            blocked = partition.hourly_blocked_reason
            if partition.hourly_ready:
                try:
                    await self.hourly.async_reconcile()
                except (
                    HourlyReconciliationError,
                    HourlyRecorderError,
                    HourlyStorageError,
                ):
                    # Only identified hourly persistence/readback failures are
                    # isolated. Revalidation below must still prove ownership.
                    blocked = "hourly_reconciliation_unverified"
            lookback_days = point_lookback_days or self._point_lookback_days
            runtime_data = await self._coordinator.async_refresh_runtime(
                skip_sync=skip_sync,
                summary_window=not force_full_summary,
            )
            _validate_thermostat_id(runtime_data, thermostat_id)
            partition = await self.hourly.async_writer_partition(
                _writer_identity(self._coordinator.beestat_config_entry, runtime_data)
            )
            blocked = blocked or partition.hourly_blocked_reason
            hourly_result: ImportResult | None = None
            if (
                partition.hourly_ready
                and partition.hourly_statistic_ids
                and blocked is None
            ):
                try:
                    hourly_result = await self._async_import_hourly(
                        runtime_data,
                        lookback_days=lookback_days,
                        rebuild_start=rebuild_start,
                        rebuild_end=rebuild_end,
                        thermostat_id=thermostat_id,
                    )
                except (
                    HourlyReconciliationError,
                    HourlyRecorderError,
                    HourlyStorageError,
                ):
                    blocked = "hourly_effect_unverified"
            history_blocked = await self._async_refresh_hourly_history(
                lookback_days=lookback_days,
                rebuilding=rebuild_start is not None or rebuild_end is not None,
            )
            blocked = blocked or history_blocked
            # A failed save can invalidate the manager's in-memory state, and
            # settings may change during an await. Never reuse an old partition.
            if self._unloaded:
                raise RuntimeError("Beestat Statistics config entry is unloaded")
            runtime_data = self._coordinator.data
            partition = await self.hourly.async_writer_partition(
                _writer_identity(self._coordinator.beestat_config_entry, runtime_data)
            )
            blocked = blocked or partition.hourly_blocked_reason
            legacy_result: ImportResult | None = None
            if partition.legacy_statistic_ids:
                legacy_result = await self._async_import_legacy(
                    runtime_data,
                    lookback_days=lookback_days,
                    force_full_summary=force_full_summary,
                    rebuild_start=rebuild_start,
                    rebuild_end=rebuild_end,
                    thermostat_id=thermostat_id,
                    allowed_legacy_ids=partition.legacy_statistic_ids,
                )
            result = _combined_import_result(
                legacy_result,
                hourly_result,
                has_hourly=partition.has_hourly,
                hourly_blocked_reason=blocked,
            )
            self._record_import_result(result)
            return result

    def _record_import_result(self, result: ImportResult) -> None:
        self._coordinator.async_record_import_result(
            imported_series=result.imported_series,
            imported_rows=result.imported_rows,
            source_rows=result.source_rows,
            skipped_windows=result.skipped_windows,
            skipped_runtime_thermostat_windows=result.skipped_runtime_thermostat_windows,
            skipped_runtime_sensor_windows=result.skipped_runtime_sensor_windows,
            skipped_window_examples=result.skipped_window_examples,
            summary_mode=result.summary_mode,
            summary_window_start=result.summary_window_start,
            summary_window_end=result.summary_window_end,
            summary_overlap_days=result.summary_overlap_days,
            summary_fallback_reason=result.summary_fallback_reason,
            cumulative_seed_count=result.cumulative_seed_count,
            coverage_incomplete=result.coverage_incomplete,
            writer_result={
                "legacy_imported_series": result.legacy_imported_series,
                "legacy_imported_rows": result.legacy_imported_rows,
                "hourly_imported_series": result.hourly_imported_series,
                "hourly_imported_rows": result.hourly_imported_rows,
                "hourly_blocked_reason": result.hourly_blocked_reason,
            },
        )
        _LOGGER.info(
            "Imported %s Beestat rows across %s series; mode=%s hourly_blocked=%s",
            result.imported_rows,
            result.imported_series,
            result.summary_mode,
            result.hourly_blocked_reason,
        )

    async def _async_import_legacy(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        lookback_days: int,
        force_full_summary: bool,
        rebuild_start: dt_date | None,
        rebuild_end: dt_date | None,
        thermostat_id: int | None,
        allowed_legacy_ids: frozenset[str],
    ) -> ImportResult:
        """Keep enabled unadmitted quantities on their existing daily writer."""

        prepared: PreparedImport | None = None
        for attempt in range(_IMPORT_TEMPORAL_CONTEXT_ATTEMPTS):
            temporal_context = self._coordinator.capture_temporal_context()
            prepared = await self._async_prepare_import(
                runtime_data,
                lookback_days=lookback_days,
                force_full_summary=force_full_summary,
                rebuild_start=rebuild_start,
                rebuild_end=rebuild_end,
                thermostat_id=thermostat_id,
                temporal_context=temporal_context,
                allowed_legacy_ids=allowed_legacy_ids,
            )
            current_partition = await self.hourly.async_writer_partition(
                _writer_identity(
                    self._coordinator.beestat_config_entry, self._coordinator.data
                )
            )
            if (
                self._coordinator.temporal_context_is_current(temporal_context)
                and runtime_data.config == self._coordinator.data.config
                and allowed_legacy_ids == current_partition.legacy_statistic_ids
            ):
                break
            if attempt + 1 == _IMPORT_TEMPORAL_CONTEXT_ATTEMPTS:
                raise RuntimeError(
                    "Home Assistant timezone or writer configuration changed repeatedly during "
                    "Beestat statistics import"
                )
            _LOGGER.info(
                "Restarting Beestat statistics preparation after a Home "
                "Assistant timezone change"
            )
            if self._coordinator.data is not None:
                runtime_data = self._coordinator.data
                allowed_legacy_ids = current_partition.legacy_statistic_ids

        assert prepared is not None  # the loop either breaks or raises

        imported_rows = 0
        latest_start_by_id: dict[str, str | None] = {}
        for item in prepared.series:
            async_add_external_statistics(
                self._hass,
                cast(StatisticMetaData, item.metadata),
                cast(Iterable[StatisticData], item.statistics),
            )
            imported_rows += len(item.statistics)
            latest_start_by_id[item.statistic_id] = _format_start(item)

        return ImportResult(
            imported_series=len(prepared.series),
            imported_rows=imported_rows,
            source_rows=len(prepared.summary_rows)
            + sum(len(rows) for rows in prepared.thermostat_rows_by_id.values())
            + sum(len(rows) for rows in prepared.sensor_rows_by_id.values()),
            skipped_windows=prepared.skipped_windows.total_count,
            skipped_runtime_thermostat_windows=(
                prepared.skipped_windows.runtime_thermostat_count
            ),
            skipped_runtime_sensor_windows=(
                prepared.skipped_windows.runtime_sensor_count
            ),
            skipped_window_examples=prepared.skipped_windows.examples,
            latest_start_by_statistic_id=latest_start_by_id,
            summary_mode=prepared.summary_plan.mode,
            summary_window_start=_format_day(prepared.summary_plan.window_start),
            summary_window_end=_format_day(prepared.summary_plan.window_end),
            summary_overlap_days=prepared.summary_plan.overlap_days,
            summary_fallback_reason=prepared.summary_plan.fallback_reason,
            cumulative_seed_count=len(prepared.summary_plan.seeds),
            legacy_imported_series=len(prepared.series),
            legacy_imported_rows=imported_rows,
        )

    async def _async_import_hourly(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        lookback_days: int,
        rebuild_start: dt_date | None,
        rebuild_end: dt_date | None,
        thermostat_id: int | None,
    ) -> ImportResult:
        prepared = await self._async_prepare_hourly(
            runtime_data,
            lookback_days=lookback_days,
            rebuild_start=rebuild_start,
            rebuild_end=rebuild_end,
            thermostat_id=thermostat_id,
        )
        imported = await self.hourly.async_import(
            prepared.series,
            prepared.identity,
            ordinary_start=prepared.ordinary_start,
            eligible_resources=prepared.eligible_resources,
        )
        status = self.hourly.status()
        coverage_incomplete = bool(status.get("pending")) or any(
            record.get("coverage_incomplete", False)
            for record in status.get("series", {}).values()
        )
        skipped = prepared.skipped_windows
        return ImportResult(
            imported_series=imported["imported_series"],
            imported_rows=imported["imported_rows"],
            source_rows=prepared.source_rows,
            skipped_windows=skipped.total_count,
            skipped_runtime_thermostat_windows=skipped.runtime_thermostat_count,
            skipped_runtime_sensor_windows=skipped.runtime_sensor_count,
            skipped_window_examples=skipped.examples,
            latest_start_by_statistic_id=imported["latest_start_by_statistic_id"],
            summary_mode="hourly",
            summary_window_start=None,
            summary_window_end=None,
            summary_overlap_days=None,
            summary_fallback_reason=(
                "hourly_coverage_incomplete" if coverage_incomplete else None
            ),
            cumulative_seed_count=0,
            hourly_imported_series=imported["imported_series"],
            hourly_imported_rows=imported["imported_rows"],
            coverage_incomplete=coverage_incomplete,
        )

    async def _async_prepare_hourly(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        lookback_days: int,
        rebuild_start: dt_date | None = None,
        rebuild_end: dt_date | None = None,
        thermostat_id: int | None = None,
        epoch_start: datetime | None = None,
        statistic_ids: tuple[str, ...] = (),
    ) -> PreparedHourlyImport:
        for attempt in range(_IMPORT_TEMPORAL_CONTEXT_ATTEMPTS):
            context = self._coordinator.capture_temporal_context()
            start, end, measurement_end = _hourly_window(
                context,
                lookback_days=lookback_days,
                rebuild_start=rebuild_start,
                rebuild_end=rebuild_end,
                epoch_start=epoch_start,
                bootstrap_start=self.hourly.bootstrap_start(
                    thermostat_id=thermostat_id,
                    eligible_resources=_hourly_resource_identities(runtime_data.config),
                ),
            )
            skipped = SkippedWindowEvidence()
            thermostat_rows = await self._async_fetch_thermostat_rows(
                lookback_days,
                runtime_data,
                skipped,
                thermostat_id=thermostat_id,
                temporal_context=context,
                window=(start, end),
                preserve_source_rows=True,
            )
            sensor_rows = await self._async_fetch_sensor_rows(
                lookback_days,
                runtime_data,
                skipped,
                thermostat_id=thermostat_id,
                temporal_context=context,
                window=(start, end),
                preserve_source_rows=True,
            )
            if (
                self._coordinator.temporal_context_is_current(context)
                and runtime_data.config == self._coordinator.data.config
            ):
                break
            if attempt + 1 == _IMPORT_TEMPORAL_CONTEXT_ATTEMPTS:
                raise RuntimeError(
                    "Home Assistant timezone or configuration changed during hourly preparation"
                )
            runtime_data = self._coordinator.data or runtime_data
        config = runtime_data.config
        if thermostat_id is not None:
            config = replace(
                config,
                thermostats=tuple(
                    item
                    for item in config.thermostats
                    if item.thermostat_id == thermostat_id
                ),
                sensors=tuple(
                    item
                    for item in config.sensors
                    if item.thermostat_id == thermostat_id
                ),
            )
        retained = (*self.hourly.base_statistic_ids(), *statistic_ids)
        ordinary_start = (
            end - timedelta(days=lookback_days)
            if epoch_start is None and rebuild_start is None
            else None
        )
        source_starts = (
            self.hourly.source_starts(
                _hourly_resource_identities(config),
                start=start,
                end=end,
                ordinary_start=ordinary_start,
            )
            if epoch_start is None
            else {}
        )
        series = build_hourly_statistics(
            thermostat_rows,
            sensor_rows,
            config,
            start=start,
            end=end,
            evaluated_at=context.evaluated_at,
            source_end_by_thermostat=_observed_hourly_horizons(
                thermostat_rows,
                _thermostat_data_end_map(list(runtime_data.thermostat_rows)),
            ),
            existing_statistic_ids=_hourly_retained_ids(config, retained),
            start_by_statistic_id=source_starts,
            measurement_end=measurement_end,
        )
        return PreparedHourlyImport(
            series,
            _hourly_identity(
                self._coordinator.beestat_config_entry,
                runtime_data,
                series,
                selected_thermostat_id=thermostat_id,
            ),
            sum(len(rows) for rows in thermostat_rows.values())
            + sum(len(rows) for rows in sensor_rows.values()),
            skipped,
            ordinary_start,
            _hourly_resource_identities(config),
        )

    async def async_select_hourly_statistics(
        self,
        *,
        epoch_start: datetime,
        statistic_ids: tuple[str, ...],
        expected_revision: int,
        preview_digest: str | None = None,
    ) -> dict[str, Any]:
        if self._unloaded:
            raise RuntimeError("Beestat Statistics config entry is unloaded")
        return (
            await self._coordinator.beestat_config_entry.async_create_background_task(
                self._hass,
                self._async_select_hourly_statistics(
                    epoch_start=epoch_start,
                    statistic_ids=statistic_ids,
                    expected_revision=expected_revision,
                    preview_digest=preview_digest,
                ),
                f"{DOMAIN}_select_hourly_statistics",
            )
        )

    async def _async_select_hourly_statistics(
        self,
        *,
        epoch_start: datetime,
        statistic_ids: tuple[str, ...],
        expected_revision: int,
        preview_digest: str | None,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._unloaded:
                raise RuntimeError("Beestat Statistics config entry is unloaded")
            epoch_start = _hourly_utc_hour(epoch_start)
            if not statistic_ids or len(set(statistic_ids)) != len(statistic_ids):
                raise ValueError("Select distinct hourly statistic IDs")
            await self.hourly.async_reconcile()
            data = await self._coordinator.async_refresh_runtime(
                skip_sync=True, summary_window=True
            )
            prepared = await self._async_prepare_hourly(
                data,
                lookback_days=self._point_lookback_days,
                epoch_start=epoch_start,
                statistic_ids=statistic_ids,
            )
            return await self.hourly.async_select(
                prepared.series,
                prepared.identity,
                epoch_start=epoch_start,
                statistic_ids=statistic_ids,
                expected_revision=expected_revision,
                preview_digest=preview_digest,
            )

    async def async_get_hourly_coverage(
        self,
        *,
        start: datetime,
        end: datetime,
        statistic_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        if self._unloaded:
            raise RuntimeError("Beestat Statistics config entry is unloaded")
        return (
            await self._coordinator.beestat_config_entry.async_create_background_task(
                self._hass,
                self._async_get_hourly_coverage(start, end, statistic_ids),
                f"{DOMAIN}_get_hourly_coverage",
            )
        )

    async def _async_get_hourly_coverage(
        self, start: datetime, end: datetime, statistic_ids: tuple[str, ...] | None
    ) -> dict[str, Any]:
        if self._unloaded:
            raise RuntimeError("Beestat Statistics config entry is unloaded")
        start, end = _hourly_utc_hour(start), _hourly_utc_hour(end)
        _validate_hourly_window(start, end)
        # A cached read must expose suppression while a writer awaits Recorder.
        return self.hourly.coverage(start=start, end=end, statistic_ids=statistic_ids)

    async def _async_prepare_import(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        lookback_days: int,
        force_full_summary: bool,
        rebuild_start: dt_date | None,
        rebuild_end: dt_date | None,
        thermostat_id: int | None,
        temporal_context: TemporalContext,
        allowed_legacy_ids: frozenset[str] | None = None,
    ) -> PreparedImport:
        """Prepare one coherent local-time import before Recorder writes."""

        existing_statistic_ids = await self._async_existing_detailed_statistic_ids(
            runtime_data, allowed_legacy_ids=allowed_legacy_ids
        )
        summary_plan = await self._async_summary_import_plan(
            runtime_data,
            force_full_summary=force_full_summary,
            temporal_context=temporal_context,
            existing_statistic_ids=existing_statistic_ids,
            allowed_legacy_ids=allowed_legacy_ids,
        )
        summary_rows = _filter_summary_rows_by_thermostat(
            summary_plan.rows,
            thermostat_id,
        )
        skipped_windows = SkippedWindowEvidence()
        thermostat_rows_by_id = await self._async_fetch_thermostat_rows(
            lookback_days,
            runtime_data,
            skipped_windows,
            start_day=rebuild_start,
            end_day=rebuild_end,
            thermostat_id=thermostat_id,
            temporal_context=temporal_context,
            allowed_statistic_ids=allowed_legacy_ids,
        )
        sensor_rows_by_id = await self._async_fetch_sensor_rows(
            lookback_days,
            runtime_data,
            skipped_windows,
            start_day=rebuild_start,
            end_day=rebuild_end,
            thermostat_id=thermostat_id,
            temporal_context=temporal_context,
            allowed_statistic_ids=allowed_legacy_ids,
        )
        series = build_statistics(
            summary_rows,
            thermostat_rows_by_id,
            sensor_rows_by_id,
            temporal_context.local_tz,
            runtime_data.config,
            existing_statistic_ids=existing_statistic_ids,
        )
        if allowed_legacy_ids is not None:
            known_ids = {
                key.removesuffix("_hourly_v2")
                for key in _hourly_resource_identities(runtime_data.config)
            }
            if any(item.statistic_id not in known_ids for item in series):
                raise ValueError("Legacy output has no verified resource identity")
            series = [
                item for item in series if item.statistic_id in allowed_legacy_ids
            ]
        if summary_plan.seeds:
            series = apply_cumulative_seeds(series, summary_plan.seeds)
        if rebuild_start is not None or rebuild_end is not None:
            series = _filter_series_statistics(
                series,
                start_day=rebuild_start,
                end_day=rebuild_end,
                local_tz=temporal_context.local_tz,
            )
        return PreparedImport(
            summary_plan=summary_plan,
            summary_rows=summary_rows,
            skipped_windows=skipped_windows,
            thermostat_rows_by_id=thermostat_rows_by_id,
            sensor_rows_by_id=sensor_rows_by_id,
            series=[item for item in series if item.statistics],
        )

    async def _async_summary_import_plan(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        force_full_summary: bool,
        temporal_context: TemporalContext,
        existing_statistic_ids: frozenset[str],
        allowed_legacy_ids: frozenset[str] | None = None,
    ) -> SummaryImportPlan:
        cached_rows = list(runtime_data.summary_rows)
        if force_full_summary:
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="forced_full_baseline",
            )

        statistic_ids = cumulative_statistic_ids(
            runtime_data.config,
            cached_rows,
            existing_statistic_ids=existing_statistic_ids,
        )
        if allowed_legacy_ids is not None:
            statistic_ids = tuple(
                value for value in statistic_ids if value in allowed_legacy_ids
            )
        if not statistic_ids:
            return SummaryImportPlan.full(
                cached_rows,
                fallback_reason="no_cumulative_statistics",
            )

        latest_by_id = await self._async_latest_cumulative_starts(statistic_ids)
        if len(latest_by_id) != len(statistic_ids):
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="missing_latest_recorder_statistics",
            )

        latest_day = min(
            value.astimezone(temporal_context.local_tz).date()
            for value in latest_by_id.values()
        )
        window_start = latest_day - timedelta(days=DEFAULT_SUMMARY_OVERLAP_DAYS)
        window_end = (
            _latest_summary_day(cached_rows)
            or temporal_context.evaluated_at.astimezone(
                temporal_context.local_tz
            ).date()
        )
        if window_start > window_end:
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="empty_summary_window",
            )

        seeds = await self._async_cumulative_seeds(
            statistic_ids,
            seed_day=window_start - timedelta(days=1),
            window_start=window_start,
            local_tz=temporal_context.local_tz,
        )
        if len(seeds) != len(statistic_ids):
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="missing_prior_recorder_seed",
            )

        try:
            rows = await self._client.async_read_runtime_thermostat_summary(
                window_start.isoformat(),
                window_end.isoformat(),
            )
        except BeestatAuthError:
            raise
        except BeestatApiError:
            _LOGGER.warning(
                "Falling back to full Beestat summary baseline after windowed read failed"
            )
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="summary_window_read_failed",
            )

        # The Recorder window can include hardware absent from the recent cache,
        # or a correction can introduce another stage between the two reads.
        if (
            not {
                value
                for value in cumulative_statistic_ids(
                    runtime_data.config,
                    rows,
                    existing_statistic_ids=existing_statistic_ids,
                )
                if allowed_legacy_ids is None or value in allowed_legacy_ids
            }
            <= seeds.keys()
        ):
            full_rows = await self._async_full_summary_rows(runtime_data)
            return SummaryImportPlan.full(
                full_rows,
                fallback_reason="missing_prior_recorder_seed",
            )

        return SummaryImportPlan(
            rows=rows,
            seeds=seeds,
            mode="windowed",
            window_start=window_start,
            window_end=window_end,
            overlap_days=DEFAULT_SUMMARY_OVERLAP_DAYS,
            fallback_reason=None,
        )

    async def _async_full_summary_rows(
        self,
        runtime_data: BeestatRuntimeData,
    ) -> list[dict[str, Any]]:
        if runtime_data.summary_rows_full:
            return list(runtime_data.summary_rows)
        return await self._client.async_read_id("runtime_thermostat_summary")

    async def _async_existing_detailed_statistic_ids(
        self,
        runtime_data: BeestatRuntimeData,
        *,
        allowed_legacy_ids: frozenset[str] | None = None,
    ) -> frozenset[str]:
        """Retain imported hardware even when its source runtime becomes all zero."""

        statistic_ids = set(detailed_runtime_statistic_ids(runtime_data.config))
        if allowed_legacy_ids is not None:
            statistic_ids.intersection_update(allowed_legacy_ids)
        if not statistic_ids:
            return frozenset()
        recorder = get_recorder_instance(self._hass)
        # Reads use a different executor from imports. Queue an unconditional
        # marker so even the last running import, including an old entry's
        # queued work, is processed before inventory and cumulative seed reads.
        synchronized: asyncio.Future[None] = self._hass.loop.create_future()
        recorder.queue_task(SynchronizeTask(synchronized))
        await asyncio.shield(synchronized)
        metadata = await recorder.async_add_executor_job(
            partial(get_metadata, self._hass, statistic_ids=statistic_ids)
        )
        return frozenset(metadata)

    async def _async_latest_cumulative_starts(
        self,
        statistic_ids: Iterable[str],
    ) -> dict[str, datetime]:
        return await get_recorder_instance(self._hass).async_add_executor_job(
            partial(_latest_cumulative_starts, self._hass, tuple(statistic_ids))
        )

    async def _async_cumulative_seeds(
        self,
        statistic_ids: Iterable[str],
        *,
        seed_day: dt_date,
        window_start: dt_date,
        local_tz: ZoneInfo,
    ) -> dict[str, CumulativeStatisticSeed]:
        return await get_recorder_instance(self._hass).async_add_executor_job(
            partial(
                _cumulative_seeds_during_period,
                self._hass,
                tuple(statistic_ids),
                _local_midnight(seed_day, local_tz).astimezone(UTC),
                _local_midnight(window_start, local_tz).astimezone(UTC),
            )
        )

    async def _async_fetch_thermostat_rows(
        self,
        lookback_days: int,
        runtime_data: BeestatRuntimeData,
        skipped_windows: SkippedWindowEvidence,
        *,
        start_day: dt_date | None = None,
        end_day: dt_date | None = None,
        thermostat_id: int | None = None,
        temporal_context: TemporalContext,
        window: tuple[datetime, datetime] | None = None,
        preserve_source_rows: bool = False,
        allowed_statistic_ids: frozenset[str] | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        start, end = window or _point_window(
            lookback_days,
            temporal_context.local_tz,
            start_day,
            end_day,
            evaluated_at=temporal_context.evaluated_at,
        )
        thermostat_data_end = _thermostat_data_end_map(
            list(runtime_data.thermostat_rows)
        )

        rows_by_id: dict[int, list[dict[str, Any]]] = {}
        thermostat_ids = sorted(
            thermostat.thermostat_id
            for thermostat in runtime_data.config.thermostats
            if thermostat_id is None or thermostat.thermostat_id == thermostat_id
            if allowed_statistic_ids is None
            or any(
                f"{STATISTIC_SOURCE}:{thermostat.slug}_{spec.statistic_suffix}"
                in allowed_statistic_ids
                for spec in THERMOSTAT_POINT_STATISTICS
            )
        )
        for current_thermostat_id in thermostat_ids:
            rows: list[dict[str, Any]] = []
            cap_end = min(end, thermostat_data_end.get(current_thermostat_id, end))
            if start > cap_end:
                rows_by_id[current_thermostat_id] = []
                continue
            for window_start, window_end in _iter_windows(start, cap_end):
                rows.extend(
                    await self._async_read_runtime_thermostat_window(
                        current_thermostat_id,
                        window_start,
                        window_end,
                        skipped_windows,
                    )
                )
            rows_by_id[current_thermostat_id] = (
                rows
                if preserve_source_rows
                else _dedupe_rows(rows, id_field="thermostat_id")
            )
        return rows_by_id

    async def _async_read_runtime_thermostat_window(
        self,
        thermostat_id: int,
        start: datetime,
        end: datetime,
        skipped_windows: SkippedWindowEvidence,
    ) -> list[dict[str, Any]]:
        return await self._async_read_runtime_window(
            "runtime_thermostat", thermostat_id, start, end, skipped_windows
        )

    async def _async_fetch_sensor_rows(
        self,
        lookback_days: int,
        runtime_data: BeestatRuntimeData,
        skipped_windows: SkippedWindowEvidence,
        *,
        start_day: dt_date | None = None,
        end_day: dt_date | None = None,
        thermostat_id: int | None = None,
        temporal_context: TemporalContext,
        window: tuple[datetime, datetime] | None = None,
        preserve_source_rows: bool = False,
        allowed_statistic_ids: frozenset[str] | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        start, end = window or _point_window(
            lookback_days,
            temporal_context.local_tz,
            start_day,
            end_day,
            evaluated_at=temporal_context.evaluated_at,
        )
        sensor_to_thermostat = _sensor_thermostat_map(list(runtime_data.sensor_rows))
        thermostat_data_end = _thermostat_data_end_map(
            list(runtime_data.thermostat_rows)
        )
        configured_sensor_ids = {
            sensor.sensor_id
            for sensor in runtime_data.config.sensors
            if thermostat_id is None or sensor.thermostat_id == thermostat_id
        }

        rows_by_id: dict[int, list[dict[str, Any]]] = {}
        sensor_ids = sorted(
            {
                spec.sensor_id
                for spec in build_sensor_specs(runtime_data.config)
                if spec.sensor_id in configured_sensor_ids
                if allowed_statistic_ids is None
                or f"{STATISTIC_SOURCE}:{spec.statistic_suffix}"
                in allowed_statistic_ids
            }
        )
        for sensor_id in sensor_ids:
            rows: list[dict[str, Any]] = []
            mapped_thermostat_id = sensor_to_thermostat.get(sensor_id)
            cap_end = min(
                end,
                thermostat_data_end.get(mapped_thermostat_id, end)
                if mapped_thermostat_id is not None
                else end,
            )
            if start > cap_end:
                rows_by_id[sensor_id] = []
                continue
            for window_start, window_end in _iter_windows(start, cap_end):
                rows.extend(
                    await self._async_read_runtime_sensor_window(
                        sensor_id,
                        window_start,
                        window_end,
                        skipped_windows,
                    )
                )
            rows_by_id[sensor_id] = (
                rows
                if preserve_source_rows
                else _dedupe_rows(rows, id_field="sensor_id")
            )
        return rows_by_id

    async def _async_read_runtime_sensor_window(
        self,
        sensor_id: int,
        start: datetime,
        end: datetime,
        skipped_windows: SkippedWindowEvidence,
    ) -> list[dict[str, Any]]:
        return await self._async_read_runtime_window(
            "runtime_sensor", sensor_id, start, end, skipped_windows
        )

    async def _async_read_runtime_window(
        self,
        resource: SkippedWindowResource,
        resource_id: int,
        start: datetime,
        end: datetime,
        skipped_windows: SkippedWindowEvidence,
    ) -> list[dict[str, Any]]:
        """Recover oversized source windows with one bounded bisection policy."""

        read = (
            self._client.async_read_runtime_thermostat
            if resource == "runtime_thermostat"
            else self._client.async_read_runtime_sensor
        )
        try:
            return await read(
                resource_id,
                _format_beestat_time(start),
                _format_beestat_time(end),
            )
        except BeestatAuthError, BeestatPermanentError:
            raise
        except BeestatApiError as err:
            if end - start > timedelta(days=1):
                midpoint = start + ((end - start) / 2)
                rows: list[dict[str, Any]] = []
                rows.extend(
                    await self._async_read_runtime_window(
                        resource,
                        resource_id,
                        start,
                        midpoint,
                        skipped_windows,
                    )
                )
                rows.extend(
                    await self._async_read_runtime_window(
                        resource,
                        resource_id,
                        midpoint,
                        end,
                        skipped_windows,
                    )
                )
                return rows

            skipped_windows.record(
                resource,
                start=_format_beestat_time(start),
                end=_format_beestat_time(end),
            )
            _LOGGER.warning(
                "Skipping Beestat %s window start=%s end=%s: %s",
                resource,
                _format_beestat_time(start),
                _format_beestat_time(end),
                exception_fingerprint(err),
            )
            return []

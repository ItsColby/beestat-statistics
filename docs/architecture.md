# Architecture

Beestat Statistics brings Beestat history and context into Home Assistant. It owns acquisition from Beestat, external Recorder statistics, its own entities, and locally recorded filter boundaries. Beestat supplies source history; Home Assistant's existing HomeKit/Ecobee integrations own their devices and live controls. This integration reads those local registrations and selected states to associate context with the right equipment. Equipment-specific limits, physical maintenance decisions, notifications, tasks, and complete maintenance history belong to users and their automation systems. Software filter defaults are not manufacturer recommendations.

This reference explains contracts that a maintainer must preserve. See [Usage](usage.md) for configuration and actions, [Development](development.md) for validation, and [the README](../README.md) for installation.

## Runtime ownership and data flow

One config entry contains a typed [`BeestatStatisticsRuntime`](../custom_components/beestat_statistics/runtime.py): an API client, coordinator, importer, and configured import interval. [`__init__.py`](../custom_components/beestat_statistics/__init__.py) owns setup and unload; [`services.py`](../custom_components/beestat_statistics/services.py) owns service dispatch, [`importer.py`](../custom_components/beestat_statistics/importer.py) Recorder writes, and [`tracking.py`](../custom_components/beestat_statistics/tracking.py) registry and state listeners. The manifest declares a cloud-polling hub with Recorder as a dependency; HomeKit is an optional ordering dependency.

The normal flow is:

1. The client requests Beestat synchronization, then reads thermostat, sensor, and summary resources. An optional `ecobee_thermostat` read supplies allowlisted settings; its failure leaves that projection unavailable without discarding primary runtime data.
2. The coordinator normalizes resource identities and builds one `BeestatRuntimeData` snapshot containing effective configuration, source rows, runtime observations, metadata, settings, and room-temperature spread.
3. Entity platforms project that snapshot. Registry and mapped-state events can rebuild relevant cached projections; entity property reads perform no network requests.
4. The importer resolves writer ownership per source quantity under its existing lock. Enabled quantities without an hourly selection retain daily imports. Selected quantities reconcile any pending hourly batch before acquiring their next point history, then build and verify hourly statistics through Home Assistant's external-statistics API. Recorder remains the history owner.

Runtime refreshes and imports have separate locks. Scheduled import requests coalesce to one running pass plus one pending follow-up through [`task_coalescer.py`](../custom_components/beestat_statistics/task_coalescer.py). This bounds repeated timer/helper events without dropping the need to reconcile current state. Entry-owned background tasks, listeners, and timers stop on unload; retained coordinator/importer references reject new work. The startup import reuses the initial refresh by skipping another sync request.

## Identity and configuration

Connection data, mutable options, source identity, and presentation names have different lifetimes. [`config_payload.py`](../custom_components/beestat_statistics/config_payload.py) owns the stored representation; [`config_model.py`](../custom_components/beestat_statistics/config_model.py) resolves it into effective thermostat and sensor models.

Entry data stores connection/account continuity information and may contain imported mappings. Options own timing and UI-edited mapping collections. When an options collection exists, it replaces that collection from data; it is not a field-by-field overlay. Mutation helpers preserve unrelated and unrecognized fields. [`config_rows.py`](../custom_components/beestat_statistics/config_rows.py) gives every consumer the same last-valid-row-per-positive-ID interpretation of duplicate overrides. Malformed identity rows cannot silently claim a valid resource.

The config flow allows one entry. An authenticated thermostat read supplies hashed thermostat identities for account continuity; an empty or unidentifiable response cannot establish a new account. Overlapping anchors allow an account's equipment inventory to change. A disjoint account requires confirmation, which clears thermostat and sensor mappings while preserving unrelated configuration. Recorder history remains, so reused slugs can continue an earlier account's series. Migrations do not invent authenticated identity.

Awaited validation and preview steps cannot assume their initial entry snapshot still owns the save. Connection flows check the saved data again; account replacement checks both data and options. Same-account connection edits preserve intervening options updates. Mapping/source-scope previews are checked against current discovery and configuration before committing. A changed owner aborts or regenerates the relevant preview rather than overwriting a winning update. These races are exercised in [`test_config_flow_ha.py`](../tests/test_config_flow_ha.py).

Native entity unique IDs use numeric Beestat resource IDs plus a semantic suffix. Display names and suggested entity IDs can change without changing that identity. Config entries below version 1.6 are rejected rather than migrated; upgrade through v2026.10.7 first. Legacy external Recorder statistic IDs use the effective slug under the `beestat:` source, so changing a slug changes their statistic identity. Explicit hourly adoption binds the initial statistic ID to the source resource and quantity; later display-name or slug changes retain that adopted ID. Neither path automatically migrates legacy history.

### Mapping and physical proof

Automatic mapping is a convenience candidate process using recognized HomeKit/Ecobee registrations, names, and uniqueness. Explicit selections take priority. Ambiguous candidates or duplicate claims do not share a foreign device. An explicit mapping that is temporarily missing remains an explicit unresolved selection; automatic discovery cannot quietly replace it.

UI selections retain a stable registry reference: registry-entry UUID plus domain, platform, and unique ID. [`entity_reference.py`](../custom_components/beestat_statistics/entity_reference.py) checks the UUID against that identity, then can recover the same source by identity after registry recreation. Once a stable reference exists, reusing its old entity ID for a different source is not recovery.

A mixed HomeKit climate/Ecobee temperature mapping needs stronger proof than an ordinary naming match. [`source_identity.py`](../custom_components/beestat_statistics/source_identity.py) recognizes Ecobee's built-in `ei:0` temperature probe only when its unique ID agrees with its Ecobee device identifier and that identifier matches one unambiguous Ecobee HomeKit thermostat serial. Names, areas, aliases, and equal readings are not physical proof. Cloud-only mappings remain supported. Identity drift invalidates the mixed claim, and registry recovery reevaluates it. An unavailable selected probe is not replaced by the thermostat's potentially room-combined displayed temperature. [`test_source_identity_ha.py`](../tests/test_source_identity_ha.py) covers proof, ambiguity, duplicate claims, drift, and recovery.

Built-in Beestat sensor rows inherit their thermostat's physical identity constraints. A child override must belong to that same device or satisfy the same mixed-source proof; options validation, runtime resolution, and Repairs evaluate the effective inherited claims together.

[`entity.py`](../custom_components/beestat_statistics/entity.py) links derived entities to mapped devices through Home Assistant's helper integration API without making this config entry a co-owner of those source devices. Unmapped resources use integration-owned fallback devices. Cleanup admits only ordinary device entries exclusively owned by this config entry, with Beestat-only identifiers and no connections; child devices cannot satisfy that ownership check. Discovery adds newly available entity identities once; relinking follows registry changes. Repairs identify missing references, invalid domains, and conflicting device claims.

## Clocks and source quality

Acquisition cadence, source age, cached projection time, and effect retries are independent. The coordinator records fetch, projection, runtime-sync, and metadata-sync timestamps separately. A successful request says when data was acquired; the thermostat's `data_end` and latest summary date say how far its source history extends.

One coordinator timer selects the earliest cached schedule transition, cloud-staleness threshold, filter uncertainty boundary, or local midnight. It rebuilds from cached rows without external I/O, then schedules the next boundary. Listener updates occur when projected values change. The filter boundary reevaluates a not-due result when elapsed unreported exposure can make it unknown. Home Assistant timezone changes increment a revision and rebuild the local projection; thermostat schedule calculation can use the thermostat's own timezone. The cloud-stale threshold allows the configured poll interval plus source grace, subject to a minimum. Constants and the exact rounded-lag boundary live in [`coordinator.py`](../custom_components/beestat_statistics/coordinator.py), not in a second configuration contract here.

Imports capture one evaluation time, timezone, and revision before preparing daily rows. A timezone change during awaited preparation restarts preparation before any Recorder submission, with a bounded number of attempts. Local days use actual UTC elapsed time across daylight-saving transitions. [`test_runtime_ha.py`](../tests/test_runtime_ha.py) exercises real timer transitions without a new source event, timezone changes, bounded import restarts, and unload cancellation.

Current comfort profile mirrors cached `currentClimateRef`; scheduled profiles project the cached program. Both use one normalization in which the last row for a climate reference wins, retaining sensor identities for membership while publishing names in profile attributes. Neither reconstructs live holds. Sensor `in_use` is Beestat's reported flag, not configured membership or momentary weighting. [`alerts.py`](../custom_components/beestat_statistics/alerts.py) keeps equipment and unknown alerts visible even alongside maintenance reminders. It recognizes the explicit maintenance codes in the Ecobee API v1 Alert Object contract (Alert Numbers table) before interpreting equipment words in reminder text. Other numeric codes may be identified as equipment from their text, but are never downgraded to maintenance by a keyword; unrecognized codes remain unknown. Legacy alerts without a numeric code retain text classification.

Room-temperature spread combines mapped readings for identity-qualified members of that thermostat's configured profile. It converts supported explicit units and accepts only absolute `temperature` observations when a live device class is present; sources with no device-class attribute remain supported. Absolute readings below 0 K, −273.15 °C or −459.67 °F are invalid, allowing only `1e-9` degrees of floating-point roundoff without clipping. Invalid or unavailable observations reduce coverage without removing their mappings, so a later valid state recovers through local events without cloud I/O. The spread itself is a temperature difference. At least two valid readings can produce a partial range that understates the full profile spread. Observation age alone does not reject a valid local reading.

Entity registry defaults, listed in [usage](usage.md), apply only to new registry entries, so updating never changes an existing entity's enabled state. Runtime summary lag stays enabled because the bundled stale-runtime blueprint selects it.

## Recorder integrity

[`statistics_builder.py`](../custom_components/beestat_statistics/statistics_builder.py) owns pure daily-series construction. Summary rows supply runtime, counters, and summary measurements; thermostat and room-sensor five-minute rows supply daily mean/min/max measurements. Runtime/counter series publish cumulative `state` and `sum`; measurement series publish arithmetic means and available extrema. Local-day timestamps, units, conversion factors, and metadata are part of that contract.

This production format has an interval limitation: Home Assistant's external
statistics import is hourly, so it represents these daily aggregates as one-hour
rows. Daily cumulative differences can still be meaningful, but hourly queries
cannot recover the day's missing intervals. The explicit hourly mode described
below supplies actual hourly rows without rewriting those legacy IDs. Installing
this source does not admit quantities or migrate an existing entry.
[Core import contract](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/recorder/statistics.py)
(`async_add_external_statistics`).

Known-unit absolute temperatures share the lower-bound check in [`temperature.py`](../custom_components/beestat_statistics/temperature.py). Historical outdoor, setpoint and room-sensor values are checked after source scaling and before aggregation; summary means and each supplied extremum are checked independently. Their qualified upstream tenths-Fahrenheit representation permits a `0.05 °F` half-step allowance in addition to floating-point roundoff, preserving the rounded absolute-zero value `−459.7 °F` while rejecting `−460 °F`. This source allowance does not apply to precise local HA readings. The pinned Beestat [runtime scaling](https://github.com/beestat/app/blob/42c3b775cbb2e8a6893ac1346a8b42e3c0bd17e1/api/runtime_sensor.php#L113) and [summary rounding](https://github.com/beestat/app/blob/42c3b775cbb2e8a6893ac1346a8b42e3c0bd17e1/api/runtime_thermostat_summary.php#L345) show this representation; the [API inventory](beestat-api-surface.json) records their source identities. Invalid means omit that day's measurement, while an invalid extremum omits only that extremum. Valid negative Celsius/Fahrenheit readings and finite high temperatures remain supported.

Routine cumulative imports read an overlapping summary window and seed it from Recorder immediately before that window. Missing latest rows, missing seeds, or a failed window read cause a full-summary fallback. Seed coverage is checked against the fetched window as well as the cached inventory, so a newly discovered stage cannot restart an existing cumulative total from zero. A rebuild reads the complete available summary baseline even when its write range starts later. Its cumulative series continue through the latest source day because a corrected increment changes every later total; measurement series honor the requested end date.

Before reading Recorder metadata and seeds, the importer waits for previously queued Recorder work. This preserves ordering after another import or rebuild, including work submitted by an earlier entry runtime. Existing detailed stage/accessory statistic IDs participate in both construction and seed selection even when corrected source rows are all zero. The metadata query is limited to possible detailed IDs for configured thermostats; it does not create series for hardware never observed or imported.

Point requests are divided into bounded windows. Recoverable failures, including oversized responses and server errors, recursively split a window until an unsatisfied window is at most one day, then skip it with evidence. Authentication failures, permanent client errors, and redirects abort without splitting. Partial success therefore remains observable: the importer records skipped-window totals by resource and at most three identifier-free examples. It never represents a skipped window as verified zero activity. The result records submitted row counts, source counts, summary mode, fallback reason, and seed count; it is not independent proof that every upstream interval existed.

Identity normalization removes duplicate source rows before aggregation; the last metadata/summary row wins, including deletion. An omitted optional counter contributes zero; an explicitly invalid counter stops its affected cumulative series. Numeric parsing and accumulation reject negative counters, non-finite or unrepresentable values, including overflow after conversion or seeding. A cumulative series also stops when its running total can no longer be represented instead of publishing a corrupted continuation. See [`test_statistics_builder.py`](../tests/test_statistics_builder.py), [`test_import_evidence.py`](../tests/test_import_evidence.py), and the real Recorder rebuild/seed coverage in [`test_filter_actions_ha.py`](../tests/test_filter_actions_ha.py).

### Qualified hourly history

The config-entry importer owns one
[`HourlyImportManager`](../custom_components/beestat_statistics/hourly_import.py),
[`HistoryWriter`](../custom_components/beestat_statistics/hourly_history_writer.py),
[Recorder adapter](../custom_components/beestat_statistics/hourly_recorder.py), and
[versioned journal](../custom_components/beestat_statistics/hourly_storage.py).
Scheduled imports, manual imports, rebuilds and explicit admission share the
importer's existing lock. There is no second scheduled writer.

Contract 3 is the supported hourly floor. Earlier cumulative hourly selection,
segment recovery and cached coverage actions are retired. Older hourly journals
and roots retaining a v2 writer fail closed; use v2026.10.11 for an older
cumulative-hourly journal; this release does not migrate it. Existing Recorder rows and statistic IDs are never removed or
rewritten by this retirement. Config-entry version 1.6 and ongoing registry
reconciliation remain unchanged.

An entry retains daily imports until explicit `plan_hourly_history` and
`apply_hourly_history` admission. Each admitted physical quantity reserves its
legacy aliases immediately, including while its operation is pending or blocked.
Other enabled quantities continue daily imports, including VOC whose hourly unit
contract is unresolved. Renaming, disabling or removing an admitted quantity
cannot reopen its daily writer. Reusing a reserved alias or changing its source
parent fails closed. Daily summaries remain available to the filter engine.

The [hourly history contract](hourly-history-v3.md) owns native representation,
logical units, source qualification, bounded queries, paging, admission and
recovery. Runtime and degree-day amounts use independent arithmetic-mean rows
rather than cumulative counters. Missing intervals cannot corrupt later totals.
Measurements preserve genuine means and extrema. Every qualified hour requires
twelve valid distinct five-minute samples, a closed observed source horizon,
committed proof and matching bounded Recorder readback. Source conflicts and
unplaceable timestamps remain explicit blockers, never zero observations.

Each entry has one native `HourlyStore` journal at
`.storage/beestat_statistics.<entry_id>.hourly_import`. It holds stable physical
selections, bounded source/proof references, operation progress and at most one
pending quantity/month intent. An entry-data `hourly_import_contract` marker
binds version 3 and its adoption token. Atomic Store writes are verified by
uncached disk readback; queued Recorder submissions are verified by a complete
bounded native snapshot before proof publication. Prepared adoption and exact
intent replay survive interruption without duplicate increments. Source identity,
unit, marker, journal or native conflicts block unverified effects.

The manager drains earlier tracked save tasks before loading a replacement
runtime. A durable invalidation fence rejects an obsolete root even if its old
marker is restored. Unload stops admission; surviving storage and Recorder effects
must be reconciled. Consistent backup/recovery includes entry data, journal,
referenced immutable objects and Recorder history.

Routine acquisition retains original staged sources and compact integration
deltas. Fully superseded deltas can retire only when the merged observation,
conflict, blocked-window and horizon resolution digest remains identical.
Each thermostat retains a deterministic horizon witness, preferring an
acquisition already needed for its observations. Protected sources, reviewed
orders, out-of-window observations and conflicts remain retained. Collection
runs only after the committed root no longer reaches the retired objects and
pauses for backups.

`get_configuration` exposes mode, revision, operation and quantity descriptors.
`get_hourly_coverage` requires contract version 3 and reads qualified logical
history without provider fetches, reconciliation or writes. Hourly queries omit
legacy reads; daily queries may use one qualified closed legacy day as fallback.
Projection and view-token hashing use detached worker inputs, followed by
configuration, identity, timezone and root-revision checks before returning.

`get_raw_points` is a separate read-only admin action through the runtime-owned
client. Explicit configured resource IDs and offset-aware whole-second bounds
are checked before and after its bounded read under the importer lock. It uses
only fixed point-read methods, with no synchronization, selection or writes.
The response preserves provider order, deletion rows and original fields, with a
bounded acquisition receipt. Transport completion never establishes provider
sample completeness or settlement. Raw history is private and excluded from
diagnostics.

[Builder tests](../tests/test_hourly_statistics.py),
[writer recovery tests](../tests/test_hourly_history_writer.py),
[native Recorder tests](../tests/test_hourly_recorder_ha.py) and
[native history tests](../tests/test_hourly_history_writer_ha.py) establish these
source contracts using synthetic data. They do not prove installation or a
particular historical repair.

## Filter observation and action contracts

Filter exposure is observed fan runtime, not a measurement of filter condition. Baseline selection is deterministic: local exact timestamp, local date override, mapped helper date, then Beestat date. [`filter_runtime.py`](../custom_components/beestat_statistics/filter_runtime.py) combines one bounded raw replacement day with later daily summaries. It counts only complete five-minute intervals after an exact replacement time; it does not prorate the crossing bucket. Missing intervals, boundary uncertainty, and the still-unreported tail remain explicit uncertainty. Date-only input omits the ambiguous replacement day.

The recent rate uses complete past local days, excluding the current day and missing, malformed, or incomplete days. [`filter_forecast.py`](../custom_components/beestat_statistics/filter_forecast.py) owns the shared forecast and quality projection. Observed runtime rounds down; remaining runtime can consequently be an upper bound. The runtime due date is a projection until observed exposure establishes the threshold. The calendar maximum-age limit is independent.

The due state is three-valued. It is true when the calendar deadline has arrived or observed exposure reaches the threshold. It is false only when the observed amount plus bounded unknown exposure still proves the threshold has not been reached. Otherwise it is unknown. A projected due date or due-soon value does not convert uncertain exposure into a definite not-due result. Due soon independently follows the projected date and notice window, including the due date and overdue days. Unknown exposure is a valid projection, distinct from an unavailable coordinator or missing forecast. Quality attributes and `forecast_revision` come from the same forecast contract across native surfaces; elapsed-only unreported-tail telemetry does not by itself change the revision, but a changed due state does. `earliest_due_date` derives from that elapsed-inclusive unknown interval, so it is a quality attribute outside the revision: the earlier of the calendar date and the runtime date recomputed with the whole unknown interval as fan runtime, unknown when that interval is unbounded.

The button records its actual aware click time. `record_filter_change` accepts the actual replacement time and an optimistic guard containing the prior timestamp, date, and request ID. [`entry_options.py`](../custom_components/beestat_statistics/entry_options.py) verifies the guard and updates entry options with the boundary plus the latest provenance event before its first await. Home Assistant schedules those options for disk persistence; the response does not prove that write has completed. Alert acknowledgement and cloud reconciliation happen afterward. A failed refresh does not undo the recorded replacement.

An identical replay of the currently saved request returns `already_recorded` without repeating effects, even after the new-request age limit. Reusing that ID for different content fails. A later correction changes the guard, preventing an older request from gaining authority from an unchanged date. If another action supersedes the request during awaited follow-up, the response reports `superseded`. The single retained [`FilterChangeEvent`](../custom_components/beestat_statistics/filter_action.py) records provenance, not a complete history or proof that physical work occurred.

Date edits and boundary repair are corrections; neither acknowledges alerts. Repair refines an existing date and checks the persisted boundary. Exact input rejects ambiguous or nonexistent naive local times. Reconciliation rechecks the current timestamp and timezone revision after I/O before writing; it retains only the affected raw local day and periodically refreshes it to catch source corrections. A separate effect timer retries recent pending boundaries. Persisted reconciliation status and current source coverage remain distinct: an earlier finalized boundary does not prove complete exposure today. Action schemas and operational examples live in [Usage](usage.md) and [`services.yaml`](../custom_components/beestat_statistics/services.yaml).

## Transport, privacy, and verification boundaries

Response JSON decoding, raw receipt validation, and routine history serialization
run outside the event loop. Workers own detached inputs and cannot submit Recorder
effects. Routine history capture rechecks its context after serialization before
continuing. Exhausted retry diagnostics distinguish an unsuccessful API envelope
from a false synchronization result, include attempt count and final HTTP status,
and expose only recognized integer provider error codes (1000, 1003, 1005, 1505).
They exclude provider messages and response bodies; retry and authentication
behavior is unchanged.

[`api.py`](../custom_components/beestat_statistics/api.py) is the only cloud transport. It uses the Home Assistant client session, an HTTPS endpoint validated by [`url_validation.py`](../custom_components/beestat_statistics/url_validation.py), disabled redirects, bounded responses, per-attempt timeouts, and bounded retries. Authentication failures and permanent client errors stop promptly. Supported resource/method wrappers and the [reviewed API inventory](beestat-api-surface.json) define the API surface; the upstream checker is a drift signal, not authenticated runtime verification.

Local filter records and Recorder imports are separate effects from Beestat sync and matching-filter-alert dismissal. The integration does not write Ecobee thermostat settings. [`thermostat_settings.py`](../custom_components/beestat_statistics/thermostat_settings.py) projects explicit allowed fields from potentially sensitive source objects; missing optional settings remain unavailable. Typed absolute-temperature setting sensors use the same source-qualified lower bound after tenths-Fahrenheit scaling and retain one-decimal presentation rounding; signed corrections and differences retain their separate semantics. Raw settings context and profile `heatTemp`/`coolTemp` fields remain source context, without assuming an unqualified profile unit.

[`configuration.py`](../custom_components/beestat_statistics/configuration.py) returns detached, non-secret saved/effective configuration and allowed cached source details without I/O. It can contain local names and mappings and is not a sanitized support bundle. [`diagnostics.py`](../custom_components/beestat_statistics/diagnostics.py) instead builds an allowlisted aggregate, redacts identifiers, connection details, account anchors, filter dates, and schedule details, and avoids exporting raw source/config dictionaries. Detailed filter provenance and forecast attributes are current-state data excluded from Recorder history. Errors use redacted text or bounded fingerprints; exception tracebacks must not become a credential channel.

Pure tests establish parsing, identity, calculations, and privacy invariants; Home Assistant tests establish config-entry, registry, entity, timer, action, and Recorder behavior. [Development](development.md) owns commands and the support floor. The [validation workflow](../.github/workflows/validate.yaml) combines unit/static, Core, Hassfest, and HACS jobs into its release gate. Passing those checks establishes the tested source candidate; live account behavior and deployment remain separate evidence.

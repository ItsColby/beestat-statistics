# Architecture

Beestat Statistics brings Beestat history and context into Home Assistant. It owns acquisition from Beestat, external Recorder statistics, its own entities, and locally recorded filter boundaries. Beestat supplies source history; Home Assistant's existing HomeKit/Ecobee integrations own their devices and live controls. This integration reads those local registrations and selected states to associate context with the right equipment. Equipment-specific limits, physical maintenance decisions, notifications, tasks, and complete maintenance history belong to users and their automation systems. Software filter defaults are not manufacturer recommendations.

This reference explains contracts that a maintainer must preserve. See [Usage](usage.md) for configuration and actions, [Development](development.md) for validation, and [the README](../README.md) for installation.

## Runtime ownership and data flow

One config entry contains a typed [`BeestatStatisticsRuntime`](../custom_components/beestat_statistics/runtime.py): an API client, coordinator, importer, and configured import interval. [`__init__.py`](../custom_components/beestat_statistics/__init__.py) owns setup, service dispatch, Recorder writes, registry listeners, and unload. The manifest declares a cloud-polling hub with Recorder as a dependency; HomeKit is an optional ordering dependency.

The normal flow is:

1. The client requests Beestat synchronization, then reads thermostat, sensor, and summary resources. An optional `ecobee_thermostat` read supplies allowlisted settings; its failure leaves that projection unavailable without discarding primary runtime data.
2. The coordinator normalizes resource identities and builds one `BeestatRuntimeData` snapshot containing effective configuration, source rows, runtime observations, metadata, settings, and room-temperature spread.
3. Entity platforms project that snapshot. Registry and mapped-state events can rebuild relevant cached projections; entity property reads perform no network requests.
4. The importer obtains recent point history, builds daily statistics, and submits them through Home Assistant's external-statistics API. Recorder remains the history owner.

Runtime refreshes and imports have separate locks. Scheduled import requests coalesce to one running pass plus one pending follow-up through [`task_coalescer.py`](../custom_components/beestat_statistics/task_coalescer.py). This bounds repeated timer/helper events without dropping the need to reconcile current state. Entry-owned background tasks, listeners, and timers stop on unload; retained coordinator/importer references reject new work. The startup import reuses the initial refresh by skipping another sync request.

## Identity and configuration

Connection data, mutable options, source identity, and presentation names have different lifetimes. [`config_payload.py`](../custom_components/beestat_statistics/config_payload.py) owns the stored representation and migration; [`config_model.py`](../custom_components/beestat_statistics/config_model.py) resolves it into effective thermostat and sensor models.

Entry data stores connection/account continuity information and may contain imported mappings. Options own timing and UI-edited mapping collections. When an options collection exists, it replaces that collection from data; it is not a field-by-field overlay. Mutation helpers preserve unrelated and unrecognized fields. [`config_rows.py`](../custom_components/beestat_statistics/config_rows.py) gives every consumer the same last-valid-row-per-positive-ID interpretation of duplicate overrides. Malformed identity rows cannot silently claim a valid resource.

The config flow allows one entry. An authenticated thermostat read supplies hashed thermostat identities for account continuity; an empty or unidentifiable response cannot establish a new account. Overlapping anchors allow an account's equipment inventory to change. A disjoint account requires confirmation, which clears thermostat and sensor mappings while preserving unrelated configuration. Recorder history remains, so reused slugs can continue an earlier account's series. Migrations do not invent authenticated identity. Connection changes from YAML also go through validation and continuity rules.

Awaited validation and preview steps cannot assume their initial entry snapshot still owns the save. Connection flows check the saved data again; account replacement checks both data and options. Same-account connection edits preserve intervening options updates. Mapping/source-scope previews are checked against current discovery and configuration before committing. A changed owner aborts or regenerates the relevant preview rather than overwriting a winning update. These races are exercised in [`test_config_flow_ha.py`](../tests/test_config_flow_ha.py).

Native entity unique IDs use numeric Beestat resource IDs plus a semantic suffix. Display names and suggested entity IDs can change without changing that identity. External Recorder statistic IDs are different: they use the effective slug under the `beestat:` source. Changing a slug therefore changes the statistic identity; stable entity IDs do not imply automatic historical-series migration.

### Mapping and physical proof

Automatic mapping is a convenience candidate process using recognized HomeKit/Ecobee registrations, names, and uniqueness. Explicit selections take priority. Ambiguous candidates or duplicate claims do not share a foreign device. An explicit mapping that is temporarily missing remains an explicit unresolved selection; automatic discovery cannot quietly replace it.

UI selections retain a stable registry reference: registry-entry UUID plus domain, platform, and unique ID. [`entity_reference.py`](../custom_components/beestat_statistics/entity_reference.py) checks the UUID against that identity, then can recover the same source by identity after registry recreation. Once a stable reference exists, reusing its old entity ID for a different source is not recovery.

A mixed HomeKit climate/Ecobee temperature mapping needs stronger proof than an ordinary naming match. [`source_identity.py`](../custom_components/beestat_statistics/source_identity.py) recognizes Ecobee's built-in `ei:0` temperature probe only when its unique ID agrees with its Ecobee device identifier and that identifier matches one unambiguous Ecobee HomeKit thermostat serial. Names, areas, aliases, and equal readings are not physical proof. Cloud-only mappings remain supported. Identity drift invalidates the mixed claim, and registry recovery reevaluates it. An unavailable selected probe is not replaced by the thermostat's potentially room-combined displayed temperature. [`test_source_identity_ha.py`](../tests/test_source_identity_ha.py) covers proof, ambiguity, duplicate claims, drift, and recovery.

Built-in Beestat sensor rows inherit their thermostat's physical identity constraints. A child override must belong to that same device or satisfy the same mixed-source proof; options validation, runtime resolution, and Repairs evaluate the effective inherited claims together.

[`entity.py`](../custom_components/beestat_statistics/entity.py) links derived entities to mapped devices through Home Assistant's helper integration API without making this config entry a co-owner of those source devices. Unmapped resources use integration-owned fallback devices. Discovery adds newly available entity identities once; relinking follows registry changes. Repairs identify missing references, invalid domains, and conflicting device claims.

## Clocks and source quality

Acquisition cadence, source age, cached projection time, and effect retries are independent. The coordinator records fetch, projection, runtime-sync, and metadata-sync timestamps separately. A successful request says when data was acquired; the thermostat's `data_end` and latest summary date say how far its source history extends.

One coordinator timer selects the earliest cached schedule transition, cloud-staleness threshold, filter uncertainty boundary, or local midnight. It rebuilds from cached rows without external I/O, then schedules the next boundary. Listener updates occur when projected values change. The filter boundary reevaluates a not-due result when elapsed unreported exposure can make it unknown. Home Assistant timezone changes increment a revision and rebuild the local projection; thermostat schedule calculation can use the thermostat's own timezone. The cloud-stale threshold allows the configured poll interval plus source grace, subject to a minimum. Constants and the exact rounded-lag boundary live in [`coordinator.py`](../custom_components/beestat_statistics/coordinator.py), not in a second configuration contract here.

Imports capture one evaluation time, timezone, and revision before preparing daily rows. A timezone change during awaited preparation restarts preparation before any Recorder submission, with a bounded number of attempts. Local days use actual UTC elapsed time across daylight-saving transitions. [`test_runtime_ha.py`](../tests/test_runtime_ha.py) exercises real timer transitions without a new source event, timezone changes, bounded import restarts, and unload cancellation.

Current comfort profile mirrors cached `currentClimateRef`; scheduled profiles project the cached program. Neither reconstructs live holds. Sensor `in_use` is Beestat's reported flag, not configured membership or momentary weighting. [`alerts.py`](../custom_components/beestat_statistics/alerts.py) keeps equipment and unknown alerts visible even alongside maintenance reminders.

Room-temperature spread combines mapped readings for identity-qualified members of that thermostat's configured profile. It converts supported explicit units and accepts only absolute `temperature` observations when a live device class is present; sources with no device-class attribute remain supported. Absolute readings below 0 K, −273.15 °C or −459.67 °F are invalid, allowing only `1e-9` degrees of floating-point roundoff without clipping. Invalid or unavailable observations reduce coverage without removing their mappings, so a later valid state recovers through local events without cloud I/O. The spread itself is a temperature difference. At least two valid readings can produce a partial range that understates the full profile spread. Observation age alone does not reject a valid local reading.

## Recorder integrity

[`statistics_builder.py`](../custom_components/beestat_statistics/statistics_builder.py) owns pure daily-series construction. Summary rows supply runtime, counters, and summary measurements; thermostat and room-sensor five-minute rows supply daily mean/min/max measurements. Runtime/counter series publish cumulative `state` and `sum`; measurement series publish arithmetic means and available extrema. Local-day timestamps, units, conversion factors, and metadata are part of that contract.

This production format has an interval limitation: Home Assistant's external
statistics import is hourly, so it represents these daily aggregates as one-hour
rows. Daily cumulative differences can still be meaningful, but hourly queries
cannot recover the day's missing intervals. The preparation described below does
not activate a repair or change the existing importer. [Core import contract](https://github.com/home-assistant/core/blob/2026.9.2/homeassistant/components/recorder/statistics.py#L2670).

Known-unit absolute temperatures share the lower-bound check in [`temperature.py`](../custom_components/beestat_statistics/temperature.py). Historical outdoor, setpoint and room-sensor values are checked after source scaling and before aggregation; summary means and each supplied extremum are checked independently. Their qualified upstream tenths-Fahrenheit representation permits a `0.05 °F` half-step allowance in addition to floating-point roundoff, preserving the rounded absolute-zero value `−459.7 °F` while rejecting `−460 °F`. This source allowance does not apply to precise local HA readings. The pinned Beestat [runtime scaling](https://github.com/beestat/app/blob/42c3b775cbb2e8a6893ac1346a8b42e3c0bd17e1/api/runtime_sensor.php#L113) and [summary rounding](https://github.com/beestat/app/blob/42c3b775cbb2e8a6893ac1346a8b42e3c0bd17e1/api/runtime_thermostat_summary.php#L345) show this representation; the [API inventory](beestat-api-surface.json) records their source identities. Invalid means omit that day's measurement, while an invalid extremum omits only that extremum. Valid negative Celsius/Fahrenheit readings and finite high temperatures remain supported.

Routine cumulative imports read an overlapping summary window and seed it from Recorder immediately before that window. Missing latest rows, missing seeds, or a failed window read cause a full-summary fallback. Seed coverage is checked against the fetched window as well as the cached inventory, so a newly discovered stage cannot restart an existing cumulative total from zero. A rebuild reads the complete available summary baseline even when its write range starts later. Its cumulative series continue through the latest source day because a corrected increment changes every later total; measurement series honor the requested end date.

Before reading Recorder metadata and seeds, the importer waits for previously queued Recorder work. This preserves ordering after another import or rebuild, including work submitted by an earlier entry runtime. Existing detailed stage/accessory statistic IDs participate in both construction and seed selection even when corrected source rows are all zero. The metadata query is limited to possible detailed IDs for configured thermostats; it does not create series for hardware never observed or imported.

Point requests are divided into bounded windows. Recoverable failures, including oversized responses and server errors, recursively split a window until an unsatisfied window is at most one day, then skip it with evidence. Authentication failures, permanent client errors, and redirects abort without splitting. Partial success therefore remains observable: the importer records skipped-window totals by resource and at most three identifier-free examples. It never represents a skipped window as verified zero activity. The result records submitted row counts, source counts, summary mode, fallback reason, and seed count; it is not independent proof that every upstream interval existed.

Identity normalization removes duplicate source rows before aggregation; the last metadata/summary row wins, including deletion. An omitted optional counter contributes zero; an explicitly invalid counter stops its affected cumulative series. Numeric parsing and accumulation reject negative counters, non-finite or unrepresentable values, including overflow after conversion or seeding. A cumulative series also stops when its running total can no longer be represented instead of publishing a corrupted continuation. See [`test_statistics_builder.py`](../tests/test_statistics_builder.py), [`test_import_evidence.py`](../tests/test_import_evidence.py), and the real Recorder rebuild/seed coverage in [`test_filter_actions_ha.py`](../tests/test_filter_actions_ha.py).

### Hourly repair preparation

[`hourly_statistics.py`](../custom_components/beestat_statistics/hourly_statistics.py)
and [`hourly_import_plan.py`](../custom_components/beestat_statistics/hourly_import_plan.py)
are pure preparation modules. Production setup, services, imports, configuration
and legacy writes do not call them. The proposed successor identity appends
`_hourly_v2` to the existing statistic suffix; legacy IDs are never renamed or
mutated by these modules. This naming contract remains subject to the separate
activation and consumer-migration decision.

The builder uses actual thermostat and sensor five-minute points for all declared
families, including the quantities currently imported from daily summaries. It
normalizes UTC instants before duplicate resolution and hour grouping, keeps the
last row at a repeated instant including deletion/invalidity, and retains coverage
for every requested hour. The conservative eligibility rule requires twelve
distinct valid slots, a closed hour and an admitted source horizon. This is an
explicit product-design tradeoff, not a native Recorder completeness guarantee.
Missing or invalid slots are never zero-filled. Malformed timestamps remain
visible as rejected source rows; the planner withholds their affected series
because their position cannot be established.

Measurement means remain arithmetic means of interval-start samples, distinct
from Recorder's time-weighted held values. Runtime increments retain Beestat's
exclusive-stage and fan semantics and convert seconds to hours. Degree-day
increments use the source's 65°F base and fixed five-minute fraction of a 24-hour
day; they do not normalize DST or missing samples. The source already normalizes
AQI to percent. VOC's trusted-unit successor is held pending authoritative unit
evidence; the existing declaration alone is insufficient. Daily summaries remain
available to the filter engine and for their explicitly daily uses.

The read-only planner requires an explicit cumulative checkpoint and a complete
supplied Recorder snapshot, including successor metadata and every row from the
preceding hour through the latest retained row. An empty query alone does not
prove completeness. Only an exact preceding successor-hour seed can continue an
existing cumulative epoch. The checkpoint carries the epoch origin and last
verified continuous hour independently of the rolling raw-history window. An
expired raw prefix does not invalidate a trusted exact predecessor; a missing
seed cannot silently move the epoch forward. At a new epoch, the first row includes
its actual first complete hour's increment, without a fabricated zero predecessor.
The caller must establish snapshot coverage, preserve
identity/unit compatibility, serialize with the writer and revalidate before any
later effect; the pure planner neither acquires data nor proves those conditions.
Its detached results expose proposed rows, every hour's coverage, continuity
breaks, blocking reasons and surviving stale row starts. `unblocked_rows` means
only that the supplied plan has no local blocker; it grants no authority and is
not proof of a completed import.

A cumulative gap withholds the affected series' entire proposed batch. A changed
increment requires recalculating every affected retained successor total. If a
correction or deletion invalidates a previously published hour, the plan identifies
the stale measurement or cumulative suffix that an omitted upsert would leave
behind. Stopping new writes does not repair those rows. Before activation, a
separate native recovery contract must cover backup and restore, stale-row
correction/removal, raw-history loss, metadata drift, partial submissions,
Recorder completion/readback, versioned cutover and deliberate downgrade. No
automatic resets, gap bridges or new epochs are selected here. An unrecoverable
gap can therefore stall a cumulative series; that limitation must be resolved
explicitly before consumer adoption.

### Recovery and persistence contract

Use the same successor ID when actual observations and a trusted exact predecessor
can reconstruct the complete affected cumulative suffix. Recompute those totals;
an upsert of only the corrected hour leaves later totals wrong. A missing raw
prefix is acceptable only when its exact predecessor remains independently
verified. Source expiry cannot move the epoch, and a row's presence alone is not
continuity proof. These rules apply separately to each quantity.

An unrecoverable gap stops that cumulative segment. The recovery choice is an
explicitly selected new segment with its own immutable ID and epoch, preserving
the old segment. Routine refresh never allocates another ID. A missing hour is
first a recoverable hold; expiry or a failed request alone does not approve a
segment transition. Preview must identify the first incomplete hour, the proposed
complete starting hour, retained rows to invalidate and affected consumers before
the separate activation decision. Repeated permanent gaps require a new deliberate
decision rather than an automatic chain of IDs.

Bind a selected segment once to the existing adopted series identity, source
account/resource identity, quantity/unit contract and whole UTC start. A future
segment ID uses the adopted base ID plus `_eYYYYMMDDtHHMMSSz`; persisted selection
owns this exact ID across retries, reloads and display-name changes. A conflicting
existing ID or metadata blocks adoption. The initial `_hourly_v2` IDs remain the
first segment; the preparation builder and planner do not allocate or route later
segment IDs. That integration remains a subsequent source change.

The first row of each new segment contains the first observed hour's increment.
There is no synthetic preceding zero row. Native hour/day changes within that
segment describe its covered observations, including a partial first day; a daily
timestamp does not establish a complete day. Consumers must select the active
segment explicitly and retain its coverage boundary. They must not splice segments
into an apparently continuous total or describe their sum across a gap as complete
runtime. Measurement series can resume under the same ID after missing hours, but
their native daily means likewise describe only the remaining observations.

Start-only upserts provide the native path to invalidate stale numeric
values without removing a statistic's identity. A measurement correction targets
its invalid hour; a cumulative correction targets every stale row from the first
invalid hour through the retained end. The complete snapshot must include all
metadata-supported numeric fields and every retained row in that suffix. The pure
planner recognizes
fully null rows as cleared, distinct from partially populated malformed rows.
Cleared rows are never cumulative seeds and do not restore source coverage. Native
nulls are not a general missing-duration marker: a same-ID counter resumed after a
null can yield different changes depending on the query's start. Carry-forward,
resetting `sum`/`state`, or adding `last_reset` therefore cannot establish a truthful
continuous recovery contract.

At activation, the existing config-entry importer owns one versioned Home
Assistant `Store` document, keyed by the entry and hourly-import contract. No
parallel options fields, helper entities or external ledger are needed. Persist:

- the adopted account/resource and quantity/unit identities, immutable segment
  IDs/epochs, selected active segment and closed-segment boundaries;
- the last native-verified continuous hour and its exact state/sum, independently
  of fetch-window bounds, plus the earliest unresolved invalidation boundary;
- a revision and at most one pending series batch containing detached expected
  prior values/absence, intended values or clearing operations, compatible metadata,
  predecessor, target range and a deterministic digest of that complete intent.

Serialize preparation, Store updates, Recorder submission and reconciliation under
the config-entry writer. Close admission on unload; a replacement runtime first
drains/reconciles pending work. Recorder and Store do not form a shared transaction:

1. Acquire and verify the native snapshot and identity against the saved revision.
   On discovering an invalid historical hour, durably lower trusted continuity and
   record the invalidation boundary before any clearing or further import. Persist
   the exact pending intent before enqueueing its first native effect. Failure to
   save means no submission.
2. Submit the captured batch. Return from submission proves queue acceptance only.
   Do not advance continuity or report completed repair until the Recorder barrier
   and exact native readback succeed. Later source corrections cannot mutate the
   pending payload.
3. After cancellation, restart or uncertain completion, drain surviving Recorder
   work and compare each target plus metadata/predecessor with its expected states.
   Exact intended rows are already applied; exact prior rows/absence may be retried
   with the identical intent after that reconciliation. Any third state is a
   conflict and stops the batch. A mixed result is not permission to recompute from
   newer source data or overwrite an unknown writer.
4. When all intended rows and required continuity are verified, advance the
   checkpoint and remove the pending intent in one Store save. A crash before that
   save is recovered by the same readback, without a new epoch or additive total.
   Close an old segment only after its stale suffix is reconciled; adopt its explicit
   successor only with the selected consumer transition.

The existing 366-day calculation bound also bounds one pending series batch.
Oversized repairs require a separately specified bounded recovery plan; they cannot
silently truncate the snapshot or stale suffix. Missing, corrupt, future-version or
independently restored Store state blocks continuation until identities, epochs,
native rows and pending effects are reconciled. Never infer replacement state from
the oldest fetchable point or latest Recorder row. Deliberate downgrade must retain
this state and prevent the old daily writer from targeting hourly successor IDs.
This section specifies future persistence and writer behavior; neither is activated
by the pure modules or the isolated native tests.

An hourly runtime consumer needs the verified active statistic/segment, UTC hour
starts, values in hours, and per-hour coverage for its requested window. A rolling
24-hour chart can prioritize fan runtime, scale its hours axis to the plotted
values, and show an average across the observed complete hours with that count
explicit. Missing or invalid hours cannot become zeros, and a planned row cannot
stand in for a verified import. The open current hour is expected latency; warnings
should identify an actual missing, invalid, stale or incompatible source affecting
the chart. A pending invalidation must suppress the affected display before stale
native rows finish clearing. Segment boundaries and incomplete window coverage
remain visible context without presenting every normal refresh as a warning.
The activation phase must provide this coverage projection; native statistic rows
alone do not carry it.

The focused tests in [`test_hourly_statistics.py`](../tests/test_hourly_statistics.py)
and [`test_hourly_import_plan.py`](../tests/test_hourly_import_plan.py) exercise
the proposed calculation and reconciliation behavior without Home Assistant.
[`test_hourly_recorder_ha.py`](../tests/test_hourly_recorder_ha.py) exercises the
same builder/planner through real external imports and native hour/day readback in
disposable synthetic Recorder instances. Its cases cover first-hour increments,
partial days, cleared rows, gap/reset counterexamples, segment boundaries, queued
completion/replay, trusted native seeds and corrected suffixes. These checks prove
the tested Recorder contracts. The future Store writer, persistence failure/restart
paths, consumer adoption, historical repair and rollout still need their own
implementation and validation.

## Filter observation and action contracts

Filter exposure is observed fan runtime, not a measurement of filter condition. Baseline selection is deterministic: local exact timestamp, local date override, mapped helper date, then Beestat date. [`filter_runtime.py`](../custom_components/beestat_statistics/filter_runtime.py) combines one bounded raw replacement day with later daily summaries. It counts only complete five-minute intervals after an exact replacement time; it does not prorate the crossing bucket. Missing intervals, boundary uncertainty, and the still-unreported tail remain explicit uncertainty. Date-only input omits the ambiguous replacement day.

The recent rate uses complete past local days, excluding the current day and missing, malformed, or incomplete days. [`filter_forecast.py`](../custom_components/beestat_statistics/filter_forecast.py) owns the shared forecast and quality projection. Observed runtime rounds down; remaining runtime can consequently be an upper bound. The runtime due date is a projection until observed exposure establishes the threshold. The calendar maximum-age limit is independent.

The due state is three-valued. It is true when the calendar deadline has arrived or observed exposure reaches the threshold. It is false only when the observed amount plus bounded unknown exposure still proves the threshold has not been reached. Otherwise it is unknown. A projected due date or due-soon value does not convert uncertain exposure into a definite not-due result. Due soon independently follows the projected date and notice window, including the due date and overdue days. Unknown exposure is a valid projection, distinct from an unavailable coordinator or missing forecast. Quality attributes and `forecast_revision` come from the same forecast contract across native surfaces; elapsed-only unreported-tail telemetry does not by itself change the revision, but a changed due state does.

The button records its actual aware click time. `record_filter_change` accepts the actual replacement time and an optimistic guard containing the prior timestamp, date, and request ID. [`entry_options.py`](../custom_components/beestat_statistics/entry_options.py) verifies the guard and saves the boundary plus the latest provenance event before its first await. Alert acknowledgement and cloud reconciliation happen afterward. A failed refresh cannot undo the durable replacement record.

An identical replay of the currently saved request returns `already_recorded` without repeating effects, even after the new-request age limit. Reusing that ID for different content fails. A later correction changes the guard, preventing an older request from gaining authority from an unchanged date. If another action supersedes the request during awaited follow-up, the response reports `superseded`. The single retained [`FilterChangeEvent`](../custom_components/beestat_statistics/filter_action.py) records provenance, not a complete history or proof that physical work occurred.

Date edits and boundary repair are corrections; neither acknowledges alerts. Repair refines an existing date and checks the persisted boundary. Exact input rejects ambiguous or nonexistent naive local times. Reconciliation rechecks the current timestamp and timezone revision after I/O before writing; it retains only the affected raw local day and periodically refreshes it to catch source corrections. A separate effect timer retries recent pending boundaries. Persisted reconciliation status and current source coverage remain distinct: an earlier finalized boundary does not prove complete exposure today. Action schemas and operational examples live in [Usage](usage.md) and [`services.yaml`](../custom_components/beestat_statistics/services.yaml).

## Transport, privacy, and verification boundaries

[`api.py`](../custom_components/beestat_statistics/api.py) is the only cloud transport. It uses the Home Assistant client session, an HTTPS endpoint validated by [`url_validation.py`](../custom_components/beestat_statistics/url_validation.py), disabled redirects, bounded responses, per-attempt timeouts, and bounded retries. Authentication failures and permanent client errors stop promptly. Supported resource/method wrappers and the [reviewed API inventory](beestat-api-surface.json) define the API surface; the upstream checker is a drift signal, not authenticated runtime verification.

Local filter records and Recorder imports are separate effects from Beestat sync and matching-filter-alert dismissal. The integration does not write Ecobee thermostat settings. [`thermostat_settings.py`](../custom_components/beestat_statistics/thermostat_settings.py) projects explicit allowed fields from potentially sensitive source objects; missing optional settings remain unavailable. Typed absolute-temperature setting sensors use the same source-qualified lower bound after tenths-Fahrenheit scaling and retain one-decimal presentation rounding; signed corrections and differences retain their separate semantics. Raw settings context and profile `heatTemp`/`coolTemp` fields remain source context, without assuming an unqualified profile unit.

[`configuration.py`](../custom_components/beestat_statistics/configuration.py) returns detached, non-secret saved/effective configuration and allowed cached source details without I/O. It can contain local names and mappings and is not a sanitized support bundle. [`diagnostics.py`](../custom_components/beestat_statistics/diagnostics.py) instead builds an allowlisted aggregate, redacts identifiers, connection details, account anchors, filter dates, and schedule details, and avoids exporting raw source/config dictionaries. Detailed filter provenance and forecast attributes are current-state data excluded from Recorder history. Errors use redacted text or bounded fingerprints; exception tracebacks must not become a credential channel.

Pure tests establish parsing, identity, calculations, and privacy invariants; Home Assistant tests establish config-entry, registry, entity, timer, action, and Recorder behavior. [Development](development.md) owns commands and support lanes. The [validation workflow](../.github/workflows/validate.yaml) combines unit/static, minimum/current Core, Hassfest, and HACS jobs into its release gate. Passing those checks establishes the tested source candidate; live account behavior and deployment remain separate evidence.

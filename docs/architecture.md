# Architecture

Beestat Statistics brings Beestat history and context into Home Assistant. It owns acquisition from Beestat, external Recorder statistics, its own entities, and locally recorded filter boundaries. Beestat supplies source history; Home Assistant's existing HomeKit/Ecobee integrations own their devices and live controls. This integration reads those local registrations and selected states to associate context with the right equipment. Equipment-specific limits, physical maintenance decisions, notifications, tasks, and complete maintenance history belong to users and their automation systems. Software filter defaults are not manufacturer recommendations.

This reference explains contracts that a maintainer must preserve. See [Usage](usage.md) for configuration and actions, [Development](development.md) for validation, and [the README](../README.md) for installation.

## Runtime ownership and data flow

One config entry contains a typed [`BeestatStatisticsRuntime`](../custom_components/beestat_statistics/runtime.py): an API client, coordinator, importer, and configured import interval. [`__init__.py`](../custom_components/beestat_statistics/__init__.py) owns setup, service dispatch, Recorder writes, registry listeners, and unload. The manifest declares a cloud-polling hub with Recorder as a dependency; HomeKit is an optional ordering dependency.

The normal flow is:

1. The client requests Beestat synchronization, then reads thermostat, sensor, and summary resources. An optional `ecobee_thermostat` read supplies allowlisted settings; its failure leaves that projection unavailable without discarding primary runtime data.
2. The coordinator normalizes resource identities and builds one `BeestatRuntimeData` snapshot containing effective configuration, source rows, runtime observations, metadata, settings, and room-temperature spread.
3. Entity platforms project that snapshot. Registry and mapped-state events can rebuild relevant cached projections; entity property reads perform no network requests.
4. The importer resolves the entry's statistics mode under its existing lock. Entries without an hourly selection retain daily imports. Explicitly adopted entries reconcile any pending hourly batch before acquiring new point history, then build and verify hourly statistics through Home Assistant's external-statistics API. Recorder remains the history owner.

Runtime refreshes and imports have separate locks. Scheduled import requests coalesce to one running pass plus one pending follow-up through [`task_coalescer.py`](../custom_components/beestat_statistics/task_coalescer.py). This bounds repeated timer/helper events without dropping the need to reconcile current state. Entry-owned background tasks, listeners, and timers stop on unload; retained coordinator/importer references reject new work. The startup import reuses the initial refresh by skipping another sync request.

## Identity and configuration

Connection data, mutable options, source identity, and presentation names have different lifetimes. [`config_payload.py`](../custom_components/beestat_statistics/config_payload.py) owns the stored representation and migration; [`config_model.py`](../custom_components/beestat_statistics/config_model.py) resolves it into effective thermostat and sensor models.

Entry data stores connection/account continuity information and may contain imported mappings. Options own timing and UI-edited mapping collections. When an options collection exists, it replaces that collection from data; it is not a field-by-field overlay. Mutation helpers preserve unrelated and unrecognized fields. [`config_rows.py`](../custom_components/beestat_statistics/config_rows.py) gives every consumer the same last-valid-row-per-positive-ID interpretation of duplicate overrides. Malformed identity rows cannot silently claim a valid resource.

The config flow allows one entry. An authenticated thermostat read supplies hashed thermostat identities for account continuity; an empty or unidentifiable response cannot establish a new account. Overlapping anchors allow an account's equipment inventory to change. A disjoint account requires confirmation, which clears thermostat and sensor mappings while preserving unrelated configuration. Recorder history remains, so reused slugs can continue an earlier account's series. Migrations do not invent authenticated identity. Connection changes from YAML also go through validation and continuity rules.

Awaited validation and preview steps cannot assume their initial entry snapshot still owns the save. Connection flows check the saved data again; account replacement checks both data and options. Same-account connection edits preserve intervening options updates. Mapping/source-scope previews are checked against current discovery and configuration before committing. A changed owner aborts or regenerates the relevant preview rather than overwriting a winning update. These races are exercised in [`test_config_flow_ha.py`](../tests/test_config_flow_ha.py).

Native entity unique IDs use numeric Beestat resource IDs plus a semantic suffix. Display names and suggested entity IDs can change without changing that identity. Legacy external Recorder statistic IDs use the effective slug under the `beestat:` source, so changing a slug changes their statistic identity. Explicit hourly adoption binds the initial statistic ID to the source resource and quantity; later display-name or slug changes retain that adopted ID. Neither path automatically migrates legacy history.

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
cannot recover the day's missing intervals. The explicit hourly mode described
below supplies actual hourly rows without rewriting those legacy IDs. Installing
this source does not select an epoch or migrate an existing entry.
[Core import contract](https://github.com/home-assistant/core/blob/2026.9.2/homeassistant/components/recorder/statistics.py#L2670).

Known-unit absolute temperatures share the lower-bound check in [`temperature.py`](../custom_components/beestat_statistics/temperature.py). Historical outdoor, setpoint and room-sensor values are checked after source scaling and before aggregation; summary means and each supplied extremum are checked independently. Their qualified upstream tenths-Fahrenheit representation permits a `0.05 °F` half-step allowance in addition to floating-point roundoff, preserving the rounded absolute-zero value `−459.7 °F` while rejecting `−460 °F`. This source allowance does not apply to precise local HA readings. The pinned Beestat [runtime scaling](https://github.com/beestat/app/blob/42c3b775cbb2e8a6893ac1346a8b42e3c0bd17e1/api/runtime_sensor.php#L113) and [summary rounding](https://github.com/beestat/app/blob/42c3b775cbb2e8a6893ac1346a8b42e3c0bd17e1/api/runtime_thermostat_summary.php#L345) show this representation; the [API inventory](beestat-api-surface.json) records their source identities. Invalid means omit that day's measurement, while an invalid extremum omits only that extremum. Valid negative Celsius/Fahrenheit readings and finite high temperatures remain supported.

Routine cumulative imports read an overlapping summary window and seed it from Recorder immediately before that window. Missing latest rows, missing seeds, or a failed window read cause a full-summary fallback. Seed coverage is checked against the fetched window as well as the cached inventory, so a newly discovered stage cannot restart an existing cumulative total from zero. A rebuild reads the complete available summary baseline even when its write range starts later. Its cumulative series continue through the latest source day because a corrected increment changes every later total; measurement series honor the requested end date.

Before reading Recorder metadata and seeds, the importer waits for previously queued Recorder work. This preserves ordering after another import or rebuild, including work submitted by an earlier entry runtime. Existing detailed stage/accessory statistic IDs participate in both construction and seed selection even when corrected source rows are all zero. The metadata query is limited to possible detailed IDs for configured thermostats; it does not create series for hardware never observed or imported.

Point requests are divided into bounded windows. Recoverable failures, including oversized responses and server errors, recursively split a window until an unsatisfied window is at most one day, then skip it with evidence. Authentication failures, permanent client errors, and redirects abort without splitting. Partial success therefore remains observable: the importer records skipped-window totals by resource and at most three identifier-free examples. It never represents a skipped window as verified zero activity. The result records submitted row counts, source counts, summary mode, fallback reason, and seed count; it is not independent proof that every upstream interval existed.

Identity normalization removes duplicate source rows before aggregation; the last metadata/summary row wins, including deletion. An omitted optional counter contributes zero; an explicitly invalid counter stops its affected cumulative series. Numeric parsing and accumulation reject negative counters, non-finite or unrepresentable values, including overflow after conversion or seeding. A cumulative series also stops when its running total can no longer be represented instead of publishing a corrupted continuation. See [`test_statistics_builder.py`](../tests/test_statistics_builder.py), [`test_import_evidence.py`](../tests/test_import_evidence.py), and the real Recorder rebuild/seed coverage in [`test_filter_actions_ha.py`](../tests/test_filter_actions_ha.py).

### Explicit hourly imports

The existing config-entry importer owns
[`HourlyImportManager`](../custom_components/beestat_statistics/hourly_import.py).
It calls the pure
[hourly builder](../custom_components/beestat_statistics/hourly_statistics.py) and
[planner](../custom_components/beestat_statistics/hourly_import_plan.py), the
[Recorder adapter](../custom_components/beestat_statistics/hourly_recorder.py), and
the [versioned journal](../custom_components/beestat_statistics/hourly_storage.py).
Scheduled imports, manual imports, rebuilds and explicit selections share the
importer's existing lock. There is no second scheduled writer.

An entry stays in legacy daily mode until an explicit
`select_hourly_statistics` selection is applied. Selection stops all legacy
statistics writes for that entry and admits only its selected hourly quantities.
It does not rename, rewrite or remove legacy history. New resources and newly
available quantities require another deliberate selection; routine refresh does
not adopt them. Daily summaries remain available to the filter engine and other
explicitly daily uses.

The builder uses actual thermostat and sensor five-minute points for all declared
families, including quantities previously imported from daily summaries. The
hourly acquisition path preserves source order and tombstones through duplicate
resolution: the last normalized resource/UTC-instant row wins, including deletion
or invalidity. Every requested hour retains coverage evidence. An eligible hour
has twelve distinct valid slots, is closed, and falls within the admitted observed
source horizon. Missing or invalid slots are never zero-filled. Malformed
timestamps remain rejected source rows and hold their affected series because
their position cannot be established.

Measurement means are arithmetic means of interval-start samples, distinct from
Recorder's time-weighted held values. Runtime increments retain Beestat's
exclusive-stage and fan semantics and convert seconds to hours. Degree-day
increments use the source's 65°F base and fixed five-minute fraction of a 24-hour
day; they do not normalize DST or missing samples. AQI is already normalized to
percent. Humidity and AQI require values in 0–100, and CO2 cannot be negative.
VOC hourly adoption remains blocked pending authoritative unit evidence; the
legacy unit declaration alone is insufficient.

The planner requires a complete supplied Recorder snapshot: compatible metadata,
all supported numeric fields, and every retained row from the exact preceding
hour through the latest retained tail. An authoritative empty result is distinct
from missing metadata, an incomplete query, a populated row and a cleared row.
The adapter fences queued Recorder work before reading. A cumulative continuation
requires the exact saved predecessor row to match Recorder, plus the independently
saved last verified checkpoint. An expired raw prefix does not move the epoch;
a missing or changed predecessor/checkpoint blocks continuation. At a new epoch,
the first row includes its actual first complete hour's increment, without a
fabricated zero predecessor. A locally unblocked plan is still only proposed data.

### Segments and native recovery

Use the same adopted ID when actual observations and a trusted exact predecessor
can reconstruct the complete affected cumulative suffix. Recompute every affected
retained total; changing only the corrected hour leaves later totals wrong.
An observed source gap holds the cumulative batch. A contiguous trailing run of
provisional hours beyond the observed horizon does not hold its complete prefix:
the writer can publish that prefix and advance its checkpoint under the same
epoch. The unobserved tail stays provisional and contributes no values or hours
to the observed average. A provisional hour followed by a non-provisional hour
still breaks continuity. The first uncalculated hour also remains the native
suffix boundary, so previously populated rows within or beyond a provisional tail
must be reconciled before the prefix can advance. When stale values survive, the
writer records the invalidation boundary and clears the complete affected suffix,
including retained rows beyond the fetched source window. It never silently
truncates that suffix. Measurement corrections invalidate their affected hours.

An unrecoverable gap can stop a cumulative segment. Resumption requires an
explicit later complete epoch and a distinct segment ID. The initial ID appends
`_hourly_v2` to the legacy suffix; later IDs append
`_eYYYYMMDDtHHMMSSz` to that adopted base, with UTC minutes and seconds both zero.
The saved selection binds the exact ID, epoch, account anchors, source resource
and quantity/unit contract. Renamed slugs cannot allocate a second owner for the
same adopted quantity. Matching unowned Recorder metadata is still an identity
collision and blocks selection.

A selection preview identifies target IDs, the observed first hour, units,
resource bindings, closed boundaries, surviving stale rows and unselected
quantities. Applying requires that exact preview digest and revision. The old
segment's stale suffix must be reconciled before its new segment is admitted;
the valid old prefix and closed-segment boundary remain. Routine imports never
allocate epochs or bridge gaps.

The first row of every segment contains its observed increment. Native hour/day
changes within that segment cover only its observations, including a partial first
day; a daily timestamp does not establish a complete day. Consumers must retain
segment boundaries and must not splice segments into an apparently continuous
total. Measurement series can resume under the same ID after missing hours, but
native daily means likewise cover only the remaining observations.

Start-only upserts invalidate native numeric values while preserving statistic
identity. Fully null rows are cleared, distinct from partially populated malformed
rows; they cannot seed a counter or restore coverage. Native nulls are not a
general missing-duration marker: a same-ID counter resumed after a null can yield
different changes depending on the query's start. Carry-forward, resetting
`sum`/`state`, or adding `last_reset` cannot establish truthful continuity across
that gap. These counterexamples remain part of the
[native Recorder contract tests](../tests/test_hourly_recorder_ha.py).

### Durable intent and entry lifecycle

Each entry owns one versioned `HourlyStore` journal at the native Store path
`.storage/beestat_statistics.<entry_id>.hourly_import`. It records adopted
identities and segments, bounded per-hour verified coverage, exact continuity
checkpoints, unresolved boundaries, a revision, and at most one pending series
batch. That batch captures expected prior rows or absence, intended rows or
clearing operations, metadata, the predecessor and retained tail, and a digest
of the detached intent.

`HourlyStore` uses native atomic Store writes and then reads its exact owned path
through the uncached native JSON reader. Native `Store.async_save` can log and
swallow a `WriteError`; a normal return, cached load or pending in-memory value
does not prove durability. The adapter checks the envelope's key and versions,
then compares the disk contents with the exact intended journal. Missing files,
corrupt contents and unsupported versions remain distinct failures.

Entry data contains one `hourly_import_contract` marker with contract version and
adoption token. It establishes adoption authority; it does not mirror epochs,
coverage or checkpoints into options or entry data. Initial selection saves its
prepared journal, updates the marker through native `async_update_entry`, then
saves the selected state. The entry update uses Home Assistant's delayed native
persistence, so marker and journal writes are not one transaction. Any mismatch
blocks routine hourly and legacy writes. An exact explicit selection retry can
complete an interrupted selection or restore its missing marker; a different
selection cannot silently take over.

A present marker with a missing journal blocks continuation. A journal without
its matching marker also blocks routine continuation. If both owners are missing,
absence cannot prove that hourly adoption never happened. Restore a consistent
backup or reconcile the prior selection and Recorder state manually before
resuming; neither a fresh epoch nor the latest native row reconstructs that
authority. Installing this implementation performs no automatic migration or
existing-user epoch selection.

Recorder and the journal have their own failure boundaries:

1. Verify the saved identity/revision and complete native snapshot. Publish
   suppression as soon as invalidation is known, then persist and verify the exact
   intent before enqueueing any native effect. A failed or uncertain intent save
   permits no submission.
2. Submit the captured batch. Queue acceptance is not completed repair. Fence
   Recorder, then compare complete metadata, predecessor, target rows and retained
   tail with the journal's expected-before and intended-after states.
3. Reconcile a pending batch before acquiring fresh provider data. Exact intended
   rows are already applied; exact prior rows or absence may be retried with the
   identical payload. Partial application does not permit recomputation from
   newer source data. A metadata mismatch or any third row state stops the whole
   batch, retains its intent and suppresses unverified consumer values.
4. After complete native readback, save the new checkpoint/coverage and remove the
   pending intent together. Only that verified save counts imported rows as
   complete. A failed checkpoint save leaves the same batch recoverable through
   readback, without allocating another epoch or adding increments twice.

Unload closes admission immediately. A cancelled native save can continue, so
`hass.data` holds only outstanding per-entry save-completion tasks to drain before
a replacement generation loads its journal. Runtime, history and coverage stay
with the typed entry runtime and journal; this handoff is not a second runtime
registry. Recorder fencing handles effects that survived their cancelled waiter.
Source identity loss, incompatible units, saved-state failures and native
conflicts hold further effects and suppress unverified coverage.

One series batch and each source/coverage window are bounded to 366 elapsed days.
Oversized reconciliation requires a separately specified recovery plan; bounds
cannot conceal retained rows. Deliberate downgrade must preserve the journal and
marker and reconcile their effect before returning to a legacy writer.

### Consumer coverage and delivery boundaries

`get_configuration` exposes cached hourly mode, revision, pending/error state,
selected IDs and segment boundaries. `get_hourly_coverage` projects an explicit
whole-UTC-hour window without provider reads, imports or journal writes. Its
cached read can return while a writer is waiting for Recorder so that pending
invalidation suppresses affected values before the native rows finish clearing.

Each selected series returns its active ID, units and epoch, closed boundaries,
per-hour value/coverage and available slot counts, requested-hour count,
`complete_observed_hours`, `complete`, and `observed_hour_average`. Runtime
values are observed hourly increments in hours. The average divides the sum of
verified values by complete observed hours; missing hours do not enter the
denominator or become zeros. With no verified hours, the average is null. The
open current hour is provisional, and hours outside the active segment remain
outside its coverage. A query cannot combine closed and active segments into a
continuous total.

These are wired source capabilities, including current import status and
coverage responses. External dashboards still need to select the adopted
identities, use this coverage contract and represent missing hours and segment
boundaries. Source implementation does not establish deployment, adoption by an
existing installation or repair of its historical data. Actual provider history,
repair horizon, source/native reconciliation, backup/restore readiness and the
consumer transition remain prerequisites for a particular installation's repair.

Focused tests in [the builder suite](../tests/test_hourly_statistics.py),
[the planner suite](../tests/test_hourly_import_plan.py) and
[the writer suite](../tests/test_hourly_import.py) cover calculation, identities,
recovery and persistence failure paths. The
[native Recorder suite](../tests/test_hourly_recorder_ha.py) retains synthetic
hour/day, null, gap/reset, segment and suffix counterexamples. Those disposable
contracts do not constitute proof of an actual installation or historical repair.

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

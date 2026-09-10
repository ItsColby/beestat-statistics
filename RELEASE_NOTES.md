# Release notes

Released changes, newest first. Compatibility and validation details describe
the release in which they appear.

## Beestat Statistics v2026.9.10.3

- Stop affected cumulative statistics at explicitly invalid counters, and
  reload the full summary baseline when a corrected window introduces a stage
  without its prior Recorder total. Missing optional counters still mean zero.
- Stop subdividing history requests after permanent client errors or redirects;
  retain bounded recovery for oversized responses and server errors.
- Validate built-in sensor overrides against their thermostat's physical
  identity across configuration, runtime resolution, and Repairs.
- Update filter uncertainty when time alone invalidates a not-due result.
  Keep due-soon independent of uncertain exposure and on through the due date,
  including zero-day notice windows.
- Preserve upstream filter alerts when correcting a saved date, detach returned
  configuration details from cached state, and reject filter writes after unload.
- Replace the installation, usage, architecture, development, and native-help
  documentation with current behavior and explicit ownership contracts.
- Preserve tracked files that match ignore rules in local validation snapshots.
  Existing mappings, statistic identities, and filter limits require no migration.

## Beestat Statistics v2026.9.10.2

- Keep filter forecast revisions stable when local temperature or schedule
  updates only advance elapsed uncertainty after the latest cloud observation.
  Downstream task descriptions no longer change solely because time passed.
- Continue revising forecasts for actual source-gap corrections, coverage,
  thresholds, replacement boundaries and due-state changes. Exact current
  uncertainty remains available in diagnostic metadata.

## Beestat Statistics v2026.9.10.1

- Distinguish observed blower runtime from unobserved exposure in filter
  forecasts. Retain qualified estimates and the independent calendar limit;
  expose five-minute source precision and incomplete-day coverage.
- Calculate recent runtime from complete prior days. Refresh the filter
  boundary when source data is corrected without charging earlier runtime to
  a newer filter.
- Add `record_filter_change` for delayed replacement events with an explicit
  timestamp. Persisted date, timestamp and request-identity preconditions reject
  stale updates; exact replays are idempotent.
- Give buttons, date corrections, configuration updates and historical repair
  the same bounded event contract while keeping replacement and correction
  distinct.
- Apply date-bounded Recorder repairs to the affected cumulative tail so later
  totals do not retain an obsolete offset. Missing observations remain unknown
  and are not reconstructed.
- Run independent local validation lanes concurrently in isolated environments
  and avoid disposable bytecode and type caches. The supported minimum remains
  Home Assistant Core 2026.8.0; the tested current target for this release is
  Core 2026.9.1.

## Beestat Statistics v2026.9.10

- Add the last cloud-reported profile name, profile reference and metadata
  refresh time to room-temperature spread entities. Live temperature and local
  schedule updates preserve that cloud refresh time.
- Keep missing readings unavailable and retain probe-coverage attributes for
  interpreting partial ranges.
- Preserve entity/statistic identities, temperature calculations, import timing
  and thermostat controls without a configuration migration. Profile context
  remains diagnostic and adds no Recorder attribute churn.

## Beestat Statistics v2026.9.9

- Allow a physical Ecobee thermostat probe alongside that thermostat's HomeKit
  entities after hardware-identity verification. Keep the physical reading
  distinct from the displayed temperature, which may combine participating
  rooms.
- Recheck mappings when device identities or source associations change.
  Invalid or ambiguous mappings raise a Repair and suppress affected
  temperature projections until identity or mapping is corrected.
- Preserve HomeKit-only and Ecobee-only mappings, entity identifiers and
  Recorder statistics without a configuration migration.
- Retain the selected Ecobee probes' cloud availability and update timing.
  An unavailable probe is not replaced by the displayed temperature, and
  HomeKit control sources remain unchanged.

## Beestat Statistics v2026.9.8

- Cancel running and queued imports/refreshes on unload so stale callers cannot
  continue after shutdown.
- Read each selected sensor's history once per import and deduplicate normalized
  point identities before Recorder writes.
- Preserve saved source references and malformed or future override rows during
  configuration updates. Reject registry-induced mapping conflicts before
  confirmation.
- Prevent invalid dates and non-finite arithmetic from disrupting valid
  statistics or forecasts. Update room-spread units and filter-button
  availability with current source state.
- Classify mixed active alerts independently. Sanitize filter-date action
  failures while preserving authentication recovery.
- Label boolean configuration diagnostics as enabled settings, distinct from
  active equipment or alarm states.
- Consolidate mapping, metadata, import-plan and statistics helpers while
  preserving entity identifiers, Recorder series and configuration behavior.
- Verify upstream API snapshots against one immutable source revision; fail
  closed if public-source scanning or test discovery is incomplete.
- Validate Core 2026.8.0 and 2026.9.1 in matching isolated environments. Align
  local and hosted checks and report every failed validation lane.

## Beestat Statistics v2026.8.24

- Pin an overdue filter runtime date to the first Beestat daily summary that
  reached the configured lifetime threshold, instead of moving it to each
  current local date.
- Preserve projections before the threshold is reached, including exact Home
  Assistant filter-change runtime boundaries.
- Regression tests cover the first threshold-crossing day, exclusion of runtime
  before a click boundary and stable overdue forecasts.

## Beestat Statistics v2026.8.17

- Name the in-use count and room-level state as Beestat-reported metadata.
  It can differ from configured comfort-profile membership and Follow Me's
  momentary weighting. Use neutral sensor/thermometer icons instead of person
  icons that could imply occupancy or motion.
- Keep missing or invalid `in_use` metadata available but unknown, preventing
  incomplete rows from manufacturing an off state or zero count. A room sensor
  with no current source row remains unavailable.
- Count current `in_use: true`, non-inactive, non-deleted rows from the
  successful metadata snapshot in the aggregate.

## Beestat Statistics v2026.8.16

- Refuse redirects for credential-bearing Beestat API requests so an API-key
  query cannot be forwarded to another endpoint.
- Refuse redirects in the upstream API-surface checker so its optional GitHub
  token remains confined to approved GitHub hosts.
- Run ShellCheck in the product release harness, rather than only installing it.
- Pin the scheduled API-surface audit to Ubuntu 24.04 for reproducible behavior.

## Beestat Statistics v2026.8.15

- Expose selected privacy-safe Ecobee settings as disabled-by-default diagnostic
  entities. Retain the complete private, response-only configuration action.
- Label alert, service-reminder, Wi-Fi and microphone configuration states as
  enabled settings so they are not mistaken for active conditions.
- Use native temperature-delta semantics for correction, differential and
  current-profile room-spread values.
- Exclude static numeric configuration settings from Recorder long-term
  measurement statistics.
- Label room-temperature spread by configured comfort-profile membership while
  retaining its entity identity and compatibility attributes.

## Beestat Statistics v2026.8.14

- Resolve comfort-profile sensor identifiers within their owning thermostat.
  Reuse of an Ecobee local identifier on another thermostat no longer makes
  room-temperature spread unavailable; ambiguity within the same thermostat
  still fails closed.
- Regression tests distinguish safe cross-thermostat reuse from unsafe
  same-thermostat ambiguity.

## Beestat Statistics v2026.8.13

- Add a private, read-only configuration action with allow-listed settings,
  comfort profiles, equipment and sensor metadata. Support configuration review
  without publishing credentials or raw vendor payloads.
- Add current-profile room-temperature spread sensors. Resolve participants
  through stable Ecobee/Beestat identifiers and read their mapped local Home
  Assistant temperature entities.
- Include complete comfort-profile participation details and additional
  allow-listed settings while preserving native value types and documented
  units.
- Interpret `backlightOffTime` in seconds. Leave random-start delays without
  unit labels because their units are undocumented.
- Fail profile spread closed when a participant cannot be resolved uniquely,
  while preserving distinct sensors that share a display name.
- Tests cover stable profile joins, duplicate names, ambiguous metadata, unit
  projection, recovery and entity wiring.

## Beestat Statistics v2026.8.12

- Publish one coherent, content-revisioned forecast snapshot on the primary
  **Filter due date** entity. It contains the change boundary, runtime inputs,
  thresholds, intermediate dates, remaining days and due state, so automations
  no longer combine sequential sibling-entity updates.
- Tests cover stable content revisions, runtime-only forecast changes, exact
  change timestamps and the primary entity's authoritative snapshot wiring.

## Beestat Statistics v2026.8.11

- Import daily room-sensor occupancy percentages only when a mapped local
  occupancy entity proves that capability. Boolean Beestat samples become
  Recorder percentage mean/min/max statistics without another option or
  live-state owner.
- Import cumulative HVAC stage/accessory runtime only after a non-zero
  observation of the corresponding summary field. Preserve aggregate cool,
  heat and fan runtime series.
- Extend private, read-only `get_configuration` with allow-listed thermostat
  model, firmware, equipment-stage, property, differential and complete
  comfort-profile details. Continue excluding credentials, connection URLs,
  arbitrary source fields and raw history.
- Derive cloud staleness from one configured acquisition interval plus
  60 minutes of publication grace, with a two-hour minimum. The default
  six-hour cadence therefore uses 420 minutes and no longer flags healthy
  between-poll data as stale.
- Preserve profile heat/cool targets, fan modes, optimization, ventilation and
  sensor membership in the existing unrecorded scheduled-profile attributes.
- Tests cover cadence boundaries, allow-list exclusion, profile parsing,
  occupancy scaling, suppression of zero-only hardware detail, cumulative IDs
  and finite Recorder output.

## Beestat Statistics v2026.8.10

- Reject stale reconfigure, reauthentication, source-scope and mapping flows
  after validation or confirmation waits. Preserve the winning config-entry
  update without a stale write, reload or implicit merge; unchanged flows keep
  unknown future fields.
- Normalize duplicate thermostat, room-sensor, summary and point-history
  identities once, taking the last effective source row. Deletions and later
  restorations produce the same effective resource in runtime models, mappings,
  diagnostics, entities and Recorder imports.
- Reject non-finite or unrepresentable derived values before cached projection
  or Recorder writes. Daily means preserve representable extreme inputs;
  cumulative totals and seed offsets stop before an invalid row.
- Concurrency tests cover second-flow and external config-entry changes,
  same-account validation and account-change confirmation.
- Regressions cover duplicate rows, deletion/restoration, timestamp boundaries,
  numeric overflow and exclusion of invalid Recorder rows.

## Beestat Statistics v2026.8.9

- Make automatic HomeKit/Ecobee matching one-to-one across Beestat rows.
  Equal-confidence competitors stay unresolved, unique name matches outrank
  weaker single-device fallbacks, and explicit mappings reserve their local
  devices.
- Require each explicit mapping to use one source device; reject duplicate
  claims within thermostat or room-sensor mappings. Existing conflicts raise a
  Repair and leave affected links unresolved instead of selecting the first
  configured field or row.
- Show exact cached entity candidates before bulk confirmation. Recompute
  against current mappings/options before saving, request confirmation after
  target drift and preserve unrelated concurrent option updates.
- Rebase destructive source-scope confirmation on current options instead of a
  stale whole-options snapshot. Discovery or removal-count drift returns to
  review, including discovery while the initial selection form is open.
- Identify Home Assistant-dependent test modules by their imports for the
  dependency-light selector. Both hosted HA lanes run the complete test tree;
  missing harnesses fail collection, new HA modules cannot silently enter the
  dependency-light suite or miss hosted checks, and an empty HA set fails
  closed.
- Keep the supported Core 2026.8.0 and current-patch Core 2026.8.1 lanes
  dependency-closed with exact harness pins and a clean `pip check` before
  running the complete test tree.
- Tests cover cross-row conflicts, confidence precedence, explicit reservations
  and mapping conflicts, exact previews, initial-form/confirmation drift,
  Repairs and concurrent option updates.

## Beestat Statistics v2026.8.8

- Add an I/O-free local scheduler for cached comfort schedules, cloud-stale
  status and local-date-dependent runtime/filter forecasts. Update at their
  actual boundaries without shortening the six-hour acquisition interval.
- Follow Home Assistant timezone changes and daylight-saving transitions for
  schedule and local-midnight boundaries. Use one timezone revision throughout
  each Recorder import and retry before writing if it changes.
- Store confirmed thermostat/room mappings as stable entity-registry
  references. Survive entity-ID renames, device moves, detachments, temporary
  removal and registry recreation without mutable-name fallback.
- Discover new Beestat thermostats and room sensors after successful refreshes
  or imports while preserving explicit exclusions and entity unique IDs.
- Keep the delayed cloud comfort profile as diagnostic context. Cached
  scheduled-profile and next-transition projections do not claim the
  thermostat's live hold or operating mode.
- Rebind enrichment entities when their HomeKit/Ecobee source-device association
  changes, without co-owning the device or recreating the config entry.
- Remove projection, timezone, registry and import listeners on unload. Suppress
  unchanged projections to avoid needless Recorder state churn.
- Bound response reads, retryable transport failures, import-window splitting
  and scheduled-import coalescing. Stop retrying deterministic request failures
  while retaining bounded recovery for transient failures.
- Validate custom API URLs before client creation, guard option rollback
  against concurrent updates and preserve account/mapping continuity across
  reconfigure, reauthentication and YAML import.
- Translate setup, coordinator and action failures into bounded Home Assistant
  errors without remote response text or credential-bearing exception details.
- Tests cover updates without new source events, local midnight/DST, refresh
  rescheduling, unload cleanup, no-I/O/no-Recorder-churn behavior, stable
  mappings, bounded responses and concurrency.
- Keep the supported-minimum Core 2026.8.0 lane dependency-closed. Add a hosted
  Core 2026.8.1 patch lane that permits only the metadata-proven harness pin
  mismatch before running the complete HA tests.
- Extend public-safety, strict-typing, workflow, exception-translation and
  API-surface checks to the new lifecycle and transport contracts.

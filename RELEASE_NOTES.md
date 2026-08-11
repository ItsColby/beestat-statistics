# Beestat Statistics v2026.8.17

## Changed

- Clarify that the active-sensor count and room-level sensor-participation
  entities report Beestat metadata, which can differ from configured comfort-
  profile membership and Follow Me's momentary weighting.

# Beestat Statistics v2026.8.16

## Security

- Refuse HTTP redirects for credential-bearing Beestat API requests so an
  API-key query cannot be forwarded to a different endpoint.
- Refuse redirects in the upstream API-surface checker so an optional GitHub
  token remains confined to its approved GitHub hosts.

## Quality

- Run ShellCheck in the product-owned release harness instead of merely
  installing it.
- Pin the scheduled API-surface audit to Ubuntu 24.04 for reproducible workflow
  behavior.

# Beestat Statistics v2026.8.15

## Added

- Make selected privacy-safe Ecobee configuration settings discoverable as
  disabled-by-default diagnostic entities while retaining the complete private
  response-only configuration action.

## Changed

- Describe alert, service-reminder, Wi-Fi, and microphone configuration states
  explicitly as enabled so they cannot be mistaken for active conditions.
- Use native temperature-delta semantics for correction, differential, and
  current-profile room-spread values.
- Keep static numeric configuration settings out of Recorder long-term
  measurement statistics.

## Fixed

- Label the room-temperature spread as configured comfort-profile membership,
  while retaining its stable entity identity and compatibility attributes.

# Beestat Statistics v2026.8.14

## Fixed

- Resolve comfort-profile sensor identifiers within their owning thermostat so
  the native room-temperature spread remains available when Ecobee reuses the
  same local identifier on another thermostat.
- Continue failing closed when an identifier is ambiguous within the same
  thermostat.

## Quality

- Add regression coverage for both safe cross-thermostat identifier reuse and
  unsafe same-thermostat ambiguity.

# Beestat Statistics v2026.8.13

## Added

- Expose a private, read-only thermostat configuration action with allow-listed
  settings, comfort profiles, equipment, and sensor metadata for supported
  configuration review without publishing credentials or raw vendor payloads.
- Add native current-profile room-temperature spread sensors that resolve
  comfort-profile participants through stable Ecobee and Beestat identifiers,
  then use the mapped local Home Assistant temperature entities.

## Changed

- Publish complete comfort-profile participation details and additional
  allow-listed thermostat settings while preserving native value types and
  documented units.

## Fixed

- Interpret `backlightOffTime` as seconds and leave the undocumented-unit
  random-start delay settings unlabelled instead of guessing units.
- Fail the current-profile spread closed when a profile participant cannot be
  resolved uniquely, while preserving distinct sensors that share a display
  name.

## Quality

- Add focused coverage for stable profile identity joins, duplicate display
  names, ambiguous metadata, unit projection, recovery, and entity wiring.

# Beestat Statistics v2026.8.12

## Changed

- Publish every filter forecast field consumed by automations as one coherent,
  content-revisioned snapshot on the primary **Filter due date** entity. The
  snapshot includes the change boundary, runtime inputs, thresholds,
  intermediate dates, remaining days, and due state so consumers no longer
  combine sequential sibling-entity updates.

## Quality

- Add focused coverage for stable content revisions, runtime-only forecast
  changes, exact change timestamps, and the authoritative entity wiring.

# Beestat Statistics v2026.8.11

## Added

- Import daily room-sensor occupancy percentages when a mapped local occupancy
  entity proves the source capability. Boolean Beestat samples become native
  Recorder percentage mean/min/max statistics without adding another option or
  live-state owner.
- Import cumulative HVAC stage and accessory runtime statistics only after the
  corresponding Beestat summary field has a non-zero observation. Existing
  aggregate cool, heat, and fan runtime series remain unchanged.
- Expose allow-listed thermostat model, firmware, equipment-stage, property,
  differential, and complete comfort-profile details through the private,
  read-only `get_configuration` action. The response still excludes credentials,
  connection URLs, arbitrary source fields, and raw history.

## Changed

- Derive the cloud-data-stale threshold from the configured acquisition cadence:
  one normal poll plus 60 minutes of source-publication grace, with the existing
  two-hour minimum. The default six-hour cadence therefore uses 420 minutes and
  no longer reports healthy between-poll data as stale.
- Preserve comfort-profile heat/cool targets, fan modes, optimization, ventilation,
  and sensor membership in the existing unrecorded scheduled-profile attributes.

## Quality

- Add regression coverage for cadence boundaries, allow-list exclusion, profile
  parsing, occupancy scaling, zero-suppressed hardware detail, cumulative IDs,
  and finite Recorder output.

# Beestat Statistics v2026.8.10

## Fixed

- Fail closed when a reconfigure, reauthentication, source-scope, or mapping
  flow becomes stale while awaiting validation or confirmation. The winning
  config-entry update is preserved without a stale write, reload, or implicit
  merge, while unchanged flows retain unknown future fields.
- Normalize duplicate thermostat, room-sensor, summary, and point-history
  identities once using the last effective source row. Deletions and later
  restorations now produce the same effective resource across runtime models,
  mappings, diagnostics, entities, and Recorder imports.
- Reject non-finite or unrepresentable derived values before cached runtime
  projection or Recorder writes. Daily means preserve representable extreme
  inputs, while cumulative totals and seed offsets stop before an invalid row.

## Quality

- Add concurrency coverage for second-flow and external config-entry changes,
  including same-account validation and account-change confirmation.
- Add duplicate-row, deletion/restoration, timestamp-boundary, numeric-overflow,
  and no-invalid-Recorder-row regression coverage.

# Beestat Statistics v2026.8.9

## Changed

- Make automatic HomeKit/Ecobee matching one-to-one across Beestat source rows.
  Competing equal-confidence rows now remain unresolved, unique name matches
  beat weaker single-device fallbacks, and explicit mappings reserve their
  local device from automatic reuse.
- Require every explicit mapping to use entities from one source device and
  prevent duplicate explicit claims within thermostat or room-sensor mappings.
  Existing conflicts now raise a Repair and leave affected device links
  unresolved instead of choosing the first configured field or row.
- Show the exact cached entity candidates before bulk mapping confirmation.
  Recompute the candidate against current mappings and options immediately
  before saving, require confirmation again after target drift, and preserve
  unrelated concurrent option updates.

## Fixed

- Rebase destructive source-scope confirmation onto current config-entry
  options instead of saving a stale whole-options snapshot. Discovery or
  removal-count drift returns to review before any change is applied, including
  discovery that occurs while the initial selection form is open.
- Discover Home Assistant-dependent test modules from their imports for the
  dependency-light selector, while both hosted HA lanes run the complete test
  tree. A missing harness now fails collection, new HA test modules cannot
  silently fall into the dependency-light suite or miss hosted validation, and
  an empty discovered HA set fails closed.

## Quality

- Keep the supported Core `2026.8.0` and current patch `2026.8.1` lanes
  dependency-closed with their exact harness pins and a clean `pip check` before
  running the complete test tree.
- Add focused coverage for cross-row match conflicts, confidence precedence,
  explicit reservations and mapping conflicts, exact mapping previews, initial
  form and confirmation drift, Repairs, and concurrent options updates.

# Beestat Statistics v2026.8.8

## Changed

- Add an I/O-free local projection scheduler that updates cached comfort
  schedules, cloud-stale status, and local-date-dependent runtime and filter
  forecasts at their actual boundaries without shortening the six-hour Beestat
  acquisition interval.
- Follow Home Assistant timezone changes and daylight-saving transitions when
  calculating schedule and local-midnight boundaries. Recorder imports now use
  one timezone revision throughout and retry before writing if that context
  changes.
- Store user-confirmed thermostat and room-sensor mappings as stable Home
  Assistant entity-registry references. Explicit mappings now survive entity-ID
  renames, device moves, detachments, temporary removal, and registry
  recreation without falling back to mutable name matching.
- Add newly discovered Beestat thermostats and room sensors after a successful
  refresh or import while preserving explicit exclusions and existing entity
  unique IDs.
- Keep Beestat's delayed current comfort profile as diagnostic cloud context;
  scheduled profile and next transition are cached schedule projections and do
  not claim the thermostat's live hold or operating mode.

## Fixed

- Rebind existing enrichment entities when their HomeKit/Ecobee source-device
  association changes, without co-owning the source device or recreating the
  Beestat config entry.
- Cancel projection, timezone, registry, and import listeners on unload; avoid
  dispatching unchanged projections so Recorder does not receive needless
  state churn.
- Bound response reads, retryable transport failures, import-window splitting,
  and scheduled-import coalescing. Deterministic request failures now stop
  retrying while transient failures retain bounded recovery.
- Validate custom API URLs before constructing a client, guard option rollback
  against concurrent updates, and preserve account and mapping continuity
  across reconfigure, reauthentication, and YAML import paths.
- Translate setup, coordinator, and entity-action failures into bounded Home
  Assistant errors without exposing remote response text or credential-bearing
  exception details.

## Quality

- Add focused no-new-source-event, local-midnight/DST, refresh-reschedule,
  unload-cleanup, no-I/O, no-Recorder-churn, stable-mapping, bounded-response,
  and concurrency tests.
- Keep the Core `2026.8.0` supported-minimum lane dependency-closed, and add a
  hosted Core `2026.8.1` same-month patch lane that permits only the
  metadata-proven harness pin mismatch before running the complete HA tests.
- Extend public-safety, strict typing, workflow, exception-translation, and API
  surface checks for the new lifecycle and transport contracts.

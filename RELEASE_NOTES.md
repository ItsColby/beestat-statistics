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

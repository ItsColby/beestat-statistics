# Architecture

## Project Shape

- This is a HACS-published Home Assistant custom integration.
- Runtime integration files live under `custom_components/beestat_statistics/`;
  do not move them to `src/` or a package-only layout.
- Keep exactly one integration directory under `custom_components/`. HACS
  manages one integration per repository, and all runtime files required by Home
  Assistant must live under that integration directory.
- Keep GitHub/HACS support at the repository root: `README.md`, `hacs.json`,
  `.github/`, `requirements-ha-test.txt`, `requirements-ha-current.txt`,
  `pytest.ini`, `docs/`, `scripts/`, `tests/`, and `blueprints/`.
- `hacs.json` and `requirements-ha-test.txt` jointly own the supported Home
  Assistant floor, currently Core `2026.8.0`, and its dependency-closed harness
  lane. `requirements-ha-current.txt` owns a second dependency-closed lane for
  the exact installed same-month patch, currently Core `2026.8.1` with harness
  `0.13.355`. Both hosted lanes run `pip check` after the final dependency
  installation and then run the complete HA tests. Conflicts, skipped
  collection, or failing tests remain unsupported. Advance
  each requirements owner, CI label, documentation, and assertion with the
  support contract it represents; advance the HACS and blueprint minima only
  when the distribution floor changes.
- `scripts/run_dependency_light_tests.py` uses direct-import AST discovery to
  select tests that do not require Home Assistant. Both hosted HA lanes run the
  complete `tests/` tree, so a newly added module needs no second file-list
  update and cannot miss the HA lane. The HA modules import the real harness
  unconditionally so either hosted lane fails collection instead of passing
  through module-level skips when its dependencies are unavailable; discovering
  no HA modules also fails the dependency-light selector closed.
- Treat `.venv/`, `.local/`, `.pytest_cache/`, and `.ruff_cache/` as local
  working state. Do not commit Home Assistant config backups, API
  keys, raw diagnostics, copied Recorder databases, Beestat cache dumps, or live
  household evidence.

## Integration Boundaries

- Home Assistant UI/config entries are the primary configuration surface. YAML
  support exists for import/backward compatibility; do not make YAML the
  preferred routine configuration path.
- Config-entry `data` owns required connection identity: API key, API base URL,
  and the non-reversible account fingerprint. Initial UI/YAML setup,
  reconfigure, and reauthentication require at least one identifiable
  thermostat anchor before saving a connection. A successful but empty
  thermostat response leaves the entry unchanged because account continuity is
  unproven. Reconfigure and reauthentication validate replacements before
  saving them and require explicit confirmation when the thermostat
  fingerprints cannot prove account continuity and may indicate a different
  account. A confirmed possible account change clears saved source scope and
  per-source overrides before reload so numeric resource IDs cannot silently
  cross the account boundary; timing options remain intact. Previously imported
  Recorder statistics remain, so the confirmation must also explain the possible
  stable-slug history overlap. Reconfigure and reauthentication snapshot the
  config-entry owners they retain across validation or confirmation. If another
  flow or external update changes data that the flow would write—or options that
  an account replacement would write—the stale flow aborts without saving or
  scheduling a reload; same-account connection updates leave concurrent options
  untouched. Awaited YAML connection validation applies the same data-owner
  guard, then reconciles its declared option fields with current saved options
  immediately before saving.
- The API base URL must use HTTPS and must not contain user information, a
  query, or a fragment. Validate this boundary before constructing the client
  or exposing the API key to transport. A legacy invalid or insecure entry
  fails setup, creates an actionable Repair, and remains uncontacted until
  Reconfigure saves a valid secure URL.
- Config-entry `options` owns persistent behavior: import timing, selected
  Beestat source scope, and local mapping/filter/statistic overrides. Options
  save through native Home Assistant flows and reload the entry. Source scope
  reuses the versioned thermostat/sensor `enabled` override contract rather
  than maintaining a second list of IDs: missing flags preserve open-world
  discovery, `enabled: false` is an explicit exclusion, and `enabled: true`
  deliberately includes a source Beestat reports inactive.
- Setup and migration normalize persisted point-history and acquisition timing
  through the same bounds as the options UI. Malformed current-version storage
  therefore falls back safely without requiring a schema bump; valid cadence,
  including the six-hour default, is preserved.
- When YAML remains the declarative thermostat-mapping owner, later imports
  rebuild the effective option rows from YAML while preserving a native filter
  date and click-time runtime boundary by Beestat source ID. An explicit YAML
  `filter_changed_date` is date-only input, takes precedence, and clears the
  click boundary. A YAML connection replacement must validate as the same saved
  account before it can update the entry. A different, unavailable, or
  unprovable account is left unchanged and raises a Repair that directs the user
  through Reconfigure, where account changes require explicit confirmation.
- Source selectors combine current raw API discovery, the effective runtime
  model, and saved overrides. This keeps excluded and temporarily missing
  resources recoverable while preserving unknown saved rows across discovery
  drift. Excluding a currently active source requires a confirmation. Both the
  initial form and destructive confirmation retain the complete displayed
  source signature, including labels and inactive state, and return to the
  source form if that evidence changes before save.
- Legacy repeated override IDs follow one contract everywhere: the last row is
  effective. Runtime construction, forms, Repairs, device-conflict checks, and
  targeted option mutations all inspect or update that same row while retaining
  unknown fields and untouched legacy rows.
- The integration remains one config entry and one account-wide coordinator.
  Config subentries or multiple account entries are not justified by the
  current API/runtime ownership model and must not be introduced without a
  concrete repeated-resource requirement and a Recorder-statistics continuity
  design.
- Use HomeKit/Ecobee entities in Home Assistant for live local thermostat, room
  temperature, occupancy, motion, and control state. Use Beestat for history,
  runtime summaries, cloud profile context, alerts, and filter forecast inputs.
- The private read-only configuration action may project only explicitly
  allow-listed thermostat hardware, equipment-stage, property, differential,
  comfort-profile, and Ecobee configuration fields already cached by the
  coordinator. The coordinator reads `ecobee_thermostat.read_id` after the
  existing `thermostat.sync` refresh, immediately reduces each raw row to this
  allowlist, and never retains its account, location, billing, utility,
  management, device-identifier, notification-recipient, or access-code data.
  It must not serialize arbitrary future fields, expose credentials, or become
  a second write owner for Ecobee settings.
- Current-profile room temperature spread is an I/O-free local projection of
  explicitly mapped HomeKit temperature entities. Profile participants resolve
  from Ecobee's capability-qualified climate sensor identifier through the
  Beestat sensor identifier and internal sensor ID to one configured mapping;
  mutable or duplicate display names are never identity. Profile changes use
  cached Beestat program data; local source state changes rebuild the projection
  immediately without a Beestat request. Unknown, unavailable, nonnumeric,
  missing-unit, unconvertible, missing-identity, or ambiguous-identity sources
  are rejected honestly. Source observation age is not an availability gate.
- Mapped Beestat entities link through the existing HomeKit/Ecobee device entry;
  they must not return that other integration's identifiers or connections in
  `device_info` or add the Beestat config entry as a device owner. Setup removes
  legacy shared ownership with Home Assistant's helper-device migration API and
  preserves entity assignments. Fallback devices remain Beestat-owned and do
  not use the deprecated `via_device` identifier contract. This is required for
  Home Assistant Core 2026.8's one-config-entry-per-device model.
- User-confirmed UI mappings store a stable foreign-source reference containing
  the entity-registry UUID plus `(domain, platform, unique_id)`, while retaining
  the selection-time entity ID for safe downgrade and local diagnostics. Resolve
  the UUID first and the source tuple second so renames and registry recreation
  do not require a Beestat config-entry recreation. An unresolved stable
  reference is authoritative and must not fall back to mutable name matching.
  Options forms resolve the reference to the current entity ID for suggested
  values without rewriting storage; a temporarily unresolved source appears
  unselected while its stored reference remains recoverable.
  Existing UI mappings gain references through the versioned minor migration
  only when their current registry entry can be proven. YAML remains the
  portable entity-ID owner and is never silently rewritten; an unresolved YAML
  or unmigratable legacy mapping raises the existing mapping Repair.
- The options flow can confirm every currently ambiguity-safe automatic
  thermostat and room-sensor match in one transaction. It derives the
  candidate solely from the coordinator's cached normalized configuration and
  the in-memory entity registry, stores the same stable references as an
  individual mapping, leaves missing, ambiguous, and cross-row conflicting
  matches unresolved, preserves unrelated options, and causes at most one
  config-entry reload. A local source device may be claimed by at most one
  automatic Beestat thermostat or room-sensor row; a higher-confidence unique
  name match wins over a weaker fallback, while explicit mappings reserve their
  source device. Every explicit mapping must resolve its selected entities to
  one foreign source device, and one source device may be claimed by at most
  one explicit mapping of the same resource type. Options reject newly
  introduced conflicts; saved conflicts raise an actionable Repair and fail
  device linking closed for every affected row instead of selecting the first
  field or row by registry order. Bulk confirmation shows the exact entity
  candidates and recomputes them against current cached mappings and options
  before saving; target drift requires confirmation again. It never silently
  persists name matches without explicit confirmation.
- Entity- and device-registry lifecycle listeners rebuild only the cached runtime
  mapping and rebind existing Beestat enrichment entities when a foreign source
  moves, detaches, is removed, or is restored. Reconciliation must not recreate
  the config entry or contact Beestat, may update only entities owned by the
  current Beestat config entry, and removes both listeners on unload.
- Automated stale-fallback removal is limited to devices owned only by the
  current Beestat config entry, carrying only Beestat identifiers and no foreign
  connections. A mixed or shared registry record must fail closed.
- Do not add direct Ecobee API integration. Ecobee no longer provides new API
  keys, so ecobee-cloud actions are out of scope for this repo. If a useful
  action is needed, integrate it through Home Assistant state/services or
  Beestat where supported.
- Keep the Beestat API boundary narrow and documented. New Beestat endpoints
  need `docs/beestat-api-surface.json`,
  `scripts/check_beestat_api_surface.py`, README, diagnostics, and test
  coverage updates.
- Keep acquisition, observation, local projection, and effect deadlines as
  separate time domains. The configured import interval owns Beestat cloud I/O
  and remains six hours by default. One config-entry-owned local scheduler
  reevaluates cached comfort schedules, the cloud-stale threshold, and local
  date-dependent runtime/filter projections at their earliest boundary without
  I/O. It reschedules after source refresh and every boundary, publishes only
  actual projection changes, follows configured-timezone changes without an
  entry reload, and cancels both its deadline and timezone listener on unload.
  The coordinator is the single current-timezone owner for these projections
  and Recorder import windows. Every normalized refresh and prepared Recorder
  import captures one evaluation clock, timezone, and timezone revision. A
  windowed refresh retries when an awaited timezone/date rollover invalidates
  its requested local-day bounds, while an import discards the uncommitted
  preparation and retries before any Recorder write. Repeated churn is bounded.
  The filter-boundary retry remains a separate effect timer because its callback
  reads raw Beestat runtime and can persist reconciliation state; it captures
  its own temporal context and leaves the boundary pending rather than
  persisting a result from a stale timezone revision. Health-projection evidence
  is owned by `coordinator.py` and `tests/test_coordinator_helpers.py`;
  `tests/test_runtime_ha.py` owns the exact-Core scheduler/lifecycle cases, but
  their presence is not executed dependency-closure evidence.
- Cloud-stale state derives from the configured acquisition cadence: the larger
  of the two-hour minimum or one poll interval plus 60 minutes of source grace.
  The same computed threshold owns both entity state and the I/O-free projection
  deadline; static thresholds that oscillate during a healthy poll interval are
  not permitted.
- Recorder owns Beestat historical analytics. Room occupancy is imported as a
  daily percentage only when a mapped local occupancy entity proves capability.
  Stage/accessory runtime series are stable cumulative IDs but are created only
  after their source field has a non-zero observation, preserving aggregate
  runtime while avoiding empty hardware series. Finite IAQ source values remain
  unchanged; unsupported, missing, non-finite, or unrepresentable values are
  rejected instead of clamped.
- Filter changes are owned by the Home Assistant `date` entity, its colocated
  mark-changed button, and the optional legacy `input_datetime` helper bridge.
  The button first persists the local date and exact UTC click timestamp, because
  the physical replacement must not be lost when Beestat is stale or unavailable.
  Pending derived runtime excludes the ambiguous change day rather than charging
  earlier same-day runtime to the new filter, while still counting unambiguous
  later-date runtime. The coordinator then reconciles the timestamp against a bounded
  raw-runtime read, rounds to the nearest 5-minute source boundary, stores that
  day's fan-runtime baseline, and retries every 15 minutes for six hours while
  source data is not ready. Normal coordinator refreshes continue attempts after
  that fast-retry window. Same-day forecasts subtract the finalized baseline. A
  historical repair accepts an offsetless local timestamp only when it resolves
  to one real instant in the configured timezone. Repeated daylight-saving times
  require an explicit offset, and nonexistent local times fail validation rather
  than being normalized to a different wall time. A manual date edit clears the
  click timestamp and boundary because date-only
  input does not prove when the replacement occurred, then performs a skip-sync
  refresh so the selected historical date is covered. Keep the timestamp and
  boundary internal attributes of the date entity; do not add a second datetime
  entity without a separate UI/data-model requirement.
- Beestat filter alert dismissal is best-effort after a Home Assistant filter
  change. Do not write Ecobee settings or directly mutate Beestat sync-owned
  filter metadata.

## Code Ownership

- `__init__.py`: setup/unload, YAML import, Recorder statistics import
  services, repair issues, device migration/removal, filter-helper state
  listeners, and cumulative Recorder seed logic.
- `coordinator.py`: runtime sync/readback, Beestat metadata derivation, cloud
  profile/alert/filter runtime status, and coordinator diagnostic fields.
- `config_flow.py`, `config_payload.py`, `config_rows.py`, `entry_options.py`,
  `config_model.py`, `issues.py`: UI setup, reconfigure, reauth, options, YAML
  import conversion, effective-row identity, validation, actionable
  configuration Repairs, and runtime config modeling.
- `sensor.py`, `binary_sensor.py`, `button.py`, `date.py`, `entity.py`: Home
  Assistant entities and device attachment behavior.
- `profile.py`: pure comfort-profile normalization and the shared private/entity
  projection contract.
- `statistics_builder.py`: conversion of Beestat rows into Home Assistant
  external Recorder statistics. Preserve cumulative-series correctness when
  changing runtime or degree-day imports.
- `api.py`, `thermostat_settings.py`, `alerts.py`, `filter_forecast.py`,
  `diagnostics.py`: Beestat transport/parsing, the strict raw-Ecobee privacy
  boundary, alert classification, filter forecasting, and redacted diagnostics.
- `translations/en.json`, `icons.json`, `services.yaml`, `quality_scale.yaml`,
  and `README.md` are part of the user-facing contract. Update them with code
  behavior changes. Custom integrations ship complete translations directly;
  do not restore the Home Assistant Core-only `strings.json` build input.

## Configuration And Continuity Invariants

- API parsing, authentication mechanics, request safety limits, normalization,
  diagnostics redaction, unique-ID composition, statistics metadata, and
  cumulative Recorder math are implementation invariants, not preferences.
- Recorder normalization admits only finite numeric values. Derived means,
  cumulative totals, and Recorder seed offsets must also remain representable;
  an invalid cumulative contribution ends that series before any invalid row is
  written. Normalization produces at most one summary row per thermostat and
  local date. If a response repeats that
  identity, its last row is effective; a winning deletion omits it. Runtime
  points use their source row ID, or resource ID plus timestamp fallback, under
  the same rule before daily aggregation. Globally unique configured slugs and
  disjoint statistic suffixes then keep every Recorder series/start identity
  unique and prevent double-counted daily contributions.
- Cached thermostat and room-sensor metadata likewise gives the last source row
  ownership of each Beestat ID. A winning deletion omits that identity; a later
  active row restores it. Runtime models, mapping, diagnostics, and entity
  unique IDs therefore consume one shared source identity.
- Remote response bodies and arbitrary API error payload details must not enter
  exceptions, logs, entity attributes, diagnostics, or exception chains. Keep
  HA-visible failures bounded to the operation, HTTP status, and safe category.
- Source-scope changes may alter future entity exposure and import membership,
  but must not rewrite entity unique IDs, statistic IDs/slugs, state classes,
  units, statistic metadata, or previously imported Recorder history.
- Capture the complete displayed source signature when the source-scope form is
  shown. If an ID, label, or inactive state changes before the first submission,
  show the refreshed form; if it changes after a destructive preview, show the
  refreshed source set or removal count before accepting the change.
- Updating source scope must preserve mapping, filter, and statistic-capability
  fields on known resources and preserve unknown saved overrides unchanged.
- Disabled source overrides are ignored by mapping-domain and missing-entity
  Repairs because those references are not runtime dependencies. The checks
  resume when the source is enabled again.
- Storage migrations remain versioned and must preserve legacy connection data,
  timing values, source flags, mappings, and stable slug fields. Do not bump the
  config-entry version when UI behavior begins using an already-supported
  storage field; do add a migration when the persisted contract itself changes.
- Never put API keys, account fingerprints, private entity IDs, or raw Beestat
  identifiers in entity state, shareable diagnostics, logs, translations,
  public fixtures, or public documentation examples.
- Keep filter-date source, helper, and click-boundary attributes available in
  current state but excluded from Recorder history.
- Keep the primary filter due-date entity as the authoritative automation
  publication for one coherent forecast snapshot. Its unrecorded attributes
  carry the filter change boundary, runtime inputs, thresholds, intermediate
  dates, due state, and a deterministic content revision from one coordinator
  model so consumers never join sequential sibling-entity updates.
- Keep schedule, filter due-date/days-remaining, alert, and maintenance controls
  as the primary thermostat surface. Categorize freshness dates/lags, active
  sensor count, filter runtime details, intermediate forecast dates, and
  Beestat's delayed `program.currentClimateRef` current-comfort-profile context
  as diagnostic; keep advanced global import counters disabled by default.
  Scheduled profile and next transition are local projections of the cached
  Beestat schedule and do not claim the thermostat's live hold/mode state.
- Keep the current-profile room temperature spread enabled as one compact
  diagnostic measurement with unrecorded room-name/count attributes. Keep
  advanced Ecobee settings disabled by default in the entity registry; the
  response-only configuration action remains the complete private audit surface.
- Keep partial-import and active-alert evidence bounded in ordinary entity
  state. Preserve complete counts/categories, retain at most three
  private-identifier-free examples, and put broader aggregate evidence in
  redacted on-demand diagnostics.
- Downloadable diagnostics redact user-assigned names/slugs, exact filter-change
  dates and timestamps, local entity/device/source identifiers, and comfort-profile
  names/timing. Saved config-entry data/options are represented by an allow-listed
  ownership/count summary so unknown future fields fail closed. Preserve aggregate
  counts and health evidence instead.
- Refresh enabled override mapping Repairs when a referenced entity-registry
  record is removed, renamed, or restored, and remove the listener on unload.
  Do not silently rewrite the explicit YAML/options mapping owner.
- Preserve the supported helper-device association across foreign source move,
  detach, removal, and restoration without config-entry recreation. Registry
  reconciliation must prove config-entry ownership before each helper update
  and remove its listeners on unload.
- Clear the YAML connection-change Repair after a validated same-account import
  or after the YAML block is removed. Never apply a YAML credential replacement
  when the saved and candidate account fingerprints cannot prove continuity.
- Persist the physical filter-change event before fallible cloud work. A pending
  five-minute boundary must be visible in diagnostics, retry without blocking the
  normal coordinator, and never revert the saved click timestamp. Re-read the
  effective timestamp and current options after each awaited raw-runtime request;
  finalize only the same still-pending revision so an older request cannot
  overwrite a repeated press or an unrelated concurrent options update.

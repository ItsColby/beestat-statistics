# Architecture

## Project Shape

- This is a HACS-published Home Assistant custom integration.
- Runtime integration files live under `custom_components/beestat_statistics/`;
  do not move them to `src/` or a package-only layout.
- Keep exactly one integration directory under `custom_components/`. HACS
  manages one integration per repository, and all runtime files required by Home
  Assistant must live under that integration directory.
- Keep GitHub/HACS support at the repository root: `README.md`, `hacs.json`,
  `.github/`, `requirements-ha-test.txt`, `pytest.ini`, `docs/`, `scripts/`,
  `tests/`, and `blueprints/`.
- Treat `.venv/`, `.local/`, `.pytest_cache/`, `.ruff_cache/`, `.agents/`, and
  `.codex/` as local working state unless a future task explicitly turns one
  into a tracked repo feature. Do not commit Home Assistant config backups, API
  keys, raw diagnostics, copied Recorder databases, Beestat cache dumps, or live
  household evidence.

## Integration Boundaries

- Home Assistant UI/config entries are the primary configuration surface. YAML
  support exists for import/backward compatibility; do not make YAML the
  preferred routine configuration path.
- Config-entry `data` owns required connection identity: API key, API base URL,
  and the non-reversible account fingerprint. Initial setup is connection-only;
  reconfigure and reauthentication validate replacements before saving them and
  require an explicit confirmation before changing accounts. A confirmed
  account replacement clears saved source scope and per-source overrides before
  reload so numeric resource IDs cannot silently cross the account boundary;
  timing options remain intact. Previously imported Recorder statistics remain,
  so the confirmation must also explain the possible stable-slug history overlap.
- Config-entry `options` owns persistent behavior: import timing, selected
  Beestat source scope, and local mapping/filter/statistic overrides. Options
  save through native Home Assistant flows and reload the entry. Source scope
  reuses the versioned thermostat/sensor `enabled` override contract rather
  than maintaining a second list of IDs: missing flags preserve open-world
  discovery, `enabled: false` is an explicit exclusion, and `enabled: true`
  deliberately includes a source Beestat reports inactive.
- Source selectors combine current raw API discovery, the effective runtime
  model, and saved overrides. This keeps excluded and temporarily missing
  resources recoverable while preserving unknown saved rows across discovery
  drift. Excluding a currently active source requires a confirmation.
- The integration remains one config entry and one account-wide coordinator.
  Config subentries or multiple account entries are not justified by the
  current API/runtime ownership model and must not be introduced without a
  concrete repeated-resource requirement and a Recorder-statistics continuity
  design.
- Use HomeKit/Ecobee entities in Home Assistant for live local thermostat, room
  temperature, occupancy, motion, and control state. Use Beestat for history,
  runtime summaries, cloud profile context, alerts, and filter forecast inputs.
- Do not add direct Ecobee API integration. Ecobee no longer provides new API
  keys, so ecobee-cloud actions are out of scope for this repo. If a useful
  action is needed, integrate it through Home Assistant state/services or
  Beestat where supported.
- Keep the Beestat API boundary narrow and documented. New Beestat endpoints
  need `docs/beestat-api-surface.json`,
  `scripts/check_beestat_api_surface.py`, README, diagnostics, and test
  coverage updates.
- Filter changes are owned by the Home Assistant `date` entity and optional
  legacy `input_datetime` helper bridge. Do not switch filter change tracking to
  a datetime entity without a deliberate UI/data-model review.
- Beestat filter alert dismissal is best-effort after a Home Assistant filter
  change. Do not write Ecobee settings or directly mutate Beestat sync-owned
  filter metadata.

## Code Ownership

- `__init__.py`: setup/unload, YAML import, Recorder statistics import
  services, repair issues, device migration/removal, filter-helper state
  listeners, and cumulative Recorder seed logic.
- `coordinator.py`: runtime sync/readback, Beestat metadata derivation, cloud
  profile/alert/filter runtime status, and coordinator diagnostic fields.
- `config_flow.py`, `config_payload.py`, `entry_options.py`,
  `config_model.py`: UI setup, reconfigure, reauth, options, YAML import
  conversion, validation, and runtime config modeling.
- `sensor.py`, `binary_sensor.py`, `button.py`, `date.py`, `entity.py`: Home
  Assistant entities and device attachment behavior.
- `statistics_builder.py`: conversion of Beestat rows into Home Assistant
  external Recorder statistics. Preserve cumulative-series correctness when
  changing runtime or degree-day imports.
- `api.py`, `alerts.py`, `filter_forecast.py`, `diagnostics.py`: Beestat
  transport/parsing, alert classification, filter forecasting, and redacted
  diagnostics.
- `translations/en.json`, `icons.json`, `services.yaml`, `quality_scale.yaml`,
  and `README.md` are part of the user-facing contract. Update them with code
  behavior changes. Custom integrations ship complete translations directly;
  do not restore the Home Assistant Core-only `strings.json` build input.

## Configuration And Continuity Invariants

- API parsing, authentication mechanics, request safety limits, normalization,
  diagnostics redaction, unique-ID composition, statistics metadata, and
  cumulative Recorder math are implementation invariants, not preferences.
- Source-scope changes may alter future entity exposure and import membership,
  but must not rewrite entity unique IDs, statistic IDs/slugs, state classes,
  units, statistic metadata, or previously imported Recorder history.
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

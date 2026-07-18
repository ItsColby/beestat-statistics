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

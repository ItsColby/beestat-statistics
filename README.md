# Beestat Statistics

Home Assistant custom integration for importing Beestat HVAC history and enriching local Ecobee/HomeKit thermostat and room-sensor entities with Beestat-only context.

## Source Model

Use Ecobee/HomeKit entities as the primary Home Assistant source for live local thermostat, room temperature, occupancy, and control state. HomeKit is local and direct.

Use this Beestat integration as the secondary cloud/history surface for data HomeKit does not provide well. The integration discovers local `homekit_controller` Ecobee thermostat and room-sensor devices first, then maps Beestat thermostat/sensor rows onto those local devices when names match.

- Beestat `runtime.sync`, `thermostat.sync`, and `sensor.sync`
- HVAC runtime summary freshness and lag
- Daily external statistics for runtime, room temperatures, thermostat setpoints, thermostat-summary weather-load and humidity context, CO2, TVOC, and air quality
- Current, scheduled, and next Ecobee comfort profile names from Beestat's Ecobee cloud data
- Beestat sensor participation in the active comfort profile
- Thermostat cloud data window, active Ecobee alert counts, and equipment-alert problem binary sensors
- Native filter replacement forecasts from Beestat runtime plus per-thermostat filter lifetime settings

## Installation With HACS

[Open this repository in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=ItsColby&repository=beestat-statistics&category=integration)

1. In HACS, open **Custom repositories**.
2. Add this repository URL as type **Integration**:

   ```text
   https://github.com/ItsColby/beestat-statistics
   ```

3. Download **Beestat Statistics**.
4. Restart Home Assistant.
5. In Home Assistant, go to **Settings > Devices & services > Add integration** and add **Beestat Statistics**.
6. Enter the Beestat API key.

## Configuration

The preferred configuration path is the Home Assistant UI. The options flow exposes:

- included Beestat thermostats and room sensors
- point-history lookback days
- import interval seconds
- thermostat mapping overrides
- room-sensor mapping overrides

Initial setup asks only for the required Beestat API key and the normally unchanged API URL. Source scope, import timing, and mapping behavior live in the integration options. The integration is intentionally single-entry: one Beestat Statistics config entry owns one account connection and its selected thermostats and room sensors. Multiple config entries or config subentries would duplicate the same account-wide coordinator and fragment the external-statistics lifecycle, so they are not supported without a distinct future account/resource requirement. YAML imports can still update the existing entry for backward compatibility.

YAML remains supported as an import/backward-compatibility route:

```yaml
beestat_statistics:
  api_key: !secret beestat_api_key
  point_lookback_days: 45
  scan_interval:
    hours: 6
```

`api_key` is required. `point_lookback_days` defaults to 45 and is capped at 366. `scan_interval` defaults to 6 hours. On startup, YAML is imported into a Home Assistant config entry so entities can attach to devices and diagnostics.

After the imported entry is loaded, prefer Home Assistant's integration UI as the owner for routine changes. If YAML was only used to bootstrap the integration, remove the `beestat_statistics:` YAML block after verifying the entry works; keep YAML only when you intentionally want it to remain the declarative source.

Configuration fields:

- `api_key`: Beestat API key.
- `api_base`: optional Beestat API URL override. Defaults to `https://api.beestat.io/`.
- `point_lookback_days`: number of recent local days to import from Beestat point-history resources. Defaults to 45 and is capped at 366.
- `scan_interval`: YAML import interval. Defaults to 6 hours. UI options expose this as `scan_interval_seconds` with a 300-second minimum.

By default, no thermostat IDs, room names, or room sensor names are required. Beestat thermostat and sensor metadata is discovered from the account, and local HomeKit/Ecobee entity names take priority when they can be matched. Open the integration options and choose **Choose Beestat sources** to include only a subset. Newly discovered active sources remain included by default; explicit exclusions are preserved across discovery refreshes. Excluding a source stops its native entities from updating and omits it from future statistics imports, but does not delete external Recorder statistics already imported for it.

Automatic matching prefers HomeKit devices with Ecobee manufacturer/entity signals. If HomeKit omits that metadata, Ecobee-shaped thermostat and room-sensor devices can still match by name. Ambiguous duplicate name matches are left as Beestat-only fallback devices; use advanced overrides to pin those.

Advanced YAML can pin Beestat IDs to existing HomeKit entities when automatic name matching is not enough:

```yaml
beestat_statistics:
  api_key: !secret beestat_api_key
  thermostats:
    - id: 12345
      climate_entity_id: climate.zone_a
      filter_changed_entity_id: input_datetime.zone_a_hvac_filter_changed
  sensors:
    - id: 67890
      temperature_entity_id: sensor.room_sensor_b_temperature
      occupancy_entity_id: binary_sensor.room_sensor_b_occupancy
      motion_entity_id: binary_sensor.room_sensor_b_motion
```

Optional `slug` fields pin Recorder statistic IDs and the default filter-helper lookup. Optional `name` fields pin fallback labels and device names. Use both sparingly; the preferred naming source is the local HomeKit/Ecobee entity or device.

For new mapping fixes, prefer the integration options UI. Open the Beestat Statistics integration options, choose **Map a thermostat** or **Map a room sensor**, then select the Beestat row and the matching HomeKit entities. Use **Choose Beestat sources** for inclusion instead of adding one-off `enabled` overrides. YAML remains available for recovery, import, and bulk setups.

Advanced thermostat override fields:

- `id`: Beestat thermostat ID.
- `slug`: optional stable statistic/helper slug.
- `name`: optional fallback display name.
- `climate_entity_id`: matching Home Assistant `climate` entity.
- `temperature_entity_id`: matching Home Assistant temperature `sensor` entity.
- `occupancy_entity_id`: matching Home Assistant occupancy `binary_sensor` entity.
- `motion_entity_id`: matching Home Assistant motion `binary_sensor` entity.
- `filter_changed_entity_id`: optional Home Assistant `input_datetime` helper used as the filter-runtime start date.
- `filter_lifetime_runtime_hours`: runtime-hours replacement threshold. Defaults to 250.
- `filter_max_age_days`: calendar-age replacement threshold. Defaults to 90.
- `filter_notice_days`: notice-window days before the calculated due date. Defaults to 7.
- `enabled`: set to `false` to ignore a Beestat thermostat.

Advanced room-sensor override fields:

- `id`: Beestat sensor ID.
- `thermostat_id`: optional Beestat thermostat ID when the sensor row does not carry one.
- `slug`: optional stable statistic slug.
- `name`: optional fallback display name.
- `temperature_entity_id`: matching Home Assistant temperature `sensor` entity.
- `occupancy_entity_id`: matching Home Assistant occupancy `binary_sensor` entity.
- `motion_entity_id`: matching Home Assistant motion `binary_sensor` entity.
- `include_temperature`, `include_air_quality`, `include_co2`, `include_voc`: override which Beestat point-history fields are imported as Recorder statistics.
- `enabled`: set to `false` to ignore a Beestat room sensor.

To change the Beestat API key or API URL after setup, open the integration entry in Home Assistant and choose **Reconfigure**. If Beestat rejects the stored API key during setup, Home Assistant starts a native reauthentication flow. Setup stores a non-reversible fingerprint of the discovered Beestat thermostats. Reconfigure and reauthentication require a separate confirmation before replacing the connection with a key from a different account; the candidate key is not saved unless that confirmation succeeds. A confirmed account change resets saved source selections and per-source overrides so old numeric source IDs cannot be applied to the replacement account. Existing Recorder statistics remain, and future sources with overlapping stable slugs can continue those series, so treat account replacement as an explicit history-boundary decision.

## Entities

Global diagnostic entities are attached to the Beestat Statistics service device:

- status
- runtime sync last success
- metadata sync last success
- runtime summary row count
- statistics last import success
- imported series
- imported rows
- source rows
- skipped windows
- import partial problem binary sensor
- HomeKit mapping incomplete problem binary sensor
- refresh runtime button
- import statistics button

Per-thermostat entities are created for discovered Beestat thermostats. When a local HomeKit/Ecobee thermostat match exists, these entities attach to that local device:

- runtime summary latest date
- runtime summary lag days
- current comfort profile
- scheduled comfort profile
- next scheduled comfort profile time
- active sensor count
- cloud data end
- cloud data lag minutes
- active alert count
- active alert category
- filter changed date
- mark filter changed button
- filter runtime hours
- recent filter runtime hours per day
- filter remaining runtime hours
- filter runtime due date
- filter max age due date
- filter due date
- filter days remaining
- active alert binary sensor
- equipment alert problem binary sensor
- filter due binary sensor
- filter due soon binary sensor
- runtime summary stale problem binary sensor
- cloud data stale problem binary sensor

Room-level binary sensors expose whether Beestat reports each mapped Ecobee sensor as active in the current comfort profile. When a local HomeKit/Ecobee room sensor match exists, these entities attach to that local room-sensor device.

Per-thermostat alert binary sensors expose whether Beestat/Ecobee reports any active thermostat alert. Equipment-looking or unknown alerts are also surfaced through a separate problem binary sensor, so routine maintenance reminders do not make the thermostat device look failed.

The integration creates a Home Assistant service device for Beestat. Thermostat and room-sensor enrichment entities attach to existing HomeKit/Ecobee devices when possible without rewriting the HomeKit device name, manufacturer, model, or configuration link; otherwise, Beestat fallback devices are created. Keep local Ecobee/HomeKit devices and entities as the primary source for current state and control.

New active Beestat thermostats or sensors discovered after setup are added on the next successful runtime refresh or statistics import unless they were explicitly excluded. Sources reported inactive by Beestat can be deliberately selected in **Choose Beestat sources**; that selection is stored explicitly so it survives refreshes.

Diagnostic, profile, mapping, and alert-detail state attributes are available in current Home Assistant state but are excluded from Recorder history to avoid retaining noisy metadata on every state write.

The Status sensor attributes include HomeKit mapping counts for thermostats and room sensors, so you can see whether Beestat is enriching local HomeKit devices or using Beestat-only fallback devices. The HomeKit mapping incomplete problem binary sensor uses the same counts.

If a Beestat-only fallback device disappears from current Beestat metadata, Home Assistant can remove that stale Beestat device manually from the device page. Shared HomeKit/Ecobee devices are not removed by this integration.

If an enabled advanced YAML/import override references an entity that no longer exists, or assigns an override to the wrong Home Assistant domain, Home Assistant Repairs shows a warning. Excluded sources do not create mapping Repairs until they are included again. Update or remove an enabled override and reload the integration.

When a Beestat row becomes mapped to a HomeKit/Ecobee device, existing Beestat entities are migrated to that HomeKit device and stale Beestat-only fallback devices are removed from the integration device list.

## Data Updates

On setup and each import interval, the integration asks Beestat to sync runtime, thermostat, and sensor metadata before reading summary data. The default import interval is 6 hours. Native Beestat entities are coordinator-backed and update from that shared runtime readback rather than polling each entity independently. Routine imports refresh native status with a bounded summary window covering recent runtime and the effective filter-change date; full summary baselines are reserved for first import, missing Recorder seeds, rebuilds, and fallback repair paths.

The integration intentionally keeps the Beestat API boundary narrow: `runtime.sync`, `thermostat.sync`, `sensor.sync`, `thermostat.read_id`, `sensor.read_id`, windowed `runtime_thermostat_summary.read_id`, windowed `runtime_thermostat.read` / `runtime_sensor.read`, and `thermostat.dismiss_alert` for Beestat-side filter alert acknowledgement after a local Home Assistant filter change. Cumulative runtime and degree-day imports use a Recorder-seeded 7-day summary overlap when Home Assistant already has a trustworthy prior cumulative row; otherwise the importer falls back to the full Beestat summary baseline.

When a thermostat is mapped to a `filter_changed_entity_id`, changes to that Home Assistant helper also trigger a Beestat statistics import so filter-runtime statistics catch up without a separate automation. The helper is a compatibility bridge; the Home Assistant **Filter changed date** entity is preferred for new changes.

For filter tracking, use the per-thermostat **Filter changed date** entity or press **Mark filter changed** on the thermostat device. The effective filter date comes from the Home Assistant date override first, then a configured legacy helper, then Beestat/Ecobee filter metadata when available. Filter forecast sensors use the effective date, Beestat runtime since that date, the recent runtime rate, and the per-thermostat lifetime/max-age settings. A replacement date of today is treated as a fresh filter for runtime forecasting because Home Assistant date entities do not store the exact replacement time. When the Home Assistant filter date changes, the integration also best-effort dismisses active Beestat filter-looking alerts for that thermostat; it does not write ecobee settings or directly edit Beestat sync-owned filter metadata. The filter due sensor is a problem binary sensor; filter due soon is an advisory binary sensor for the notice window.

Use the **Refresh Runtime** button to refresh native Beestat status/profile/freshness entities without importing Recorder statistics. Use the **Import Statistics** button or service action to sync Beestat and import daily external statistics. Use `beestat_statistics.rebuild_statistics` only when you need to repair or backfill Recorder statistics from the full Beestat summary baseline.

No automation is required for normal operation. If you want a manual or event-driven import, use the Home Assistant automation UI to call `beestat_statistics.import_statistics` or press the **Import Statistics** button.

This integration does not provide custom device triggers or conditions.

## Automation Examples

Normal sync/import operation does not require an automation. The integration includes one optional blueprint for alerting when a selected Beestat runtime summary lag-days sensor remains stale:

- [Beestat runtime data stale notification](blueprints/automation/beestat_statistics/stale_runtime_notification.yaml)

After this repository is available to Home Assistant, import the blueprint from:

```text
https://raw.githubusercontent.com/ItsColby/beestat-statistics/main/blueprints/automation/beestat_statistics/stale_runtime_notification.yaml
```

## Use Cases

- Show whether Beestat summary data is fresh for each thermostat.
- Chart long-term HVAC runtime, weather-load context, setpoints, and room temperatures with Recorder statistics.
- See which Ecobee room sensors Beestat says are active in the current comfort profile.
- Track filter runtime and replacement forecasts from Beestat data, optionally initialized from a legacy Home Assistant filter-changed helper.
- Distinguish routine maintenance reminders from equipment-looking active alerts in dashboard summaries and HA problem cards.

## Service Action

The `beestat_statistics.import_statistics` service action syncs Beestat, refreshes native Beestat entities, and imports derived daily external statistics into Home Assistant Recorder. Normal imports use the 7-day Recorder-seeded summary overlap when possible and automatically fall back to the full baseline when prior Recorder state is missing.

Fields:

- `point_lookback_days`: optional number of recent local days to read for point-history statistics.
- `skip_sync`: optional boolean. Use only for controlled workflows where Beestat was just synced and another sync would be redundant.

The `beestat_statistics.rebuild_statistics` service action forces the full Beestat summary baseline before writing statistics, optionally limited by configured Beestat `thermostat_id`, `start_date`, and `end_date`. Use it for repairs, corrected historical Beestat rows, or targeted backfills rather than routine imports.

## Diagnostics

Home Assistant diagnostics are available from the integration entry. Diagnostics redact the API key, API URL, Beestat account fingerprint, Beestat IDs, Home Assistant entity IDs, and device identifiers, and include status, row counts, import metrics, import summary mode/window/fallback details, skipped-window counts, automatic filter-alert dismissal results, freshness, and compact thermostat/profile summaries. Raw Beestat history is not included.

For an exact local configuration audit, call the read-only `beestat_statistics.get_configuration` action with this integration's configuration entry. It returns the effective timing, saved thermostat and room-sensor overrides, and the complete effective mappings without contacting Beestat or changing Home Assistant state. The response deliberately excludes the API key and API URL, but it includes local names, Beestat IDs, and Home Assistant entity IDs; treat it as private household configuration and do not attach it to public issues.

## Recorder Statistics

The integration imports external statistics under source `beestat`, including:

- Cumulative cool, heat, and fan runtime hours
- Cumulative thermostat-summary heating and cooling degree days
- Daily room temperature mean/min/max
- Daily thermostat heat/cool setpoint mean/min/max
- Daily thermostat-summary indoor humidity, outdoor temperature mean/min/max, and outdoor humidity means
- Daily air quality, CO2, and TVOC mean/min/max for mapped sensors that expose those Beestat fields

Temperature statistics use Home Assistant recorder temperature metadata, so Home Assistant can display them in the preferred frontend unit.

These are intended for long-term charts and Home Assistant dashboards. They are not a replacement for live HomeKit/Ecobee entities.

## Supported Scope

This integration is designed for Ecobee thermostats and Ecobee room sensors that are visible in Beestat. It enriches local HomeKit Controller devices when names can be matched, and supports YAML ID/entity overrides when automatic matching is not enough.

## Known Limitations

Beestat's public API is useful but not versioned as a stable Home Assistant integration contract. This integration keeps calls narrow and exposes failures through diagnostic state instead of silently masking them.

Beestat is a cloud/history source. HomeKit/Ecobee entities should remain the primary source for live local temperature, occupancy, HVAC mode, setpoints, and control. Beestat alert entities mirror Beestat/Ecobee alert metadata and may include maintenance reminders rather than active equipment faults.

Removing the integration stops future imports and removes the integration's native entities, but Recorder external statistics already imported under source `beestat` may remain in Home Assistant's statistics database.

## Troubleshooting

- Check the Beestat Statistics **Status** sensor first. Its attributes include the latest error, runtime fetch time, summary row count, import mode/window/fallback details, automatic filter-alert dismissal results, and latest import row count.
- If summary dates lag, press **Refresh Runtime** or call `beestat_statistics.import_statistics` without `skip_sync`.
- If Home Assistant asks for reauthentication, enter a current Beestat API key in the reauth flow.
- If a thermostat or room sensor is absent, check **Choose Beestat sources** in the integration options.
- If automatic HomeKit mapping misses an included thermostat or room sensor, use **Map a thermostat** or **Map a room sensor** in the integration options. Use advanced YAML only for recovery or bulk configuration.
- If a filter forecast is unavailable, check the thermostat **Filter changed date**, **Filter runtime hours**, and **Filter recent runtime hours per day** entities first.
- If an existing install is upgraded from a release where runtime/cloud stale problem sensors were disabled by default, the integration enables only those integration-disabled stale diagnostic entities during setup. User-disabled entities remain disabled.

## Development Validation

Home Assistant `2026.7.x` requires Python `3.14.2` or newer. The GitHub validation workflow uses Python `3.14`; use the same major version for any local Home Assistant test harness work.

This repository is a HACS custom integration. The Beestat API client is intentionally in-tree and uses Home Assistant's shared aiohttp websession. If this integration is ever prepared for Home Assistant Core inclusion, split the Beestat client into an async, tagged, open-source PyPI package before submission.

Local pure-module checks:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m compileall -q custom_components\beestat_statistics tests scripts
```

Upstream Beestat API drift check:

```powershell
.\.venv\Scripts\python.exe scripts\check_beestat_api_surface.py
```

The checked-in snapshot is `docs/beestat-api-surface.json`. Review upstream changes before refreshing it with `--update`; do not treat a changed snapshot as approval to broaden the Home Assistant integration scope.

The checked-in `custom_components/beestat_statistics/quality_scale.yaml` tracks Home Assistant integration-quality rules with current repo evidence. Omitted rules are intentionally unclaimed until matching coverage, typing, or runtime evidence exists.

Home Assistant harness checks require a Python `3.14` environment with the pinned dependencies in `requirements-ha-test.txt` installed:

```powershell
python -m pip install -r requirements-ha-test.txt
pytest tests/test_config_flow_ha.py -q
```

If local Python is older than `3.14.2`, run the same harness through Docker Desktop from the repository root:

```powershell
docker run --rm -v "${PWD}:/work" -w /work python:3.14-slim bash -lc "python -m pip install --upgrade pip && python -m pip install -r requirements-ha-test.txt && pytest tests/test_config_flow_ha.py -q"
```

## Release Publishing

Before publishing a release intended for HACS, verify the repository still has a public description, relevant Home Assistant/HACS topics, issues enabled, a brand icon, passing Hassfest, passing HACS Action, and a GitHub release tag matching the manifest version.

When publishing manually with GitHub CLI, write the release body to a Markdown file and pass it with `--notes-file`. Avoid PowerShell strings containing `\n`; GitHub renders those as literal backslash-n text.

```powershell
gh release create vYYYY.M.D --title vYYYY.M.D --notes-file release-notes.md
gh release edit vYYYY.M.D --notes-file release-notes.md
```

## Removal

1. In Home Assistant, remove the **Beestat Statistics** integration entry from **Settings > Devices & services**.
2. If installed through HACS, remove **Beestat Statistics** from HACS.
3. Restart Home Assistant after removing the custom integration files.

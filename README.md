# Beestat Statistics

## Local release validation

Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-release-local.ps1`
before publishing a release candidate. It uses the `Ubuntu-24.04` WSL2
distribution and rootless Podman to run the same local-tree unit,
minimum/current Home Assistant, and Hassfest validation classes as the hosted
workflow. Images are pinned by digest. HACS validation reads a pushed repository
through GitHub's API, so the hosted HACS job remains the independent public
metadata and release gate rather than receiving a local GitHub credential. The
hosted unit and Home Assistant jobs call this same script in `native` mode, so
future validation changes have one product-owned command surface.

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

Initial setup asks only for the required Beestat API key and the normally unchanged API URL. Beestat must return at least one identifiable thermostat before a new or replacement connection is saved; otherwise the integration cannot prove account continuity. Source scope, import timing, and mapping behavior live in the integration options. The integration is intentionally single-entry: one Beestat Statistics config entry owns one account connection and its selected thermostats and room sensors. Multiple config entries or config subentries would duplicate the same account-wide coordinator and fragment the external-statistics lifecycle, so they are not supported without a distinct future account/resource requirement. YAML imports can still update the existing entry for backward compatibility.

Credential-bearing API requests never follow redirects. If Beestat moves the
endpoint, update the validated HTTPS API URL through **Reconfigure** instead of
allowing an HTTP redirect to forward the API-key query to another endpoint.

YAML remains supported as an import/backward-compatibility route:

```yaml
beestat_statistics:
  api_key: !secret beestat_api_key
  point_lookback_days: 45
  scan_interval:
    hours: 6
```

`api_key` is required. `point_lookback_days` defaults to 45 and is capped at 366. `scan_interval` defaults to 6 hours. On startup, YAML is imported into a Home Assistant config entry so entities can attach to devices and diagnostics.

If YAML later supplies a different API key or API URL, the integration validates
the candidate before changing the saved connection. It applies the replacement
only when the non-reversible account fingerprint proves it is the same Beestat
account. Otherwise the entry, mappings, and imported Recorder history remain
unchanged and Home Assistant Repairs directs you to **Reconfigure**, where an
intentional account change requires explicit confirmation. Removing the YAML
block clears a stale YAML connection Repair.

After the imported entry is loaded, prefer Home Assistant's integration UI as the owner for routine changes. If YAML was only used to bootstrap the integration, remove the `beestat_statistics:` YAML block after verifying the entry works; keep YAML only when you intentionally want it to remain the declarative source.

When YAML continues to own thermostat mappings, a native **Mark filter changed**
click remains stored in config-entry options and is overlaid on the YAML mapping
after later imports or restarts. An explicit YAML `filter_changed_date` remains
date-only declarative input, takes precedence, and clears any saved click-time
runtime boundary.

Configuration fields:

- `api_key`: Beestat API key.
- `api_base`: optional HTTPS Beestat API URL override. Defaults to `https://api.beestat.io/`. URLs containing user information, a query, or a fragment are rejected so the API key is never sent through an ambiguous or plaintext transport.
- `point_lookback_days`: number of recent local days to import from Beestat point-history resources. Defaults to 45 and is capped at 366.
- `scan_interval`: YAML import interval. Defaults to 6 hours. UI options expose this as `scan_interval_seconds` with a 300-second minimum and one-year maximum.

By default, no thermostat IDs, room names, or room sensor names are required. Beestat thermostat and sensor metadata is discovered from the account, and local HomeKit/Ecobee entity names take priority when they can be matched. Open the integration options and choose **Choose Beestat sources** to include only a subset. Newly discovered active sources remain included by default; explicit exclusions are preserved across discovery refreshes. If a discovered ID, label, or active/inactive state changes while the selection form or its destructive confirmation is open, Home Assistant shows the refreshed source set or removal count before accepting the change. Excluding a source stops its native entities from updating and omits it from future statistics imports, but does not delete external Recorder statistics already imported for it.

Automatic matching prefers HomeKit devices with Ecobee manufacturer/entity signals. If HomeKit omits that metadata, Ecobee-shaped thermostat and room-sensor devices can still match by name. Automatic mappings are one-to-one: when multiple Beestat sources compete for the same local device at the same confidence, every conflicting source remains unresolved; a unique name match can win over a weaker single-device fallback. Every explicit mapping must select entities from one source device, and the same source device cannot be explicitly assigned to multiple thermostat mappings or multiple room-sensor mappings. Conflicting explicit mappings remain detached and raise a Repair instead of silently linking to the first selected device. Explicit mappings also reserve their local device from automatic reuse. Use the mapping options to resolve conflicts deliberately.

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

For new mapping fixes, prefer the integration options UI. Choose **Confirm automatic mappings** to review the exact cached HomeKit entity list and pin all current unambiguous thermostat and room-sensor matches in one update, or choose **Map a thermostat** or **Map a room sensor** to correct an individual match. The confirmation recomputes against current cached mappings and options immediately before saving: a changed target is shown again for confirmation, and unrelated concurrent option changes are preserved. Individual mapping forms reject a newly introduced cross-device or duplicate-device claim. Confirmed UI mappings retain stable entity-registry source identity, so entity-ID renames and removal/restoration of the same source do not require recreating the Beestat entry. Missing, ambiguous, or conflicting matches remain unresolved, and automatic name matching remains an ambiguity-safe onboarding fallback only; the integration never persists those matches without confirmation. YAML remains a portable entity-ID owner and must be updated manually after a mapped entity-ID rename. Use **Choose Beestat sources** for inclusion instead of adding one-off `enabled` overrides. YAML remains available for recovery, import, and bulk setups.

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

To change the Beestat API key or API URL after setup, open the integration entry in Home Assistant and choose **Reconfigure**. If Beestat rejects the stored API key during setup, Home Assistant starts a native reauthentication flow. Setup stores a non-reversible fingerprint of the discovered Beestat thermostats. Reconfigure and reauthentication require a separate confirmation when the validated thermostat fingerprint cannot prove the connection matches the saved Beestat account and it may belong to a different account; the candidate key is not saved unless that confirmation succeeds. A confirmed possible account change resets saved source selections and per-source overrides so old numeric source IDs cannot be applied to the replacement account. Existing Recorder statistics remain, and future sources with overlapping stable slugs can continue those series, so treat account replacement as an explicit history-boundary decision.

Do not rotate credentials for an existing YAML-managed entry by editing YAML
alone when the replacement may belong to another account. Use **Reconfigure**
first so Home Assistant can validate and, when necessary, confirm the history
boundary; then make YAML match the saved connection.

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

Scheduled/next comfort profiles, filter due date and days remaining, alerts,
and filter-maintenance controls form the primary thermostat surface. Beestat's
current comfort profile is delayed cloud diagnostic context: it mirrors the
cached `program.currentClimateRef` and is not a replacement for Home
Assistant's local thermostat mode or active hold. Freshness dates/lags,
active-sensor count, raw filter-runtime detail, and intermediate
runtime/max-age forecast dates are also categorized as diagnostic. Advanced
account-wide import counters remain disabled by default.

Room-level binary sensors expose whether Beestat reports each mapped Ecobee sensor as active in the current comfort profile. When a local HomeKit/Ecobee room sensor match exists, these entities attach to that local room-sensor device.

Per-thermostat alert binary sensors expose whether Beestat/Ecobee reports any active thermostat alert. Equipment-looking or unknown alerts are also surfaced through a separate problem binary sensor, so routine maintenance reminders do not make the thermostat device look failed.

The integration creates a Home Assistant service device for Beestat. Thermostat and room-sensor enrichment entities link to existing HomeKit/Ecobee devices when possible without adding Beestat as an owner of those devices or rewriting their name, manufacturer, model, or configuration link; otherwise, Beestat fallback devices are created. Setup also removes legacy cross-integration device ownership while preserving each enrichment entity's device assignment. Supported registry listeners resolve confirmed source identity across entity-ID renames, moves, detaches, removal, and restoration, rebinding existing enrichment entities without recreating the Beestat config entry or contacting Beestat; the listeners are removed on unload. This follows Home Assistant's helper-integration device model for Core 2026.8 and later. Keep local Ecobee/HomeKit devices and entities as the primary source for current state and control.

New active Beestat thermostats or sensors discovered after setup are added on the next successful runtime refresh or statistics import unless they were explicitly excluded. Sources reported inactive by Beestat can be deliberately selected in **Choose Beestat sources**; that selection is stored explicitly so it survives refreshes.

Diagnostic, profile, mapping, filter-boundary, and alert-detail state attributes
are available in current Home Assistant state but are excluded from Recorder history
to avoid retaining noisy metadata on every state write. Active-alert surfaces keep
the complete count and category while showing at most three alert examples. Those
examples retain only bounded code/type/severity/time fields and a derived category;
arbitrary alert text and source identifiers are omitted.

The Status sensor attributes include HomeKit mapping counts for thermostats and room sensors, so you can see whether Beestat is enriching local HomeKit devices or using Beestat-only fallback devices. The HomeKit mapping incomplete problem binary sensor uses the same counts.

If a Beestat-only fallback device disappears from current Beestat metadata, Home Assistant can remove that stale Beestat device manually from the device page. Shared HomeKit/Ecobee devices are not removed by this integration.

If an enabled advanced YAML/import override references an entity that no longer exists, assigns an override to the wrong Home Assistant domain, spans multiple source devices, or duplicates another mapping's source device, Home Assistant Repairs shows a warning. Mapping-device conflicts disable device linking for every affected mapping rather than choosing one field or row by registry order. The warnings follow referenced entity-registry removal, rename, move, and recovery without waiting for an integration reload. Excluded sources do not create mapping Repairs until they are included again. Update or remove an enabled override when the mapping intentionally changed.

When a Beestat row becomes mapped to a HomeKit/Ecobee device, existing Beestat entities are migrated to that HomeKit device and stale Beestat-only fallback devices are removed from the integration device list. Subsequent source-device association changes are reconciled from Home Assistant's registries, and only entity records owned by the loaded Beestat config entry can be changed.

## Data Updates

On setup and each import interval, the integration asks Beestat to sync runtime, thermostat, and sensor metadata before reading summary data. The default import interval is 6 hours. Native Beestat entities are coordinator-backed and update from that shared runtime readback rather than polling each entity independently. Routine imports refresh native status with a bounded summary window covering recent runtime and the effective filter-change date; full summary baselines are reserved for first import, missing Recorder seeds, rebuilds, and fallback repair paths.

The six-hour interval owns cloud acquisition only. Between cloud reads, one
config-entry-owned local scheduler reevaluates the cached comfort schedule,
cloud-stale threshold, runtime-summary local date, and filter due-date/day
projections at the earliest relevant boundary. Those callbacks perform no
Beestat I/O, reschedule after refresh and after each boundary, and notify
entities only when projected state changes. Schedule boundaries and local
midnight follow the configured/thermostat timezone across daylight-saving time.
Changing Home Assistant's configured timezone rebuilds and reschedules these
cached projections without contacting Beestat or reloading the config entry;
each refresh and Recorder import attempt captures one evaluation time and
coordinator-owned timezone revision. If a timezone change alters local-day
bounds during an awaited refresh, the window is retried. Prepared statistics
are discarded and retried before any Recorder write when their timezone
revision becomes stale, so one import cannot mix local-day interpretations.
The separate 15-minute filter-boundary retry can read Beestat and persist a
result, so it is not part of this local projection scheduler; a timezone change
during that read leaves the boundary pending for a fresh effect attempt.

The cloud-stale diagnostic follows that acquisition owner. Its threshold is the
larger of two hours or the configured poll interval plus 60 minutes of source
publication grace. The default six-hour cadence therefore becomes stale only
after 420 minutes of source lag, while short cadences retain the two-hour floor.

The integration intentionally keeps the Beestat API boundary narrow: `runtime.sync`, `thermostat.sync`, `sensor.sync`, `thermostat.read_id`, `sensor.read_id`, `ecobee_thermostat.read_id`, windowed `runtime_thermostat_summary.read_id`, windowed `runtime_thermostat.read` / `runtime_sensor.read`, and `thermostat.dismiss_alert` for Beestat-side filter alert acknowledgement after a local Home Assistant filter change. The raw Ecobee row is used only after Beestat's existing thermostat sync and is immediately reduced to a strict configuration allowlist; account, location, billing, utility, management, device-identifier, notification-recipient, and access-code data is never retained. Cumulative runtime and degree-day imports use a Recorder-seeded 7-day summary overlap when Home Assistant already has a trustworthy prior cumulative row; otherwise the importer falls back to the full Beestat summary baseline.

When a thermostat is mapped to a `filter_changed_entity_id`, changes to that Home Assistant helper also trigger a Beestat statistics import so filter-runtime statistics catch up without a separate automation. The helper is a compatibility bridge; the Home Assistant **Filter changed date** entity is preferred for new changes.

For filter tracking, use the per-thermostat **Filter changed date** entity or press **Mark filter changed** on the thermostat device. The effective filter date comes from the Home Assistant date override first, then a configured legacy helper, then Beestat/Ecobee filter metadata when available. Filter forecast sensors use the effective date, Beestat runtime since that date, the recent runtime rate, and the per-thermostat lifetime/max-age settings. The primary **Filter due date** entity also exposes one coherent, content-revisioned forecast snapshot containing every runtime, threshold, intermediate date, and due-state field needed by downstream automations; consumers do not need to combine sequential sibling-entity publications. **Mark filter changed** immediately saves the exact UTC click timestamp before any fallible cloud work, so the new filter starts at zero even if Beestat is temporarily stale or unavailable. The coordinator then reads only the relevant raw-runtime window, reconciles the click to Beestat's nearest 5-minute source boundary, and retries every 15 minutes for the first six hours while that interval is pending. Normal coordinator refreshes continue reconciliation attempts after the fast-retry window without creating perpetual 15-minute cloud syncs. While the exact boundary is pending, the ambiguous change day is excluded but unambiguous later-date runtime counts toward the new filter's lifetime. Reconciliation compares the persisted timestamp again immediately before saving, so a slow older request cannot overwrite a newer repeated press or another options update. After reconciliation, same-day runtime and forecasts subtract the finalized change-day baseline, and the due indicators resolve the latest saved thresholds without requiring an integration reload. Repeated presses on the same day replace the prior timestamp and reset the new-filter lifetime again. Manually editing **Filter changed date** clears the click timestamp and boundary because date-only input does not establish when during that day the replacement occurred. The date entity exposes unrecorded boundary status and timestamps for local troubleshooting. The integration also best-effort dismisses active Beestat filter-looking alerts for that thermostat; it does not write Ecobee settings or directly edit Beestat sync-owned filter metadata. The filter due sensor is a problem binary sensor; filter due soon is an advisory binary sensor for the notice window.

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
- Measure the current comfort profile's local room-temperature spread from the
  mapped HomeKit sensors, with unavailable sources rejected instead of aged out.
- Inspect useful non-secret Ecobee configuration in the response-only action and
  optionally enable a small set of advanced diagnostic entities later.
- Track filter runtime and replacement forecasts from Beestat data, optionally initialized from a legacy Home Assistant filter-changed helper.
- Distinguish routine maintenance reminders from equipment-looking active alerts in dashboard summaries and HA problem cards.

## Service Action

The `beestat_statistics.import_statistics` service action syncs Beestat, refreshes native Beestat entities, and imports derived daily external statistics into Home Assistant Recorder. Normal imports use the 7-day Recorder-seeded summary overlap when possible and automatically fall back to the full baseline when prior Recorder state is missing.

Fields:

- `point_lookback_days`: optional number of recent local days to read for point-history statistics.
- `skip_sync`: optional boolean. Use only for controlled workflows where Beestat was just synced and another sync would be redundant.

The `beestat_statistics.rebuild_statistics` service action forces the full Beestat summary baseline before writing statistics, optionally limited by configured Beestat `thermostat_id`, `start_date`, and `end_date`. Use it for repairs, corrected historical Beestat rows, or targeted backfills rather than routine imports.

The `beestat_statistics.repair_filter_change_boundary` service action assigns a verified timestamp to an existing filter date from the last 31 days, then runs the same bounded five-minute reconciliation without dismissing alerts. It requires the loaded config entry and Beestat thermostat ID, interprets an unambiguous timestamp without an offset in Home Assistant's local timezone, and rejects timestamps whose local date does not match the saved filter date. During a repeated daylight-saving hour, include an explicit offset so the exact occurrence is known; nonexistent local times are rejected. This is a narrow historical repair tool, not the normal replacement workflow.

## Diagnostics

Home Assistant diagnostics are available from the integration entry. Diagnostics use an allow-listed aggregate of saved configuration rather than serializing the config-entry payload. They redact credentials, URLs, names/slugs, Beestat and Home Assistant identifiers, exact filter-change details, and comfort-profile names/timing while retaining configuration ownership/counts, status, row counts, import metrics, import summary mode/window/fallback details, skipped-window counts plus at most three identifier-free resource/time examples, automatic filter-alert dismissal results, freshness, and compact aggregate thermostat evidence. Remote response bodies and arbitrary API error payload details are never included; HA-visible failures use bounded operation, status, and category messages. Raw Beestat history is not included.

For an exact local configuration audit, call the read-only `beestat_statistics.get_configuration` action with this integration's configuration entry. It returns the effective timing, saved thermostat and room-sensor overrides, the complete effective mappings, and allow-listed Beestat source details already held by the coordinator: thermostat model/firmware, reported and detected HVAC equipment/stages, basic property characteristics, comfort-profile targets and membership, and useful Ecobee comfort, staging, range, humidity, ventilation, equipment, alert, display, access-policy, and audio settings. Unit-bearing raw Ecobee scalars are labeled or normalized in the response. It does not contact Beestat or change Home Assistant state. The response deliberately excludes the API key, API URL, account/location/billing/utility/management/device/access-code data, notification recipients, arbitrary future source fields, and raw history, but it includes local names, Beestat IDs, and Home Assistant entity IDs; treat it as private household configuration and do not attach it to public issues.

The per-thermostat **Configured profile room temperature spread** sensor follows
the sensors configured in the current Beestat comfort profile while reading
their mapped local HomeKit temperature entities. It does not claim which sensor
Follow Me is momentarily weighting. It rebuilds immediately from local state
changes and profile transitions without cloud I/O, uses Home Assistant's native
temperature-delta semantics, and retains legacy participating-sensor attributes
for compatibility. Advanced setting entities such
as Auto Away, Follow Me, Smart Circulation, preheat/precool, compressor
protection, staging differentials and dissipation times, temperature correction,
temperature/humidity alert policies, service reminders, Wi-Fi alerts, microphone
state, playback volume, heat/cool minimum delta, and hold action are disabled by
default so they remain discoverable without crowding routine device and dashboard
surfaces. Static numeric settings preserve their native semantic unit but do not
opt into Recorder long-term measurement statistics, and binary setting names say
`enabled` so they cannot be mistaken for active alerts or microphone activity.
The response-only configuration action remains the exhaustive private audit
surface.

## Recorder Statistics

The integration imports external statistics under source `beestat`, including:

- Cumulative cool, heat, and fan runtime hours
- Cumulative stage and accessory runtime hours, created only for hardware fields
  with observed non-zero runtime
- Cumulative thermostat-summary heating and cooling degree days
- Daily room temperature mean/min/max
- Daily room occupancy percentage mean/min/max when a mapped local occupancy
  entity proves that capability
- Daily thermostat heat/cool setpoint mean/min/max
- Daily thermostat-summary indoor humidity, outdoor temperature mean/min/max, and outdoor humidity means
- Daily air quality, CO2, and TVOC mean/min/max for mapped sensors that expose those Beestat fields

Finite Beestat IAQ values are preserved as source observations, including
legitimate spikes. Missing, non-numeric, non-finite, and unrepresentable values
are excluded from Recorder rather than guessed, clamped, or replaced.

Temperature statistics use Home Assistant recorder temperature metadata, so Home Assistant can display them in the preferred frontend unit.

These are intended for long-term charts and Home Assistant dashboards. They are not a replacement for live HomeKit/Ecobee entities.

## Supported Scope

This integration is designed for Ecobee thermostats and Ecobee room sensors that are visible in Beestat. It enriches local HomeKit Controller devices when names can be matched, and supports YAML ID/entity overrides when automatic matching is not enough.

## Known Limitations

Beestat's public API is useful but not versioned as a stable Home Assistant integration contract. This integration keeps calls narrow and exposes failures through diagnostic state instead of silently masking them.

Beestat is a cloud/history source. HomeKit/Ecobee entities should remain the primary source for live local temperature, occupancy, HVAC mode, setpoints, and control. Beestat alert entities mirror Beestat/Ecobee alert metadata and may include maintenance reminders rather than active equipment faults.

The scheduled comfort profile and next transition are reevaluated locally from
the last cached Beestat schedule. The current comfort profile remains delayed
cloud diagnostic context because an active hold cannot be reconstructed safely
from the schedule alone.

Removing the integration stops future imports and removes the integration's native entities, but Recorder external statistics already imported under source `beestat` may remain in Home Assistant's statistics database.

## Troubleshooting

- Check the Beestat Statistics **Status** sensor first. Its attributes include the latest error, runtime fetch time, summary row count, import mode/window/fallback details, automatic filter-alert dismissal results, and latest import row count.
- If setup reports an invalid or insecure API URL, open **Reconfigure** and save a valid HTTPS URL without user information, a query, or a fragment. The integration does not contact the rejected endpoint.
- If setup cannot identify the account, confirm that the Beestat account currently exposes at least one thermostat; the integration will not reuse an older account fingerprint for an empty response.
- If summary dates lag, press **Refresh Runtime** or call `beestat_statistics.import_statistics` without `skip_sync`.
- If Home Assistant asks for reauthentication, enter a current Beestat API key in the reauth flow.
- If a thermostat or room sensor is absent, check **Choose Beestat sources** in the integration options.
- If automatic HomeKit mapping misses an included thermostat or room sensor, use **Map a thermostat** or **Map a room sensor** in the integration options. Use advanced YAML only for recovery or bulk configuration.
- If a filter forecast is unavailable, check the thermostat **Filter changed date**, **Filter runtime hours**, and **Filter recent runtime hours per day** entities first.
- If an existing install is upgraded from a release where runtime/cloud stale problem sensors were disabled by default, the integration enables only those integration-disabled stale diagnostic entities during setup. User-disabled entities remain disabled.

## Development Validation

Home Assistant `2026.8.0` requires Python `3.14.2` or newer. The GitHub validation workflow uses Python `3.14`; use the same major version for any local Home Assistant test harness work.

This repository is a HACS custom integration. The Beestat API client is intentionally in-tree and uses Home Assistant's shared aiohttp websession. If this integration is ever prepared for Home Assistant Core inclusion, split the Beestat client into an async, tagged, open-source PyPI package before submission.

Local pure-module checks:

```powershell
.\.venv\Scripts\python.exe -m pip install "ruff==0.16.2" "mypy==2.3.0" "shellcheck-py==0.11.0.1" "zizmor==1.29.0"
.\.venv\Scripts\python.exe scripts\run_dependency_light_tests.py
.\.venv\Scripts\python.exe -m compileall -q custom_components\beestat_statistics tests scripts
.\.venv\Scripts\ruff.exe check custom_components tests scripts
.\.venv\Scripts\ruff.exe format --check custom_components tests scripts
.\.venv\Scripts\shellcheck.exe scripts\verify-release-local.sh
.\.venv\Scripts\python.exe -m mypy --strict custom_components/beestat_statistics
$env:GH_TOKEN = gh auth token
if (-not $env:GH_TOKEN) { throw "GitHub CLI authentication required" }
try {
  .\.venv\Scripts\zizmor.exe --strict-collection --persona auditor .
  if ($LASTEXITCODE -ne 0) { throw "zizmor audit failed" }
} finally {
  Remove-Item Env:GH_TOKEN
}
```

Upstream Beestat API drift check:

```powershell
.\.venv\Scripts\python.exe scripts\check_beestat_api_surface.py
```

The checked-in snapshot is `docs/beestat-api-surface.json`. Review upstream changes before refreshing it with `--update`; do not treat a changed snapshot as approval to broaden the Home Assistant integration scope.

The checked-in `custom_components/beestat_statistics/quality_scale.yaml` tracks Home Assistant integration-quality rules with current repo evidence, including strict typing. Omitted rules are intentionally unclaimed until matching coverage or runtime evidence exists.

Home Assistant harness checks require Linux with Python `3.14`. The supported-minimum lane is dependency-closed at Core `2026.8.0`, matching published harness `0.13.354`, and a second dependency-closed lane targets exact current same-month patch Core `2026.8.1` with harness `0.13.355`. Each lane installs its exact harness and Core requirements separately, runs a literal `python -m pip check` after the final dependency installation, and then runs the complete Home Assistant tests. Home Assistant imports Linux-only modules and its test harness assumes Unix-domain sockets, so a native Windows Python environment is not a valid substitute even when its Python version matches.

Supported-minimum lane:

```powershell
python -m pip install pytest-homeassistant-custom-component==0.13.354
python -m pip install --upgrade -r requirements-ha-test.txt
python -m pip check
pytest tests -q
```

Current same-month patch lane:

```powershell
python -m pip install pytest-homeassistant-custom-component==0.13.355
python -m pip install --upgrade -r requirements-ha-current.txt
python -m pip check
pytest tests -q
```

On Windows, run the same harness through Docker Desktop or WSL from the repository root:

```powershell
docker run --rm -v "${PWD}:/work" -w /work python:3.14-slim bash -lc "python -m pip install --upgrade pip && python -m pip install pytest-homeassistant-custom-component==0.13.354 && python -m pip install --upgrade -r requirements-ha-test.txt && python -m pip check && pytest tests -q"
```

The workflow pins every third-party action to a full commit SHA and runs
exact-pinned Ruff, mypy, actionlint, ShellCheck, and `zizmor` in auditor mode.
Dependabot proposes weekly GitHub Actions updates after a seven-day stability
and supply-chain cooldown. The stable **Release gate** check succeeds only when
unit, both dependency-closed Home Assistant lanes, Hassfest, and HACS validation
all succeed.

## Release Publishing

Every release follows this order:

1. Create a release-candidate branch from current `main`.
2. Open a pull request and require terminal success for **Unit tests**, **Home
   Assistant minimum integration tests (Core 2026.8.0)**, **Home Assistant
   current-patch integration tests (Core 2026.8.1)**, **Hassfest**, **HACS**,
   the aggregate **Release gate**, and CodeQL's **Analyze (actions)**, **Analyze
   (python)**, and **CodeQL** checks.
3. Merge through default-branch protection without bypass, using squash or
   rebase so history remains linear.
4. On the resulting `main` commit, require a successful **Validate** push run
   and CodeQL analysis. Inspect the complete logs and open code-scanning alerts;
   workflow success proves analysis completed, not that it found nothing.
5. Resolve or explicitly disposition candidate-introduced alerts, then align
   the manifest version, immutable `vYYYY.M.D` tag, and GitHub Release to that
   exact `main` commit.
6. Treat HACS selection or installation, the Home Assistant configuration
   check, restart, live validation, migration, and rollback as later, separately
   gated phases. A source push or GitHub Release alone is not a completed Home
   Assistant deployment.

Before publishing a release intended for HACS, verify the repository still has a public description, relevant Home Assistant/HACS topics, issues enabled, a brand icon, passing unit and Home Assistant tests, passing Hassfest, passing HACS Action, and a GitHub release tag matching the manifest version.

When publishing manually with GitHub CLI, write the release body to a Markdown file and pass it with `--notes-file`. Avoid PowerShell strings containing `\n`; GitHub renders those as literal backslash-n text.

```powershell
gh release create vYYYY.M.D --title vYYYY.M.D --notes-file release-notes.md
gh release edit vYYYY.M.D --notes-file release-notes.md
```

## Removal

1. In Home Assistant, remove the **Beestat Statistics** integration entry from **Settings > Devices & services**.
2. If installed through HACS, remove **Beestat Statistics** from HACS.
3. Restart Home Assistant after removing the custom integration files.

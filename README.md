# Beestat Statistics

Beestat Statistics brings Ecobee history and cloud context from Beestat into
Home Assistant. It imports long-term statistics, enriches existing thermostat
and room-sensor devices, and provides local calculations such as comfort-profile
temperature spread and filter replacement forecasts.

Use HomeKit/Ecobee entities for live temperature, occupancy, HVAC mode, setpoints
and control. Beestat is a secondary cloud and history source; its reported state
can arrive later than the local thermostat state.

## What It Provides

| Need | Provided by this integration |
|---|---|
| Historical charts | Daily Recorder statistics for HVAC runtime, temperatures, setpoints, occupancy, weather context and supported air-quality fields |
| Cloud context | Comfort schedules, reported sensor use, alerts, source freshness and selected Ecobee settings |
| Device presentation | Beestat entities attached to matching HomeKit/Ecobee devices, with fallback devices when no match exists |
| Local calculations | Configured-profile room temperature spread and filter forecasts calculated in Home Assistant from Beestat history and local settings |

Filter tracking includes a local replacement baseline, runtime calculations,
forecasts and recording actions. You choose maintenance limits and establish
when physical work occurred. Household inspection schedules, reminders, task
completion and maintenance history belong to your Home Assistant workflows or
other maintenance system. See [integration boundaries](docs/architecture.md#integration-boundaries).

## Installation With HACS

Requires Home Assistant **2026.8.0 or later**, Recorder, and a Beestat API key
for an account with at least one identifiable thermostat. Local HomeKit/Ecobee
entities are recommended for device enrichment; they are not required to import
Beestat history.

[Open this repository in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=ItsColby&repository=beestat-statistics&category=integration)

1. In HACS, open **Custom repositories** and add
   `https://github.com/ItsColby/beestat-statistics` as type **Integration**.
2. Download **Beestat Statistics** and restart Home Assistant.
3. Go to **Settings > Devices & services > Add integration** and select
   **Beestat Statistics**.
4. Enter your Beestat API key. Leave the API URL at its default unless you
   intentionally use a different supported endpoint.
5. Review source selection and local device mappings in the integration options.

One integration entry owns the account connection and its selected thermostats
and room sensors. Multiple entries and config subentries are not supported.

## Configuration

Use the integration options for routine changes. No thermostat IDs or room
names are required for automatic discovery.

| Option | Default and purpose |
|---|---|
| **Choose Beestat sources** | Include discovered active thermostats and room sensors; explicitly exclude sources you do not want |
| **Confirm automatic mappings** | Review and pin unambiguous matches to local HomeKit/Ecobee entities |
| **Map a thermostat** / **Map a room sensor** | Correct a mapping or set per-source options |
| `point_lookback_days` | 45 recent local days of thermostat and room-sensor point history, up to 366 |
| `scan_interval_seconds` | 21,600 seconds (6 hours); allowed range 300 seconds to one year |

New active sources are included unless explicitly excluded. Inactive sources
can be selected deliberately. Exclusion stops native entity updates and future
imports for that source; previously imported Recorder statistics remain.

### Local Device Mappings

Automatic matching uses Ecobee/HomeKit device identity and names. Ambiguous
matches stay unresolved. Review **Confirm automatic mappings** before pinning
matches, or correct an individual source through its mapping form. Explicit
mappings must identify one physical device and cannot claim a device already
assigned to another mapping of the same kind. Conflicts raise a Repair and
suppress the affected projections instead of choosing a match arbitrarily.

Confirmed UI mappings follow entity-ID renames and removal/restoration through
stable registry identity. Device links enrich the existing HomeKit/Ecobee device
without taking ownership of it or rewriting its name, model or manufacturer.

An explicitly selected Ecobee built-in thermostat probe must match the
thermostat hardware identity. Its reading stays distinct from the thermostat's
displayed temperature, which may combine rooms. That probe can still depend on
Ecobee cloud availability; an unavailable probe is not replaced with the
displayed temperature.

See [source identity and mapping](docs/architecture.md#source-identity-and-device-mapping)
for conflict handling, source selection and device migration details.

### Connection Changes

Use **Reconfigure** to change the API key or URL. Reauthentication starts when
Beestat rejects the saved key. The API URL must use HTTPS and contain no user
information, query or fragment; credential-bearing requests do not follow
redirects.

The integration verifies account continuity before saving a new connection.
A possible account change needs explicit confirmation and resets source
selections and per-source overrides. Existing Recorder history remains, and
sources with overlapping stable slugs can continue those series. Treat an
account change as a history-boundary decision.

### YAML Compatibility

YAML is supported for bootstrap, recovery and declarative configuration:

```yaml
beestat_statistics:
  api_key: !secret beestat_api_key
  point_lookback_days: 45
  scan_interval:
    hours: 6
```

YAML creates or updates the config entry on startup. If it was only used for
bootstrap, remove the block after verifying the entry works and make subsequent
changes in the UI. Keep the block when you intentionally want YAML to remain
the configuration owner.

When automatic matching is insufficient, YAML can pin sources to local entities:

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

The example IDs and entity names are placeholders. Available fields are:

| Scope | Fields |
|---|---|
| Connection | `api_key`; optional `api_base`, default `https://api.beestat.io/` |
| Import timing | `point_lookback_days`; YAML `scan_interval` |
| Either source row | `id`, optional `slug`, `name`, `temperature_entity_id`, `occupancy_entity_id`, `motion_entity_id`, `enabled` |
| Thermostat row | `climate_entity_id`, `filter_changed_entity_id`, `filter_changed_date`, `filter_lifetime_runtime_hours`, `filter_max_age_days`, `filter_notice_days` |
| Room-sensor row | `thermostat_id`, `include_temperature`, `include_air_quality`, `include_co2`, `include_voc` |

A `slug` pins Recorder statistic IDs and the thermostat's default legacy-helper
lookup; `name` pins fallback labels. Prefer local device names unless those
identities need to be fixed. Use **Choose Beestat sources** for normal source
selection; `enabled: false` remains an advanced override. Update YAML entity IDs
manually after renames.

A native **Mark filter changed** baseline survives YAML reimports unless YAML
explicitly supplies `filter_changed_date`. That date-only input takes precedence
and clears the exact timestamp. Use **Reconfigure** before changing YAML
credentials when they may belong to another account, then make YAML match the
saved connection. The [configuration and continuity reference](docs/architecture.md#configuration-and-account-continuity)
describes validation, precedence and retained state.

## Entities

The Beestat service device provides **Status**, sync/import timestamps, mapping
and partial-import problem sensors, and **Refresh runtime** / **Import
statistics** buttons. Advanced import counters are disabled by default.

Thermostat devices expose comfort schedules, alerts, filter controls and due
dates. Freshness, the last cloud-reported comfort profile, reported sensor-use
counts, runtime details and intermediate forecast dates are diagnostic
entities. Advanced Ecobee setting entities are disabled by default.

Keep these distinctions in mind when building a dashboard:

- **Scheduled comfort profile** and the next transition are local projections
  of the cached Beestat schedule. **Current comfort profile** is the last
  cloud-reported profile; neither establishes the thermostat's live hold state.
- **Beestat-reported sensor in use** is upstream `in_use` metadata. It is not
  configured profile membership or Follow Me's momentary weighting. Missing
  metadata is unknown, not off or zero.
- **Configured profile room temperature spread** uses mapped local temperature
  readings for the last cloud-reported profile's configured sensors. With at
  least two valid readings it can report a partial spread; check its coverage
  attributes because omitted rooms can make the range look smaller.
- Active-alert sensors mirror Beestat/Ecobee alerts. Equipment-looking or
  unknown alerts also appear as problem sensors; a routine maintenance reminder
  alone is not presented as an equipment failure.

Diagnostic context is available in current entity attributes but excluded from
Recorder history to avoid repeated metadata storage. The [entity and action
reference](docs/architecture.md#entities-and-actions) covers these surfaces and
their availability semantics.

## Filter Tracking

Filter forecasts are **calculated in Home Assistant** from Beestat fan-runtime
history and your local settings. They are not Beestat-provided replacement
recommendations or a measurement of filter condition.

Set the limits under **Map a thermostat**:

| Setting | Default | Meaning |
|---|---|---|
| `filter_lifetime_runtime_hours` | 250 hours | Observed fan-runtime replacement threshold |
| `filter_max_age_days` | 90 days | Calendar limit from the effective replacement date |
| `filter_notice_days` | 7 days | Advisory window before the calculated due date |

These defaults are starting values, **not manufacturer recommendations**.
Choose limits appropriate to your equipment, filter and maintenance guidance.
The forecast uses the earlier runtime or calendar date.

Press **Mark filter changed** when replacing a filter. It records the exact
press timestamp locally before contacting Beestat. For a date correction, edit
**Filter changed date**; a date-only edit clears the exact timestamp. The
effective date comes from the local override, then a configured legacy
`input_datetime` helper, then Beestat/Ecobee filter metadata.

Runtime counts observed fan exposure after replacement. Five-minute source
precision leaves the interval straddling an exact replacement uncounted;
date-only input omits the whole ambiguous change day. Missing history remains
unknown exposure and cannot be reconstructed by this integration. The observed
runtime is a lower bound, and remaining runtime can be an upper bound.

The recent demand estimate uses complete days from the previous 30 local days,
excluding today and incomplete or invalid days. A projected runtime due date can
therefore change. Once observed runtime proves the limit reached, the date stays
pinned to the first qualifying source day. The calendar limit remains usable
when runtime coverage is incomplete. **Filter due** reports a proven runtime or
calendar limit; **Filter due soon** also considers the forecast notice window.

For automations, read the **Filter due date** entity's coherent snapshot and
`forecast_revision`, including coverage, source horizon and projection flags.
Do not assemble a forecast by reading sibling sensors during sequential updates.
A boundary awaiting Beestat data does not undo a saved replacement. The date
entity retains only the latest replacement/correction receipt, not maintenance
history or independent proof that physical work occurred.

Recording a replacement or editing the date also attempts to dismiss matching
Beestat filter alerts. Neither writes Ecobee settings or Beestat's sync-owned
filter metadata. See
[filter tracking](docs/architecture.md#filter-tracking) for exact timestamp,
coverage, correction and automation contracts.

## Data Updates And Recorder Statistics

Setup and the scheduled import sync Beestat runtime and metadata, refresh
entities, and import statistics. The default interval is six hours. Cached
schedule and date-dependent projections update locally between cloud reads;
they do not require an automation. Pending filter boundaries have a separate
bounded retry schedule.

Cloud freshness is assessed against the configured import cadence. The stale
threshold is the larger of two hours or the import interval plus one hour of
source publication grace: seven hours at the default cadence. Freshness and
history coverage describe different things; fresh data can still contain gaps.

External Recorder statistics use source `beestat` and include:

- Cumulative heating, cooling and fan runtime, supported stage/accessory runtime,
  and thermostat-summary heating/cooling degree days.
- Daily room temperatures, thermostat heat/cool setpoints, indoor humidity,
  outdoor temperature and outdoor humidity.
- Daily room occupancy when a mapped local entity establishes that capability,
  and supported air quality, CO2 and TVOC fields.

Stage/accessory series are created only after nonzero runtime is observed.
Temperature statistics support Home Assistant's preferred display unit. Valid
air-quality spikes are retained; missing or invalid measurements are not
replaced, clamped or invented.

Routine cumulative imports reuse a seven-day summary overlap when Recorder has
a trustworthy prior total, otherwise they fall back to the full baseline.
Point-history statistics use the configured lookback. See [acquisition and
local projection](docs/architecture.md#acquisition-and-local-projection) and
[Recorder statistics](docs/architecture.md#recorder-statistics) for source
windows, correction handling and timezone behavior.

## Actions And Automations

Normal operation requires no automation. Use the buttons for a manual refresh
or call these actions from Home Assistant:

| Action | Use |
|---|---|
| `beestat_statistics.import_statistics` | Sync Beestat, refresh entities and import daily statistics; accepts `point_lookback_days` and `skip_sync` |
| `beestat_statistics.rebuild_statistics` | Repair or backfill statistics from the full summary baseline; accepts `thermostat_id`, `start_date`, `end_date` and `skip_sync` |
| `beestat_statistics.get_configuration` | Read cached configuration and mappings without cloud I/O or state changes; requires `config_entry_id` |
| `beestat_statistics.record_filter_change` | Record an actual replacement at a supplied timestamp with replay and stale-state safeguards |
| `beestat_statistics.repair_filter_change_boundary` | Assign a verified recent timestamp to a saved Home Assistant filter-date override; requires `config_entry_id`, `thermostat_id` and `changed_at` |

**Refresh runtime** refreshes Beestat entity data without importing Recorder
statistics. For import/rebuild actions, use `skip_sync` only when Beestat was
just synced; it skips sync requests, not Beestat history reads. A rebuild's
`end_date` limits measurement statistics; cumulative
statistics rewrite the affected tail through the latest source day so later
totals do not retain an old offset.

`record_filter_change` requires `config_entry_id`, `thermostat_id`, `changed_at`,
`expected_changed_at`, `expected_changed_date`, `expected_request_id` and
`request_id`. Capture the expected fields from the saved date entity with the
maintenance cycle and preserve them across retries. A new recording must be
within the last 31 days and later than the prior exact timestamp. Check the
response: `superseded` is not successful completion, and `pending_data` does not
mean a saved change failed. Read the [timestamped action and replay contract](docs/architecture.md#timestamped-action-and-replay)
before connecting another maintenance workflow.

The repair action requires a timestamp on the saved Home Assistant override's
local date; an effective date inherited from Beestat or a helper is insufficient.
Its runtime boundary can remain pending after the timestamp is saved, and it
does not dismiss alerts. For either timestamped action, use an explicit UTC
offset during a repeated daylight-saving hour; nonexistent local times are
rejected. These actions record supplied evidence and do not establish that
physical work happened.

For optional stale-data notifications, use the [Beestat runtime data stale
notification blueprint](blueprints/automation/beestat_statistics/stale_runtime_notification.yaml).
Import it from this URL:

```text
https://raw.githubusercontent.com/ItsColby/beestat-statistics/main/blueprints/automation/beestat_statistics/stale_runtime_notification.yaml
```

There are no custom device triggers or conditions. The [action reference](custom_components/beestat_statistics/services.yaml)
and [architecture](docs/architecture.md#entities-and-actions) describe the
supported fields and response behavior.

## Diagnostics And Troubleshooting

Start with the Beestat Statistics **Status** sensor and Home Assistant Repairs.
The status attributes expose the last error, fetch/import times, import coverage
and mapping counts.

| Symptom | Check |
|---|---|
| Setup cannot identify an account | Confirm the account exposes at least one identifiable thermostat in Beestat |
| Invalid API URL or rejected credentials | Use **Reconfigure** for a valid HTTPS URL, or complete the native reauthentication flow |
| Missing thermostat or room sensor | Review **Choose Beestat sources**, including explicit exclusions and inactive sources |
| Wrong or unresolved local device | Review **Confirm automatic mappings**, **Map a thermostat** or **Map a room sensor** and resolve the Repair |
| Old summary dates | Press **Refresh runtime** or run `import_statistics` without `skip_sync`; inspect source freshness and partial-import status |
| Unavailable or uncertain filter forecast | Check the changed date, observed runtime, recent complete-day count and coverage attributes |
| A profile or sensor-use value differs from the thermostat | Check cloud freshness and distinguish reported state, configured membership and local control |

Downloadable diagnostics from the integration entry contain redacted aggregate
configuration, health and import evidence. They exclude credentials, private
source identifiers, exact filter-change details and raw history. Inspect any
attachment before sharing it.

For an exact local audit, `get_configuration` returns saved overrides, effective
mappings, profiles and allow-listed cached Ecobee settings. It includes local
names and IDs, so treat that response as private configuration and do not attach
it to public issues. See [privacy and diagnostics](docs/architecture.md#privacy-and-diagnostics).

If a Beestat-only fallback device disappears from current source metadata, you
can remove the stale device manually from its device page. Shared HomeKit/Ecobee
devices are not removed by this integration. User-disabled entities remain
disabled during upgrades.

Source gaps and API changes can limit what is available. A successful refresh
does not restore history absent from Beestat. Report reproducible defects through
the [issue tracker](https://github.com/ItsColby/beestat-statistics/issues), with
redacted diagnostics and the affected integration/Home Assistant versions.

## Removal

1. Remove the **Beestat Statistics** integration entry from **Settings >
   Devices & services**.
2. Remove **Beestat Statistics** from HACS if installed there.
3. Restart Home Assistant after removing the custom integration files.

Removal stops future imports and removes native integration entities. External
Recorder statistics already imported under source `beestat` may remain.

## Development

Run all local checks through the repository's pinned containers. On Windows,
use Ubuntu 24.04 WSL2 with rootless Podman:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-release-local.ps1
```

On Linux:

```bash
bash scripts/verify-release-local.sh all
```

`requirements-ha-test.txt` pins the minimum Core lane (`2026.8.0`), and
`requirements-ha-current.txt` pins the current lane (`2026.9.1`). Both use Linux,
Python 3.14.2 or newer and matching Home Assistant test harnesses. Native Windows
Python cannot substitute for those harnesses. The runner checks dependency
closure with `python -m pip check` and runs the complete test tree.

Use `-Mode unit`, `minimum`, `current` or `release` for a focused Windows lane.
The `release` lane validates metadata; it does not publish. See [development and
validation](docs/architecture.md#development-and-validation) for dependency-light
tests, CI, API drift checks and the protected publication/release process.
[RELEASE_NOTES.md](RELEASE_NOTES.md) records user-visible changes; the
[quality scale](custom_components/beestat_statistics/quality_scale.yaml) lists
integration-quality claims and their evidence.

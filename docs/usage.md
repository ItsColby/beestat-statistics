# Using Beestat Statistics

After [installation and connection](../README.md), use this guide to choose
sources, interpret their data, and maintain filter records. Thermostat control
continues through the integration that already provides it. Beestat Statistics
adds historical statistics, cloud context, and locally calculated filter
estimates.

## Choose what to include

Open the Beestat Statistics entry under **Settings → Devices & services**, then
open its options.

- **Choose Beestat sources** selects thermostats and room sensors for entities
  and future imports. Newly discovered active sources are included unless
  explicitly excluded. Excluding a source requires confirmation; its existing
  Recorder statistics remain. Inactive sources can be included deliberately.
- **Confirm automatic mappings** previews available HomeKit matches and saves
  them as explicit entity-registry references. Unresolved or conflicting
  matches remain unresolved. Review again if the preview changes before save.
- **Map a thermostat** selects its climate, temperature, occupancy, and motion
  entities and configures its filter policy.
- **Map a room sensor** selects its local entities, optional owning-thermostat
  override, and temperature, air-quality, CO2, and TVOC imports.

Map entities representing the same physical device. A device cannot supply two
mappings of the same type; a thermostat probe from another integration needs a
matching hardware serial. Saved registry references follow entity renames.
Clearing an entity override permits automatic matching again. Conflicts disable
affected device linking and temperature projections and create a Repair.
Unmatched sources can still supply Beestat data on integration-owned devices.
An explicitly selected Ecobee built-in probe remains distinct from the displayed
thermostat temperature, which may combine rooms. If that cloud probe becomes
unavailable, the displayed temperature does not replace it.

**Import timing** controls scheduled syncs and Recorder imports:

| Option | Default | Allowed range |
| --- | --- | --- |
| Point-history lookback | 45 local days | 1–366 days |
| Import interval | 21,600 seconds (6 hours) | 300–31,536,000 seconds |
| Filter lifetime | 250 fan-runtime hours | 1–10,000 hours |
| Filter maximum age | 90 days | 1–730 days |
| Filter notice | 7 days | 0–365 days |

Filter limits belong to each thermostat. They are starting values for your
maintenance policy, not manufacturer recommendations. A shorter import interval
cannot make Beestat publish sooner; a larger lookback increases import work.

## Connection and YAML ownership

Only one Beestat Statistics entry is supported. **Reconfigure** validates a
replacement API key or endpoint; a blank key keeps the saved key.
**Reauthenticate** requires a valid replacement key. The endpoint must use HTTPS
and contain no embedded credentials, query string, or fragment. The saved key
is sent to that endpoint, so change it only for an intended compatible service.

Connection validation also checks account continuity using thermostat identity.
A connection without identifiable thermostats cannot establish continuity.
If the account cannot be matched, explicit confirmation is required. Continuing
clears source selections and per-source overrides, including mappings, filter
policy, and saved filter changes. Recorder history remains. Sources missing
from the new account stop updating; a new source with the same stable slug may
continue an existing statistics series.

YAML is optional. Use actual Beestat numeric IDs and existing local entity IDs
in this illustrative `configuration.yaml` block:

```yaml
beestat_statistics:
  api_key: !secret beestat_api_key
  point_lookback_days: 45
  scan_interval:
    hours: 6
  thermostats:
    - id: 12345
      slug: main
      climate_entity_id: climate.main
      filter_lifetime_runtime_hours: 250
      filter_max_age_days: 90
      filter_notice_days: 7
  sensors:
    - id: 67890
      thermostat_id: 12345
      slug: study
      temperature_entity_id: sensor.study_temperature
      include_temperature: true
      include_air_quality: false
```

Source rows support `name`, `slug`, and `enabled`; `enabled: false` excludes a
source. Thermostats additionally accept `temperature_entity_id`,
`occupancy_entity_id`, `motion_entity_id`, `filter_changed_date`, and an
`input_datetime` `filter_changed_entity_id`. Room sensors accept occupancy and
motion mappings plus `include_co2` and `include_voc`. The climate mapping requires
`climate`, temperature requires `sensor`, and occupancy/motion require
`binary_sensor` entities. `api_base` optionally replaces the default
`https://api.beestat.io/`.

YAML imports at startup; remove its block after bootstrap if the UI should own
routine configuration. A supplied thermostat or sensor collection regains
ownership on reimport and can replace its UI mappings and settings. Saved native
filter boundaries survive for matching thermostat rows unless YAML explicitly
supplies `filter_changed_date`: that date takes precedence and clears the exact
timestamp.
Update YAML entity IDs manually after renames. Use `get_configuration` to distinguish
saved overrides from effective configuration. A YAML connection change that
cannot match the saved account is blocked: use Reconfigure, then align YAML
with the accepted connection or remove the YAML block.

## Read the data at the right level

### Historical statistics

Recorder imports use external IDs beginning `beestat:`. These are statistics,
not ordinary sensor entities. Daily cumulative series include combined cooling,
heating, and fan runtime; individual compressor/auxiliary stages and available
humidifier, dehumidifier, ventilator, and economizer runtime; and heating/cooling
degree days. Runtime uses hours.
Stage/accessory series appear after nonzero runtime is observed. Once imported,
they remain eligible for corrections to zero; never-observed hardware stays
omitted.

Daily measurement series include indoor/outdoor humidity, outdoor temperature,
heat/cool setpoints, and selected room-sensor temperature, air quality, CO2, and
TVOC. Occupancy history is included for a room sensor with an occupancy mapping.
Temperature statistics use °F; humidity, air quality, and occupancy use percent;
CO2 uses ppm and TVOC ppb. Five-minute point history is aggregated into daily
mean/min/max rows, not imported as five-minute Recorder history. Available source
fields determine which statistics have values.
Home Assistant can convert temperature statistics to its preferred display unit.

For a thermostat slug `main`, examples include
`beestat:main_fan_runtime_hours` and `beestat:main_cool_setpoint`; a room-sensor
slug `study` produces `beestat:study_temperature`. Keep slugs stable when history
continuity matters. Entity identity uses numeric source IDs and is separate
from these statistic IDs. Excluding or removing a source does not delete history.

### Context and health entities

| Surface | Interpretation |
| --- | --- |
| Current comfort profile | Beestat's reported current profile context. |
| Scheduled comfort profile / next scheduled time | Schedule projected from cached program data in the thermostat's timezone when supplied, with Home Assistant's timezone as fallback. |
| Beestat-reported sensor in use / in-use sensor count | Cloud-reported participation; not a fresh local occupancy reading. |
| Configured profile room temperature spread | Spread of mapped local temperatures for configured profile membership; inspect coverage attributes before using it. |
| Active alert / count / category | Upstream alerts, classified to distinguish maintenance from equipment concerns. |
| Equipment alert | Equipment/other actionable alert classification rather than filter maintenance alone. |
| Runtime summary latest date / lag / stale | Age of daily summary coverage. |
| Cloud data end / lag / stale | Age of the source's data horizon, independent of a successful request. |
| Status / sync timestamps | Acquisition and import health. |
| Import partial / skipped windows | Whether unsupported point-history windows were omitted. |

Profiles do not establish live hold state. Reported sensor use is separate from
configured membership and Follow Me weighting; missing metadata is unknown.
Spread needs at least two valid local readings and can understate the full range
when rooms are missing. Its attributes identify the profile and coverage used.

Selected cached Ecobee settings are also exposed as diagnostic entities,
including differentials, compressor protection, dissipation times, hold behavior,
alert thresholds, service reminders, comfort features, and available audio
settings. These details are disabled by default and are read-only. Unsupported
settings can be unavailable; they are not inferred.

Startup performs an initial runtime refresh, then a background statistics import
without another sync request. Scheduled imports use the configured interval.
Cached schedule transitions, local-date changes, and freshness thresholds are
reevaluated between polls. Mapped local temperature changes update the room
spread without a cloud request. This does not make cached cloud values live.

## Maintain a filter record

### Choose the operation by what happened

| Situation | Operation and effect |
| --- | --- |
| You replaced the filter now | Press **Mark filter changed**. Saves the press time as a replacement and attempts to acknowledge matching filter alerts. |
| You are recording a replacement at its actual earlier time | Use `record_filter_change` with the prior boundary and a unique request ID. |
| The saved replacement date is wrong | Edit **Filter changed date**. Saves a correction and clears the exact-time boundary without acknowledging filter alerts. |
| The date is right and you now know its exact time | Use `repair_filter_change_boundary`. Saves a correction and refreshes runtime; it does not acknowledge alerts or change Ecobee settings. |

The effective replacement boundary prefers a saved exact timestamp, then a
Home Assistant date override, then a configured/discovered helper, then Beestat's
date. A date-only record cannot locate the replacement within that day. A saved
exact boundary uses five-minute Beestat history to separate earlier fan runtime
from the new filter's runtime; a partial five-minute interval remains uncertain.
The latest `filter_change_event` describes the saved action and its source. It is
neither a full maintenance history nor independent proof of physical work.
Date-only input omits the ambiguous change day. None of these operations writes
Ecobee settings or Beestat's sync-owned filter metadata.

New timestamped replacements and repairs must be within the last 31 days and
not in the future. A new replacement must follow the saved exact timestamp and
cannot predate the saved override date. A repair must match that date in Home
Assistant's timezone. Include a UTC offset for a repeated local clock time;
ambiguous or nonexistent offset-free local times are rejected.

### Interpret the estimate and its uncertainty

Filter runtime counts observed fan operation. Missing or invalid source records
are gaps, not zero runtime. The recent rate uses complete days in the preceding
30-day window, excluding today and incomplete days. The runtime due date projects
remaining hours at that rate; the maximum-age date applies the calendar limit.
**Filter due date** is the earlier available date. **Filter due soon** follows
that projected date and the configured notice window, independently of **Filter
due**. It remains on at and after the projected date; a zero-day notice starts on
that date. It sends no notification by itself.

**Filter due** is true when the calendar limit has expired or observed runtime
proves the runtime limit was reached. A projected due date alone is not that
proof. The binary sensor can be unknown when missing exposure prevents proving
the filter remains below the limit. Once observed runtime reaches the limit,
gaps cannot undo that proof; the calendar limit also remains usable.

Read the quality attributes alongside the number: `runtime_coverage`,
`runtime_is_lower_bound`, `runtime_source_data_end`,
`runtime_unknown_interval_minutes`, `runtime_boundary_uncertainty_minutes`, and
`runtime_threshold_reached`. When runtime is a lower bound, remaining runtime is
an upper bound and the runtime-based due date is provisional. Recent complete
and excluded day counts describe the rate's evidence.

For automations, read the complete **Filter due date** attribute snapshot and
`forecast_revision` rather than joining sibling sensors during sequential updates.
These current-state details, including exact boundaries and receipts, are
excluded from Recorder history.

A saved replacement can return successfully with `boundary_status: pending_data`.
The integration retries pending exact boundaries every 15 minutes while the
replacement timestamp is within its six-hour fast-retry window. An older
replacement recorded now does not start a new six-hour window. Regular refreshes
can reconcile it later. `finalized` means the saved change-day baseline was
reconciled; inspect current runtime quality for later gaps and source corrections.

## Call actions and handle their results

Use **Developer tools → Actions** or an automation. The authoritative field
reference is [services.yaml](../custom_components/beestat_statistics/services.yaml).
Replace illustrative IDs and dates before running examples.

To inspect local configuration without network access or mutation:

```yaml
action: beestat_statistics.get_configuration
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
response_variable: beestat_configuration
```

The response includes timing, saved overrides, effective mappings and filter
quality, comfort profiles, and allowed cached hardware/Ecobee settings. It does
not include the API key, but contains private local names and IDs; do not attach
this response to public issues.

To refresh entities and import recent point history:

```yaml
action: beestat_statistics.import_statistics
data:
  point_lookback_days: 45
  skip_sync: false
```

The **Refresh runtime** button updates current integration data; **Import
statistics** also writes Recorder statistics. `skip_sync: true` skips Beestat sync
requests only. Imports and rebuilds still read the network.

To rewrite statistics after an older source correction:

```yaml
action: beestat_statistics.rebuild_statistics
data:
  thermostat_id: 12345
  start_date: "2026-08-01"
  end_date: "2026-08-31"
```

Omit `thermostat_id` to rebuild all configured sources. Earlier available summary
history is read to seed cumulative totals. Measurement writes obey the selected
date range, but cumulative writes continue from the start through the latest
source day so later totals remain consistent. `end_date` is therefore not a hard
write boundary for cumulative series. The start cannot follow the end.

For a physical replacement, first capture `filter_changed_at`,
`home_assistant_override_date`, and `filter_change_event.request_id` from the
Filter changed date entity. Supply those prior values, including explicit
`null` when absent:

```yaml
action: beestat_statistics.record_filter_change
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
  thermostat_id: 12345
  changed_at: "2026-09-10T09:30:00-04:00"
  expected_changed_at: null
  expected_changed_date: null
  expected_request_id: null
  request_id: "main-filter-2026-09-10-0930"
response_variable: filter_change_result
```

Use a 1–128-character request ID for this replacement and retain the exact
request for retries. Handle `status` before continuing an automation:

- `recorded`: this replacement was saved; boundary reconciliation may be pending.
- `already_recorded`: the current saved request matches; effects were not
  repeated. Identical retries remain valid after the 31-day acceptance window.
- `superseded`: another change won before completion; inspect the current record.

A changed prior boundary/token or reused request ID with different contents is
rejected. Never refresh the expected values just to force an old request through.
The boundary is persisted before alert acknowledgment and runtime refresh;
network failure can leave the replacement saved. Read back after an uncertain
outcome, and retry the same request rather than creating a new replacement.

To refine an existing date without acknowledging its alerts:

```yaml
action: beestat_statistics.repair_filter_change_boundary
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
  thermostat_id: 12345
  changed_at: "2026-09-10T09:30:00-04:00"
```

## Resolve a problem

- **Connection/authentication:** follow Reauthenticate for rejected keys or
  Reconfigure for endpoint repairs. Invalid endpoints are blocked before the
  key is sent. A successful connection check with no identifiable thermostats
  cannot complete setup.
- **Missing or conflicting mappings:** inspect the Repair and effective
  configuration, select registered entities for the correct physical device,
  and reload when instructed. Run Refresh runtime if options have no discovered
  sources. Temporarily missing saved references are retained for recovery.
- **Old data:** compare source horizons with sync times. Runtime summary stale
  means more than one local day behind; cloud stale uses the larger of 120
  minutes or the configured interval plus 60 minutes. Repeated sync success
  does not prove the cloud source advanced.
- **Import gaps:** inspect Import partial, Skipped windows, and Status. Last
  import success can coexist with skipped windows. A supported window returning
  no rows is distinct from an unsupported skipped window. Increase lookback only
  when the desired point history is available; use rebuild for older summary
  corrections.
- **Unexpected filter estimate:** inspect the effective date source, boundary
  status, recent complete-day count, and runtime quality before changing policy.
  Use corrections for erroneous records, not another replacement action.

Download integration diagnostics and retain relevant sanitized log messages for
an issue. Diagnostics provide redacted aggregate evidence, excluding private
source identities, exact filter records, credentials, and raw history. Review
files before sharing them. Implementation contracts and source
limits are in [architecture](architecture.md); reproducible checks and
contribution guidance are in [development](development.md).

For optional notifications, the [stale-runtime blueprint](../blueprints/automation/beestat_statistics/stale_runtime_notification.yaml)
uses your chosen lag sensor, threshold, duration, and notification actions.
Use its [raw blueprint URL](https://raw.githubusercontent.com/ItsColby/beestat-statistics/main/blueprints/automation/beestat_statistics/stale_runtime_notification.yaml)
when importing it into Home Assistant.
Normal operation needs no automation; there are no custom device triggers or
conditions.

To uninstall, remove the integration entry, remove its HACS installation if
present, and restart Home Assistant after removing the integration files.
Native integration entities are removed and future imports stop; imported
external statistics may remain. Shared HomeKit/Ecobee devices are preserved.

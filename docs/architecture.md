# Architecture

Beestat Statistics connects Beestat cloud history and metadata to Home
Assistant entities, devices, actions and Recorder statistics. This document
owns the technical contracts and development workflow. Start with the
[README](../README.md) for installation and normal use;
[services.yaml](../custom_components/beestat_statistics/services.yaml) defines
the action schemas.

## Integration boundaries

The integration owns Beestat acquisition, normalization and source semantics.
It also provides useful local presentation and calculations over that source:
scheduled profile transitions, room-temperature spread, source health and a
generic filter-runtime forecast. Derived features identify their inputs,
freshness and uncertainty.

| Responsibility | Owner |
| --- | --- |
| Beestat credentials, discovery, source scope and cloud operations | This integration |
| History normalization, coverage, timestamp interpretation and imports | This integration; Recorder stores the statistics |
| Beestat metadata, alerts, cached settings and device enrichment | This integration |
| Optional filter tracking: saved baseline, observed runtime, generic forecast and latest mutation receipt | This integration |
| Equipment-specific thresholds, physical replacement decisions, inspections, task history, reminders and completion workflow | The user and their Home Assistant automations or other maintenance system |
| Live thermostat state, room readings, occupancy, motion, mode, setpoints and equipment control | Existing HomeKit/Ecobee integrations |
| Household dashboards, power/energy analysis, savings conclusions and cross-service automation | Home Assistant and its consumers |
| Installation and distribution | HACS and the product release |

A saved user-supplied baseline and configured thresholds do not make the
integration an equipment-maintenance authority. Its defaults are software
defaults, not manufacturer recommendations. It does not decide that physical
work occurred, keep complete maintenance history, create tasks, send
notifications or control thermostats. Technical source-health thresholds belong
here because they describe data validity rather than a household response.

One config entry owns one Beestat account connection and one account-wide
coordinator. Multiple entries, config subentries or a parallel importer require
a concrete need and statistics-continuity design. Attaching enrichment entities
to an existing device does not transfer its control or integration ownership.

### Beestat API scope

The in-tree async client uses Home Assistant's shared aiohttp session.

| Beestat surface | Purpose |
| --- | --- |
| `runtime.sync`, `thermostat.sync`, `sensor.sync` | Refresh upstream history and metadata |
| `thermostat.read_id`, `sensor.read_id` | Discovery, identity, matching and metadata |
| `ecobee_thermostat.read_id` | Immediately reduce cached Ecobee configuration to a privacy allowlist |
| `runtime_thermostat_summary.read_id` with date attributes | Daily summaries and cumulative imports |
| Windowed `runtime_thermostat.read`, `runtime_sensor.read` | Point statistics and exact filter change-day assessment |
| `thermostat.dismiss_alert` | Best-effort acknowledgement of matching filter alerts after a local filter update |

The integration does not call Ecobee directly, write Ecobee settings or overwrite
Beestat's sync-owned filter metadata. Broad thermostat updates, alert
restoration, comparison metrics and profile generation remain outside the
current scope. [beestat-api-surface.json](beestat-api-surface.json) records the
reviewed inventory and upstream fingerprints. New endpoints require updates to
that inventory, the checker, documentation, diagnostics and tests.

## Configuration and account continuity

### Configuration owners

The Home Assistant UI is the primary configuration surface. Config-entry `data`
owns the API key, validated URL and non-reversible account fingerprint.
Config-entry `options` owns import timing, source selection, mappings and local
filter/statistic overrides. Options-flow saves reload the entry.

| Setting | Default and bounds |
| --- | --- |
| API URL | `https://api.beestat.io/`; HTTPS without user information, query or fragment |
| Point-history lookback | 45 local days; 1–366 days; includes thermostat/setpoint and room-sensor history |
| Acquisition interval | Six hours; 300–31,536,000 seconds |
| Filter runtime limit | 250 hours; configurable up to 10,000 hours |
| Filter calendar limit | 90 days; configurable up to 730 days |
| Filter notice window | Seven days; configurable up to 365 days |

Setup and migrations normalize timing with the same bounds as the UI.
Malformed values fall back through that normalization; valid cadence is
preserved. Versioned storage migrations retain connection data, source flags,
mappings, timing and statistic slugs. Using an already-supported field in a UI
flow does not itself require a storage-version bump.

Thermostat and room-sensor overrides share `id`, `slug`, `name`, `enabled` and
supported temperature, occupancy and motion mappings. Thermostats add a climate
mapping and filter settings. Room sensors can override their parent
`thermostat_id`, including a parent reported by Beestat, and their temperature,
air-quality, CO2 and VOC import capabilities. Explicit slugs fix statistic
identity; changing one can create a different series. Submitted blank entity
selections clear those overrides; omitted fields preserve their existing values.

### Connection changes

Initial UI/YAML setup, reconfigure and reauthentication require an identifiable
thermostat anchor. An HTTP success with no thermostat cannot prove continuity
and does not save a new connection.

Reconfigure and reauthentication validate candidates before saving. A
fingerprint that cannot prove the same account requires confirmation of a
possible account change. Confirmation clears saved source scope and per-source
overrides, preserves timing and reloads the entry. Recorder statistics remain;
reused slugs may therefore continue older series. Account replacement is also
a history-boundary decision.

These flows snapshot the data they intend to retain or replace. A concurrent
entry-data change, or an options change that an account replacement would
overwrite, aborts the stale flow without saving or reloading. Same-account
connection updates preserve unrelated options.

### Source selection and effective rows

Selection combines fresh discovery, the runtime model and saved overrides so
excluded and temporarily missing resources remain recoverable. An absent
`enabled` flag allows ordinary discovery; `false` excludes a source, while
`true` can include one reported inactive. Exclusion stops native source updates
and future imports without deleting imported Recorder history.

Excluding an active source requires confirmation. Forms retain their displayed
source IDs, labels and active/inactive states. Drift before saving refreshes
the form rather than accepting a stale removal decision. Unrelated fields and
unknown saved rows remain intact.

One positive-integer parser owns resource identity across configuration,
acquisition, projections and statistics. Booleans, fractional and nonpositive
values are invalid. Raw rows prefer their resource-specific ID, then a valid
generic alias. An explicit override `id` is authoritative: an invalid value
leaves the row ignored and retained rather than using a parent thermostat ID
as the sensor ID.

For repeated legacy override IDs, the last row wins everywhere: runtime models,
forms, Repairs, conflicts and targeted mutations share that effective row.
Unknown fields and untouched legacy rows survive updates. Clearing inclusion
retains an empty winning row when removing it would reactivate an older duplicate.

### YAML compatibility

YAML is an import/recovery route. Remove its block after bootstrap when the UI
should own routine configuration; retain it only when intentionally declarative.
YAML `scan_interval` becomes `scan_interval_seconds`. Omitted YAML-owned mapping
collections are removed on import, while unknown entry-data fields remain.

A changed YAML credential is validated against the saved account. A different,
unavailable or unprovable account leaves the entry unchanged and raises a Repair
directing the user to Reconfigure. A verified same-account import, or removing
the block, clears that Repair. Awaited validation guards entry data, then merges
declared options against their current values immediately before saving.

When YAML owns thermostat mappings, local filter dates and exact click
boundaries are overlaid by Beestat source ID on later imports. An explicit YAML
`filter_changed_date` takes precedence as date-only input and clears the exact
timestamp. A changed explicit boundary, including loss of the timestamp on the
same date, records a `configuration` correction; unchanged imports preserve the
latest receipt. YAML entity IDs are not silently rewritten after registry renames.

## Source identity and device mapping

### Discovery and confirmation

Automatic discovery prefers HomeKit devices with Ecobee manufacturer/entity
signals. Ecobee-shaped devices can still match by name when that metadata is
absent. Names help onboarding; they are not permanent identity.

Mappings are one-to-one within each resource type. A unique name match can win
over a weaker single-device fallback. Equally confident sources competing for
the same device all remain unresolved. Explicit mappings reserve their devices
from automatic reuse and must resolve to one physical source device. Conflicts
suppress linking and temperature projections and raise a Repair.

**Confirm automatic mappings** reviews unambiguous cached matches in one
transaction, showing exact entities and leaving missing/conflicting matches
unresolved. It rechecks cached mappings and registry state before saving;
changed targets require confirmation again. The save preserves unrelated
options and causes at most one reload. Automatic matches are never persisted
without confirmation.

### Stable references and physical probes

Confirmed UI mappings store the foreign entity-registry UUID and
`(domain, platform, unique_id)`, plus the selection-time entity ID for local
readback and downgrade compatibility. Resolution uses the UUID, then the source
tuple. An unresolved reference cannot fall back to a different same-device
entity, mutable name or inherited thermostat sensor. Forms show resolved current
IDs without rewriting storage; unresolved references remain recoverable.

A versioned migration adds references to existing UI mappings only when the
registry proves them. Unresolved YAML or unmigratable mappings raise the mapping
Repair. Disabled source overrides do not raise missing-entity/mapping-domain
Repairs until enabled again.

An explicit Ecobee built-in thermostat probe can accompany the same thermostat's
HomeKit climate, motion and occupancy entities despite its separate cloud
device registration. The exception requires the Ecobee platform, exact
`<identifier>-ei:0-temperature` unique ID, one Ecobee device identifier and one
uniquely matching Ecobee-manufacturer HomeKit climate-device hardware serial.
Both registrations reserve the physical device against duplicate claims.
Ambiguous or contradictory evidence retains the conflict.

This proof does not merge devices, add aliases or change control ownership. An
unavailable selected physical probe remains unavailable; the thermostat's
displayed temperature, which may combine rooms, is not its fallback.

### Registry lifecycle

Enrichment entities attach to existing HomeKit/Ecobee devices without returning
foreign identifiers/connections from `device_info`, taking ownership or
rewriting metadata. Setup removes legacy shared ownership through Home
Assistant's helper-device migration API while preserving entity assignments.
Unmatched sources use Beestat-owned fallback devices without deprecated
`via_device` identifiers.

Registry listeners rebuild cached mappings, refresh Repairs and rebind this
entry's entities after renames, moves, detachments, removal or restoration.
They perform no Beestat I/O and do not recreate the entry. Stable resource
identity covers disabled/advanced entities too. Unload removes the listeners.
Automatic cleanup only removes stale devices solely owned by this entry, with
only Beestat identifiers and no foreign connections. Mixed/shared devices stay.

## Acquisition and local projection

### Four distinct clocks

| Clock | Meaning |
| --- | --- |
| Acquisition | Successful Beestat contact or metadata synchronization |
| Observation | The timestamp/day represented by source data |
| Projection | Local evaluation time for cached schedules, dates and calculations |
| Effect deadline | A retry/operation that can contact Beestat or persist state |

Setup and each import interval synchronize runtime, thermostat and sensor
metadata, then read required rows. Entities share one coordinator snapshot.
Each sensor/window is acquired once regardless of the number of derived
statistics. `skip_sync` skips the sync calls; it still reads Beestat.

The six-hour default owns cloud acquisition. Between acquisitions, one
entry-owned scheduler reevaluates cached schedules, cloud-stale thresholds,
local summary dates and date-dependent filter calculations at their earliest
boundary. It uses no cloud I/O, publishes actual projection changes and
reschedules after each boundary or refresh.

The coordinator owns the timezone. Each refresh and prepared Recorder import
captures one evaluation time, timezone and revision. A timezone/date rollover
invalidating an awaited summary window retries it. A timezone change
invalidating prepared statistics discards them before Recorder writes and
retries with new bounds. Repeated churn is bounded. Timezone changes rebuild
local projections without entry reload; unload cancels deadlines and listeners.

Cloud acquisitions serialize per entry. Refreshes, alert dismissals and imports
run in entry-owned tasks; unload cancels active and queued work before stale
results publish/write. Filter-boundary reconciliation has its own timer because
it can read Beestat and persist options; it checks the captured timezone and
replacement after awaited work.

### Freshness and profile context

The cloud-stale threshold is the larger of 120 minutes or one poll interval
plus 60 minutes of publication grace. Entity state and its local deadline share
that calculation. Runtime-summary stale is a separate daily diagnostic: the
latest summary is more than one local day behind.

**Current comfort profile** mirrors cached `program.currentClimateRef`. It is
delayed context, not live hold/mode state. Scheduled profile and next transition
project the cached program; they do not reconstruct active holds.

**Beestat-reported sensor in use** reflects upstream `in_use`, not configured
profile membership or Follow Me's momentary weighting. Missing/invalid values
remain available but unknown; an absent source row is unavailable. The aggregate
counts only present, non-inactive, non-deleted rows explicitly reporting true.

### Configured-profile temperature spread

Spread combines cached profile membership with mapped HA
temperatures. Participants resolve from capability-qualified Ecobee climate
sensor identifiers through Beestat identity to one mapping in the owning
thermostat. Display names and identifiers reused by another thermostat cannot
establish membership.

Local temperature changes rebuild the spread immediately without cloud I/O.
Unknown, unavailable, nonnumeric, missing-unit, unconvertible or ambiguous
sources are rejected. Observation age alone does not reject a valid HA reading.
The native unit is a temperature delta.

`profile_name` and `profile_ref` describe the membership used; `metadata_synced_at`
remains the successful cloud acquisition time during local updates. A partial
spread with at least two valid readings reports their range, valid/configured
counts and unavailable names. It can understate the full profile range. Unknown
membership leaves the entity unavailable, and HA omits those attributes while
unavailable. Legacy participating-sensor attributes remain for compatibility.

## Recorder statistics

The integration writes external statistics under source `beestat`. Stable slugs
and disjoint suffixes identify series; exclusions and normal imports preserve
unique IDs, units, metadata and existing history.

| Source | Imported statistic |
| --- | --- |
| Cooling, heating and fan summary runtime | Cumulative hours |
| Stage/accessory runtime | Cumulative hours after a non-zero source observation |
| Heating/cooling degree days | Cumulative source weather-load context |
| Room temperature | Daily mean/min/max |
| Room occupancy with a mapped local occupancy capability | Daily percentage mean/min/max |
| Thermostat heat/cool setpoints | Daily mean/min/max |
| Summary indoor humidity, outdoor temperature and outdoor humidity | Daily means; outdoor temperature also has min/max |
| Supported sensor air quality, CO2 and TVOC | Daily mean/min/max |

Recorder temperature metadata supports frontend unit conversion. Static
configuration values do not claim measurement statistics. Imported weather
context and runtime are not electrical consumption or proof of savings.

### Windows, corrections and numerical integrity

Routine cumulative imports use a seven-day summary overlap seeded from a
trustworthy prior Recorder cumulative row. First imports, missing seeds,
rebuilds and fallback repairs use the full baseline. Point reads are bounded
into source windows and aggregated by local day. Runtime refreshes also cover
the effective filter date as needed.

`rebuild_statistics` recalculates cumulative values before filtering writes.
`start_date` applies to every series; cumulative `has_sum` series rewrite the
affected tail through the latest source day. `end_date` clips measurement
series only. Corrections must not leave later cumulative rows or subsequent
seeds at an old offset. Optional thermostat scoping preserves source-selection
rules.

Summary normalization produces one row per thermostat/local date. The last
repeated identity wins; a winning deletion omits it. Runtime points use source
row ID, or resource ID plus timestamp fallback, before aggregation. Metadata
likewise gives the last row ownership of each Beestat ID; deletion removes it
and a later active row restores it. Ecobee settings tombstones follow this rule.

Only finite, representable values are accepted. Derived means, cumulative sums
and seed offsets must also remain representable. Negative/overflowing runtime
or degree-day contributions end that series before invalid writes. Points with
unrepresentable timezone conversions are omitted. Finite IAQ observations,
including spikes, remain unchanged; missing, unsupported, nonnumeric or
non-finite values are not clamped or invented.

Partial imports retain successful windows and report skipped-window evidence.
Removing the integration stops imports and removes its entities; existing
external statistics may remain in Recorder.

## Filter tracking

Filter tracking calculates Beestat fan exposure from a local baseline. The user
chooses runtime, calendar and notice limits. Consumers own inspections,
reminders, task identity, complete history and completion decisions. Use of the
tracking feature is optional; source gaps cannot become complete exposure merely
because a maintenance workflow needs a number.

### Replacement inputs and mutation order

The effective date prefers the local HA override, then a configured legacy
`input_datetime` helper, then Beestat/Ecobee metadata. Legacy helpers are date-only
compatibility inputs; their listener follows current mappings and triggers an
import when changed.

Date, button and timestamped actions share `entry_options.py`. YAML corrections
create their receipt in `config_payload.py`:

| Input | Meaning |
| --- | --- |
| **Mark filter changed** | Replacement at the click's exact UTC timestamp and local date |
| `record_filter_change` | Caller-verified replacement timestamp with prior-boundary and replay guards |
| **Filter changed date** | Date correction; clears exact timestamp and change-day baseline |
| `repair_filter_change_boundary` | Verified recent timestamp added to an existing matching saved HA override date |
| Changed explicit YAML boundary | Configuration correction |

Timestamped replacement persists date, timestamp and latest receipt before
fallible cloud work. Guard comparison, complete option merge and persistence
do not yield. Cached projections are rebuilt before cloud refresh; a cloud
failure leaves replacement saved and boundary work pending. Repeated button
presses are new mutations, not retries of an earlier click.

A date edit clears precision and performs a skip-sync refresh. On refresh
failure it can restore previous options only if no newer options update has
intervened. Successful date edits, button presses and timestamped replacements
attempt best-effort Beestat filter-alert acknowledgement. Repair does not
dismiss alerts. These paths never write Ecobee settings or Beestat's sync-owned
filter metadata.

`filter_change_event` holds schema version, action/source, request ID, prior
request ID, prior/new dates and timestamps, and recording time. `button` and
`service` identify replacement; `date`, `repair` and `configuration` identify
correction. This is one bounded latest receipt, not an event history, event-bus
API or independent proof of physical work.

### Timestamped action and replay

`beestat_statistics.record_filter_change` requires seven fields:

| Field | Contract |
| --- | --- |
| `config_entry_id` | Loaded Beestat Statistics entry |
| `thermostat_id` | Exact positive configured source ID |
| `changed_at` | Verified replacement, within the last 31 days and not future-dated for a new recording |
| `expected_changed_at` | Saved `filter_changed_at`, or null |
| `expected_changed_date` | Saved `home_assistant_override_date`, or null; not merely effective date |
| `expected_request_id` | Saved `filter_change_event.request_id`, or null |
| `request_id` | Stable opaque ID of 1–128 characters for this replacement |

Capture all three expected fields with the cycle and preserve them on retries.
Prior event identity prevents stale completion after a boundary changes and
changes back. A new replacement must be later than the saved exact timestamp
and cannot predate the saved local date. Changed expected fields or reuse of
the current ID with different input reject the mutation.

The current request with the same timestamp/source returns `already_recorded`
without repeated effects, even when its timestamp has aged beyond 31 days.
That limit applies to new recordings. After a later mutation, older requests
are no longer retained as replay receipts.

The schema-version-1 response includes entry, thermostat, request,
`requested_changed_at`, saved `changed_at`, saved `changed_date` and
`boundary_status`. `recorded` or `already_recorded` with the exact requested
timestamp confirms persistence; `pending_data` does not undo it. `superseded`
means a later mutation won and is not successful completion of this request.
Response boundary status describes saved reconciliation fields; the forecast
carries richer current source quality.

Repair requires the entry, thermostat and `changed_at`; the timestamp must be
within the last 31 days and match the saved HA override's local date. It
refreshes/synchronizes upstream and can remain pending after saving. Both
timestamped actions resolve unambiguous offsetless times in HA's timezone;
repeated daylight-saving times require explicit offsets and nonexistent local
times fail validation.

### Runtime coverage and boundary reconciliation

A timestamped change day counts only valid five-minute intervals wholly after
replacement. The straddling interval is omitted without proration; valid later
intervals still count when replacement occurred inside a source gap. Date-only
input omits the ambiguous change day. Later days use normalized fan totals and
coverage counts, including absent days. Expected counts use UTC elapsed local-day
duration across daylight-saving transitions.

`complete`, `partial` and `unknown` describe coverage through the reported
horizon. Freshness, gaps and boundary uncertainty are separate. Unknown interval
duration bounds possible unobserved exposure; it is never synthetic fan runtime.
Weather-only rows cannot reconstruct missing equipment runtime.

One bounded raw change-day cache per thermostat is invalidated by replacement,
timezone or normalized summary-fingerprint changes, even after finalization.
Six-hour expiry on ordinary refreshes catches point corrections with unchanged
daily totals/counts. Recomputed prefix baselines prevent corrected earlier
history from being charged to the new filter.

`finalized` requires a validated prefix baseline. `source_gap` can coexist with
complete post-replacement coverage. Pending work retries every 15 minutes while
the replacement timestamp is at most six hours old, then on ordinary refreshes.
A delayed recording or repair older than six hours gets its immediate attempt
and then ordinary refreshes. Awaited reads recheck persisted
replacement, current options and timezone revision so stale work cannot
overwrite later mutations. Pending status never reverts the exact timestamp.

### Forecast snapshot and revision

**Filter due date** publishes one coherent forecast from one coordinator model.
Consumers read that snapshot instead of joining sequential sibling updates. It
contains the baseline/source, limits, observed/remaining hours, recent rate,
intermediate dates, due state and these quality fields:

| Fields | Interpretation |
| --- | --- |
| `runtime_observed_hours`, `runtime_is_lower_bound` | Observed runtime and whether the displayed value is a lower bound |
| `runtime_coverage`, `runtime_source_data_end` | Coverage through the reported horizon, separate from freshness |
| `runtime_unknown_interval_minutes` | Unobserved elapsed time including source gaps, unreported tail and boundary uncertainty |
| `runtime_boundary_precision_minutes`, `runtime_boundary_uncertainty_minutes`, `boundary_status` | Five-minute precision and change-day assessment |
| `runtime_threshold_reached` | True, false or unknown from exposure bounds |
| `runtime_forecast_basis`, `remaining_runtime_hours_is_upper_bound`, `runtime_due_date_is_projection` | Runtime-forecast qualification |
| `recent_runtime_window_start`, `recent_runtime_window_end`, `recent_runtime_complete_days`, `recent_runtime_excluded_days` | Rate-window provenance and denominator |

Observed hours round down to 0.1 hour and remaining hours round up to 0.1 hour.
Threshold decisions use underlying seconds: observations can prove a limit
reached; observed exposure plus bounded uncertainty can prove it not reached;
otherwise it is unknown. The due problem sensor reports proven runtime or
calendar limits. Due-soon uses the forecast notice window. Calendar age remains
enforceable independently of missing runtime.

Recent rate averages only complete past local days in its 30-day window.
Today, missing days, incomplete counts and invalid runtime are excluded. Days
before replacement can qualify. No qualifying days leaves the runtime projection
unavailable while calendar age can still supply a date. Before observed exposure
reaches the threshold, the runtime date is a projection; afterward it is pinned
to the first qualifying daily-summary date rather than moving each day. The
final due date is the earlier available runtime or calendar date.

`forecast_revision` identifies forecast semantics and source quality. A source-gap
correction can revise it even without a changed date or runtime counter.
Internally, uncertainty through the source horizon is retained separately from
the elapsed unreported tail. Revision includes that stable source uncertainty
and excludes only total `runtime_unknown_interval_minutes` telemetry. Elapsed
time alone does not revise the forecast unless it changes lower-bound/threshold
decisions, dates or another semantic input. Local temperature reevaluations
therefore need not produce new consumer work.

Exact timestamps, source/helper details, boundary fields, latest receipts and
forecast attributes are available in current state but excluded from Recorder
history. There is no second datetime entity or persisted forecast history.

## Entities and actions

| Surface | Contents |
| --- | --- |
| Beestat service device | Status, sync/import success, summary/import counters, partial-import and mapping problems, refresh/import buttons |
| Thermostat primary surface | Scheduled/next profiles, alerts, filter date/button, final due date, days remaining and due/due-soon states |
| Thermostat diagnostics | Cloud-reported current profile, freshness/windows/lags, reported active-sensor count, runtime detail and intermediate dates |
| Profile measurement | Configured-profile room temperature spread with unrecorded membership/coverage attributes |
| Room diagnostics | Beestat-reported `in_use` |
| Advanced optional entities | Cached protection/staging, corrections, Auto Away, Follow Me, Smart Circulation, preheat/precool, alert/reminder policies, microphone enabled and playback volume |

Advanced import counters and settings are disabled by default. Static numeric
settings retain their units without claiming present-time measurements. Binary
settings say enabled/disabled rather than implying active alerts or microphone
use. Migrations enabling older integration-disabled freshness diagnostics
preserve user-disabled entities.

Alert counts/categories mirror active Beestat/Ecobee metadata. Equipment-looking
and unknown alerts also appear through the equipment problem sensor; maintenance
reminders alone do not imply failed equipment. State retains complete counts
and categories with at most three bounded examples.

| Action | Acquisition and persistence |
| --- | --- |
| **Refresh Runtime** | Refresh native state without Recorder imports |
| **Import Statistics** / `import_statistics` | Sync/import; optional `point_lookback_days` and `skip_sync` |
| `rebuild_statistics` | Full-baseline repair; optional thermostat/date scope and `skip_sync` |
| `get_configuration` | Response-only cached read; no cloud call or mutation |
| Filter date/button and timestamp actions | Shared baseline/mutation contracts above |

Normal operation requires no automation. The optional
[stale-runtime blueprint](../blueprints/automation/beestat_statistics/stale_runtime_notification.yaml)
instantiates an HA automation with user-selected sensor, threshold, duration and
notification actions. The integration itself sends no notifications and
registers no custom device triggers or conditions.

## Privacy and diagnostics

Validate the HTTPS API URL before constructing a client. Credential-bearing
requests never follow redirects. Invalid legacy URLs fail setup, raise a Repair
and remain uncontacted until corrected. Requests have bounded timeout/retries
and response sizes; limits apply to declared length and streamed bytes.
Authentication failures start native reauth; other errors retain bounded
operation, HTTP status and category context.

Remote response bodies and arbitrary error payloads must not enter exceptions,
logs, entity attributes, diagnostics or chained exceptions. Credentials and
account fingerprints do not belong on those surfaces. Public fixtures and docs
use synthetic examples; household configuration and exports stay outside source.

After thermostat sync, `ecobee_thermostat.read_id` rows are immediately reduced
to explicit settings/audio/source-detail allowlists. Account, location, billing,
utility, management, device identifiers, notification recipients and access
codes are not retained. Unknown fields cannot silently enter responses.

| Support surface | Contract |
| --- | --- |
| Downloadable diagnostics | Shareable aggregate status, timing, counts, import windows/fallbacks, bounded skipped-window examples, alert-dismissal results and thermostat quality. Redacts credentials/URLs, names/slugs, Beestat/HA IDs, exact filter details, request IDs and profile names/timing. Saved data/options become an allowlisted ownership/count summary. |
| `get_configuration` response | Private audit of saved/effective mappings, timing, filter state, model/firmware, equipment/stages, basic property characteristics, profiles and allowlisted cached Ecobee settings. Includes local names/source/entity IDs; do not attach it to public issues. |

Neither returns raw history or the original Ecobee payload. The private action
excludes credentials/API URL and labels or normalizes unit-bearing scalars.
Unrecorded current-state attributes support local consumers; they do not replace
redacted downloadable diagnostics.

## Development and validation

### Repository layout and support lanes

This HACS product has exactly one runtime directory,
`custom_components/beestat_statistics/`. All runtime files belong there. Root
`hacs.json`, `.github/`, requirements, tests, scripts, docs and blueprints provide
distribution/development support. `.venv/`, `.local/`, caches, databases, raw
diagnostics and backups are local material, not distributable source.

The client remains in-tree for this HACS integration. A future Home Assistant
Core submission would first require an async, tagged, open-source client package
published separately.

| Lane owner | Supported Core | Harness |
| --- | --- | --- |
| `hacs.json` and `requirements-ha-test.txt` | Minimum `2026.8.0` | `pytest-homeassistant-custom-component==0.13.354` |
| `requirements-ha-current.txt` | Current target `2026.9.1` | `pytest-homeassistant-custom-component==0.13.364` |

The HA harness requires Linux and Python `3.14.2` or newer; hosted validation
uses Python `3.14`. Both lanes install Core separately after its matching harness,
run `python -m pip check` after final installation, and execute the complete test
tree. Strict mypy runs in the minimum lane. Conflicts, failed collection or a
skipped required harness do not prove support. Native Windows Python cannot
substitute for the Linux Home Assistant harness. Advance requirements, labels,
docs and assertions together; advance HACS/blueprint minima when the distribution
floor changes.

### Maintained commands

Run all local lanes on Windows with Ubuntu 24.04 WSL2 and rootless Podman:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-release-local.ps1
```

On Linux:

```bash
bash scripts/verify-release-local.sh all
```

Modes are `all`, `unit`, `minimum`, `current` and `release` (`-Mode` in PowerShell).
The `release` lane runs Hassfest; it does not publish. The default container
backend requires Podman. Hosted CI supplies `native` as the second shell
argument, uses disposable Python environments and requires Go for actionlint
and Docker for Hassfest. Scripts resolve the repository from their location.

The container runner snapshots tracked and nonignored new files, including
uncommitted changes. Digest-pinned images validate this read-only snapshot;
each Python lane has a fresh environment. `all container` runs four independent
lanes concurrently, retains each result after failure and waits for all workers
before cleanup. Individual lanes and native execution remain sequential.

The Podman volume `beestat-statistics-validation-pip` caches downloads/wheels,
not environments or results. Remove it only when no local validation runs to
force fresh downloads:

```bash
podman volume rm beestat-statistics-validation-pip
```

Container pip installs defer dependency bytecode until import; mypy writes no
cache. Product compilation, installs and every test still run. The runner owns
exact Ruff, mypy, actionlint, ShellCheck and `zizmor` pins to keep local/CI checks
aligned.

Dependency-light work without HA or containers:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install tzdata
.\.venv\Scripts\python.exe scripts\run_dependency_light_tests.py
```

`tzdata` supplies Windows IANA timezone data. The selector uses direct-import
AST discovery to omit HA-dependent modules; both HA lanes still execute all
of `tests/`. HA modules import the real harness unconditionally. Missing HA
modules, empty tests, unsupported free-function/nested layouts and all-skipped
dependency-light runs fail closed.

The unit lane covers compilation, JSON, whitespace, formatting/lint, workflow
and public-safety checks. The public guard reads current tracked/nonignored new
file contents and names, rejecting links, unreadable/oversized files and
unreviewed binaries. It is not a Git-history audit.
[quality_scale.yaml](../custom_components/beestat_statistics/quality_scale.yaml)
records claimed HA quality rules/evidence; omitted rules remain unclaimed.

Check upstream API drift separately:

```powershell
.\.venv\Scripts\python.exe scripts\check_beestat_api_surface.py
```

The checker resolves one immutable upstream commit, verifies Git blobs and
rejects incomplete inventories. Review differences before `--update`; the
snapshot is replaced atomically only after every read/check succeeds. Updating
fingerprints does not authorize broader API use.

### Release publishing

Third-party actions use full commit SHAs. Dependabot proposes weekly Actions
updates after a seven-day stability/supply-chain cooldown. **Release gate**
requires unit, both dependency-closed HA lanes, Hassfest and HACS. HACS validates
the pushed repository and is not part of local containers.

1. Prepare a candidate from current `main`, aligning manifest version and notes.
2. Require candidate PR success for **Unit tests**, **Home Assistant minimum
   integration tests (Core 2026.8.0)**, **Home Assistant current integration tests
   (Core 2026.9.1)**, **Hassfest**, **HACS**, **Release gate**, and CodeQL's
   **Analyze (actions)**, **Analyze (python)** and **CodeQL** checks.
3. Merge through protection without bypass, using squash/rebase for linear history.
4. Require **Validate** push success and CodeQL analysis on the exact resulting
   `main`. Inspect full logs and open code-scanning alerts; analysis success does
   not mean it found nothing.
5. Resolve or explicitly disposition candidate-introduced alerts. Publish the
   immutable `vYYYY.M.D` tag (or same-day patch suffix) and GitHub Release against
   that validated commit, matching the manifest.
6. Treat HACS install, configuration check, restart, live validation, migration
   and rollback as subsequent operational stages. Publication does not prove
   that an HA instance loaded the change.

Before HACS publication, verify repository description, relevant Home
Assistant/HACS topics, enabled issues, brand icon and required validation.
For GitHub CLI, put final prose in a Markdown file and supply `--notes-file`
and an explicit validated target when creating the release. PowerShell literal
`\n` text is not a newline. Immutable releases are not editable preparation
surfaces.

### Evidence for changed behavior

Pure normalization, coverage, forecast and identity helpers have dependency-light
tests. Native HA tests cover lifecycle, flows, registry mapping, clocks,
Recorder and actions. `tests/test_entity_runtime_ha.py` exercises real publication,
including local temperature changes that update uncertainty without revising
forecast semantics. Tests present in source do not prove a support lane ran;
report executed checks and versions. Live adoption also needs loaded-version,
import, health and affected-consumer readback.

## Code ownership

| Module or surface | Responsibility |
| --- | --- |
| `__init__.py` | Lifecycle, YAML, actions, Recorder orchestration/seeding, Repairs, registry lifecycle and helper listeners |
| `coordinator.py` | Acquisition, cached models, quality, local clocks, profile/spread and filter-boundary reconciliation |
| `api.py`, `url_validation.py` | Async transport, URL/request safety, response normalization and bounded errors |
| `config_flow.py`, `config_payload.py`, `config_rows.py`, `config_model.py`, `issues.py` | Flows, overrides, identity, validation, normalized configuration and Repairs |
| `source_identity.py`, `entity_reference.py`, `entity.py` | Physical-source proof, stable references and device attachment |
| `entry_options.py`, `filter_action.py` | Filter mutation, optimistic guards and latest receipt |
| `filter_runtime.py`, `filter_forecast.py` | Pure exposure/coverage/rate and coherent forecast/revision |
| `profile.py`, `thermostat_settings.py`, `configuration.py`, `alerts.py` | Profiles, cached-setting privacy, private response and alert categories |
| `statistics_builder.py` | Recorder conversion and cumulative integrity |
| `sensor.py`, `binary_sensor.py`, `button.py`, `date.py` | Entity publication and user actions |
| `diagnostics.py`, `import_evidence.py` | Redacted aggregate evidence and bounded skipped-window details |
| `runtime.py`, `task_coalescer.py` | Typed entry runtime and bounded background-work scheduling |
| `const.py` | Shared configuration keys, defaults, units and statistic metadata |
| `translations/en.json`, `icons.json`, `services.yaml`, `quality_scale.yaml` | User-facing schema, semantics and quality declarations |

Behavior changes update direct consumers, tests and documentation. Custom
integrations ship complete translations directly; the Core-only `strings.json`
build input is not used. Keep household instructions, credentials, snapshots
and deployment-specific values outside this public product.

# Beestat Statistics

Use your Beestat history in Home Assistant: chart HVAC statistics, see
cloud-reported schedules and alerts beside your existing devices, and estimate
filter use from recorded fan runtime.

Your existing HomeKit/Ecobee integration continues to supply live thermostat
control. Beestat Statistics supplies historical and delayed cloud information,
plus calculations made locally from that information. It does not measure
filter condition or choose a maintenance policy for your equipment.

## Get connected

You need Home Assistant **2026.8.0 or later**, Recorder, and a Beestat API key
for an account that exposes at least one identifiable thermostat. Matching local
HomeKit/Ecobee devices are useful but are not required for history imports.

1. [Open Beestat Statistics in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=ItsColby&repository=beestat-statistics&category=integration),
   or add `https://github.com/ItsColby/beestat-statistics` as a custom repository
   of type **Integration**.
2. Download the integration and restart Home Assistant.
3. Under **Settings > Devices & services**, add **Beestat Statistics** and enter
   the API key. Keep the default API URL unless you need another supported
   HTTPS endpoint.
4. Open the integration options. Use **Choose Beestat sources** to control
   inclusion, then review **Confirm automatic mappings** or map sources
   individually to their local devices.

One entry manages the account. You can change credentials later through
**Reconfigure**. See the [user guide](docs/usage.md) for account changes,
YAML configuration, mapping conflicts and removal.

## Check the result

The Beestat service device shows acquisition/import status and provides
**Refresh runtime** and **Import statistics** buttons. Mapped thermostats and
room sensors gain Beestat context on their existing devices. Sources without a
local match use Beestat fallback devices.

Allow the initial import to finish, then check source freshness, mapping Repairs
and partial-import status. A completed import can still contain skipped windows.
By default, imported Recorder statistics are daily aggregates and cumulative
totals. [Opt-in hourly history](docs/hourly-history-v3.md) uses eligible raw
observations for explicitly adopted quantities; installing the integration or
selecting room measurements does not adopt hourly history. Neither path copies
every five-minute source sample into Recorder.

Scheduled acquisition defaults to six hours. Cached schedules, calendar dates
and local room-temperature calculations can change between polls without a new
Beestat observation. More frequent polling cannot recover data absent upstream.

## Put the information to use

| You want to… | Start with… |
| --- | --- |
| Chart historical heating, cooling or fan use | Recorder statistics under source `beestat` |
| Understand thermostat context | Comfort schedules, cloud freshness, alerts and reported sensor-use entities |
| Compare rooms in a configured comfort profile | The profile temperature-spread sensor and its coverage attributes |
| Track a filter | Its changed date, locally configured limits, due-date forecast and runtime coverage |
| Build an automation | The [user guide](docs/usage.md) and [action schemas](custom_components/beestat_statistics/services.yaml) |

For filter tracking, choose your own runtime and calendar limits. The defaults
of **250 fan-runtime hours, 90 days and 7 days' notice** are software starting
values, not manufacturer recommendations. Press **Mark filter changed** when
replacing a filter; use the documented timestamp action when recording a past
replacement. A date correction and a new replacement have different effects.

Missing runtime stays unknown. A projected due date can exist while **Filter
due** remains unknown; neither proves that a filter is physically dirty. Your
Home Assistant workflows own inspections, reminders, physical-work evidence and
complete maintenance history. The integration keeps only its current baseline
and latest mutation receipt.

## Find the next step

- [User guide](docs/usage.md): settings, entity interpretation, filter actions,
  history repair, troubleshooting and removal.
- [Architecture](docs/architecture.md): ownership and the contracts that protect
  source identity, history, runtime estimates and action replay.
- [Development](docs/development.md): reproducible checks, source inventory and
  release preparation.
- [Release notes](RELEASE_NOTES.md): changes associated with each released version.

For a problem, start with the integration's **Status** sensor and Home Assistant
Repairs. Download diagnostics from the integration entry and inspect them before
sharing. The `get_configuration` action returns private configuration details;
it is not a substitute for redacted diagnostics in a public issue.

Report reproducible defects in the [issue tracker](https://github.com/ItsColby/beestat-statistics/issues).

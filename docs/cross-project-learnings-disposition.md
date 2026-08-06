# Cross-project Learnings Disposition

This document records how the aggregated Free Library Events and Beestat
Statistics contracts used by Ecobee Unified apply back to Beestat Statistics.
It is a disposition, not a dependency on either sibling project. Beestat keeps
its own product boundary: cloud history, runtime/profile/alert/filter context,
forecasting, and Recorder import. HomeKit/Ecobee entities remain the owners of
live thermostat and room state and HVAC control.

Upstream contracts were refreshed against Home Assistant Core 2026.8 guidance:

- [config flows, reconfigure, reauthentication, and migrations](https://developers.home-assistant.io/docs/core/integration/config_flow/);
- [helper entities linking to source devices](https://developers.home-assistant.io/blog/2025/07/18/updated-pattern-for-helpers-linking-to-devices/);
- [single-config-entry device ownership in Core 2026.8](https://developers.home-assistant.io/blog/2026/07/21/device-registry-single-config-entry/);
- [Recorder statistics metadata changes](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes/);
- [Repairs lifecycle](https://developers.home-assistant.io/docs/core/platform/repairs/); and
- [current integration quality rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/).

## Configuration and Lifecycle

| Contract | Disposition | Current evidence and consequence |
|---|---|---|
| Typed config-entry runtime | Already satisfied | `runtime.py` defines a typed `ConfigEntry` alias and a slotted runtime dataclass containing the client, coordinator, importer, and interval. Entry-owned state is attached only through `runtime_data`. |
| Minimum stable identity in data; behavior in options | Already satisfied | Connection identity stays in config-entry data. Timing, source scope, mappings, filters, and statistic capability overrides stay in options. `config_payload.py` preserves unknown saved override rows. |
| Native setup, reconfigure, reauth, and options flows | Already satisfied | `config_flow.py` validates connection replacements before saving, reloads through native helpers, and confirms account changes or removal of active source scope. |
| Sensitive identity changes fail closed | Already satisfied | The non-reversible account fingerprint detects a different Beestat account. Confirmation clears numeric source mappings while preserving timing options and warns that already-imported Recorder history remains. |
| Versioned migrations and rollback continuity | Already satisfied | `async_migrate_entry` and `migrate_entry_payload` preserve legacy connection, timing, scope, mapping, stable slug, filter, and statistic fields. No new persisted field was needed for this review, so no version bump is justified. |
| Entity-registry rename tracking | Deliberately not applicable | Beestat's stable source identity is the Beestat numeric resource ID, not a Home Assistant entity ID. Optional local entity overrides remain explicit user policy; a rename/removal raises an actionable mapping Repair instead of silently rewriting YAML or options ownership. |

## Source and Runtime Model

| Contract | Disposition | Current evidence and consequence |
|---|---|---|
| Explicit supported source boundary | Already satisfied | `docs/beestat-api-surface.json` and its checker own the exact Beestat resources/methods. Direct Ecobee API access, source-integration runtime objects, diagnostics scraping, and `.storage` access remain prohibited. |
| Normalize once, project many | Already satisfied | The coordinator builds one treated-as-immutable `BeestatRuntimeData` snapshot containing normalized config, runtime summaries, thermostat metadata, and sensor metadata. Entities and diagnostics project that shared snapshot; Recorder projection is centralized in `statistics_builder.py`. |
| Capability-aware partial success | Already satisfied | Optional mappings and point-statistic capabilities are independent. One failed one-day point window does not discard healthy sources or cumulative summary series; authentication and complete runtime refresh failures still fail the owning operation. |
| Bounded partial-failure evidence | Implemented | `SkippedWindowEvidence` counts every skipped thermostat/sensor window while retaining at most three identifier-free resource/time examples. Status, the partial-import problem entity, and downloadable diagnostics expose those examples without retaining an unbounded failure list. |
| Bounded work | Already satisfied | Point reads use at most 30-day windows and recursively narrow failed windows to one day; API retries/timeouts, lookback, scan interval, summary overlap, and fast filter-boundary retries are bounded. Normal coordinator refreshes continue after the six-hour fast-retry window. A Library-style RSS response cap is not transferable because Beestat full-summary baselines are an intentional Recorder recovery source. |
| Stale-result revision guards | Already satisfied | Filter boundary reconciliation re-reads the effective saved click timestamp and current reconciliation state after the awaited raw-runtime request, then writes only if the same revision remains pending. Repeated presses and concurrent option changes cannot be overwritten by an older request. |

## Devices and Entities

| Contract | Disposition | Current evidence and consequence |
|---|---|---|
| Link without foreign ownership | Already satisfied | Mapped entities assign the source `DeviceEntry` directly and return no foreign identifiers/connections/device info. Supported helper cleanup removes legacy Beestat ownership. Fallback devices remain Beestat-owned. |
| Fail closed for mixed device records | Already satisfied | Automated and manual cleanup requires one Beestat config-entry owner, at least one Beestat identifier, no foreign identifier, and no connection. Mixed/shared records are not removed. |
| Stable identity and dynamic discovery | Already satisfied | Entity unique IDs and Recorder statistic IDs use stable source IDs/slugs. New enabled Beestat resources are added without duplicate unique IDs; explicit exclusions and unknown saved overrides survive discovery drift. Scope changes do not rewrite existing Recorder history. |
| Compact state; rich on-demand diagnostics | Implemented | High-volume attributes are excluded from Recorder. Ordinary alert surfaces retain the full alert count/category but expose at most three examples containing only bounded code/type/severity/time fields and a derived category; arbitrary remote text and source identifiers are omitted. Skipped-window evidence is similarly capped at three; detailed aggregate runtime/config evidence remains in redacted diagnostics or the explicitly private response-only configuration action. |
| Duplicate live climate/room entities | Deliberately not applicable | Beestat does not create a climate entity or duplicate live temperature, motion, occupancy, or control. It enriches existing source devices with Beestat-owned history/status semantics only. |

## Actions, Side Effects, and Recovery

| Contract | Disposition | Current evidence and consequence |
|---|---|---|
| One action, one owner | Already satisfied | Refresh/import/rebuild actions invoke only Beestat/Recorder owners. `get_configuration` is response-only. Filter alert dismissal is a narrow best-effort Beestat acknowledgement after the local physical-change record; there is no Ecobee write or second-backend retry. |
| Validate before side effects | Already satisfied | Actions resolve the loaded entry, configured thermostat, date range, and timestamp bounds before import/rebuild/repair work. HA-visible failures use translated safe exception categories and drop raw exception chains. |
| Persist local intent before fallible cloud work | Already satisfied | The native filter button stores the local date and exact UTC click timestamp before refresh or alert dismissal. Cloud failure cannot roll back the physical replacement record. |
| Actionable Repairs and cleanup | Implemented | Missing/wrong-domain enabled override references remain the only mapping Repairs. A scoped entity-registry listener now refreshes them immediately when a referenced entity is removed, renamed, or restored; unload removes the listener. Disabled sources remain excluded from the check. |
| Climate command confirmation and writer policy | Deliberately not applicable | Beestat owns no live climate command. Ecobee Unified's command tokens, confirmation window, writer selection, and no-fallback rules must not be copied here. |

## Privacy, Validation, and Release

| Contract | Disposition | Current evidence and consequence |
|---|---|---|
| Generic public payload and private runtime boundary | Already satisfied | Fixtures use generic zones/sensors. The static guard scans tracked/public-relevant text and binaries while ignoring generated environments. Private exact-value publication scanning remains outside this public repository and CI. |
| Redacted diagnostics and bounded exceptions | Implemented | Diagnostics use an allow-listed saved-config ownership/count summary and redact credentials, URLs, account/source IDs, local entity IDs, device IDs, user-assigned names/slugs, filter dates/timestamps, and comfort-profile names/timing while preserving aggregate health evidence. Unknown future saved fields therefore fail closed. Remote response bodies and arbitrary exception text do not cross HA-visible boundaries; unexpected failures use bounded private-safe fingerprints. |
| Recorder continuity | Already satisfied | Metadata includes current `mean_type` and `unit_class`. Cumulative runtime/degree-day imports seed from the previous Recorder row for bounded overlap, fall back to a full baseline when continuity cannot be proven, and retain stable statistic IDs/metadata and existing history. |
| Current/minimum HA and repository validation | Already satisfied | CI covers Python 3.14, minimum Core 2026.7.1, current Core 2026.8.0, strict mypy, Ruff, unit/HA tests, compile, JSON, privacy, API surface, Hassfest, HACS, actionlint/ShellCheck, zizmor, dependency updates, and a terminal release gate. |
| Exact-candidate security/release inspection | Deferred to release gate | CodeQL completion/open-alert disposition, live GitHub settings, private exact-value scanning, push/PR/tag/release, HACS update, HA check/restart, migration, and live validation require the separately authorized publication/deployment workflow. A local commit cannot prove those states. |
| Library feed/email/WebCal machinery | Deliberately not applicable | Feed expansion, calendar/WebCal, digest rendering, email delivery, image downloads, capability URLs, and their cleanup have no Beestat requirement or owner. |
| Ecobee Unified climate/control machinery | Deliberately not applicable | Per-field live fallback, command routing/confirmation, vendor operations, and consumer migration belong only to Ecobee Unified. Beestat remains an optional enrichment/history source for that project. |

## Ecobee Unified Consequence Cross-check

The consequence table is fully covered: typed shared runtime and projection,
native recoverable configuration, bounded cloud work, no live-control writer,
source-device linking without co-ownership, bounded/redacted diagnostics,
actionable Repairs, stable compact entities, and separately gated immutable
release validation. Event-only source subscriptions and command-confirmation
tokens are deliberately Ecobee Unified-specific and are not imported into this
cloud-polling statistics integration.

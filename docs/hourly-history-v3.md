# Qualified hourly history, contract 3

This additive contract separates logical history from Recorder storage. Calls
without `contract_version: 3` keep the existing coverage contract and v2 selection
behavior. A v3 capability declaration does not establish installation, admission
of any quantity, or completion of a historical repair. Installation, explicit
adoption, and verified history remain separate operational outcomes.

## Discover identities

Call `beestat_statistics.get_configuration` with `config_entry_id`.
`hourly_statistics.history_v3` contains `contract_version`, `method_version`,
`daily_policy`, `services`, `limits`, `identity`, `root_revision`, `source_revision`,
`coverage_revision`, `operation`, and `quantities`.

Each quantity descriptor contains:

| Field | Meaning |
|---|---|
| `quantity_id` | Physical quantity within the explicit entry/account scope, e.g. `thermostat:101:fan_runtime_hours` or `sensor:201:temperature` |
| `thermostat_id`, `sensor_id`, `quantity` | Explicit physical and semantic identity; thermostat-only quantities have null sensor ID |
| `kind`, `logical_unit` | `runtime` in `h`, `degree_days` in `°F·day`, or `measurement` in its authoritative unit |
| `method_version` | `five_minute_complete_hour_v3` |
| `statistic_id` | Saved native v3 destination; never construct this from a slug |
| `legacy_statistic_ids` | Exact retained aliases; resolve existing display/configuration selections through this mapping |
| `representation` | Native arithmetic mean, unit/unit class, logical multiplier and original v2 binding |
| `admission`, `writer_status` | Current eligibility/reservation state, independent of data availability |

Identity contains the config-entry ID, normalized API origin and existing hashed
thermostat account anchors. These are the producer's actual continuity evidence,
not a provider-issued account ID. Preserve and compare them with physical IDs.
Renames retain saved native ownership. An identity or method mismatch requires
explicit re-admission; a matching display name is insufficient.

Runtime stores a component-runtime rate in `%`; logical hourly amount is mean /
100. Aggregate heat can exceed 100% or one hour because compressor and auxiliary
components both contribute. Degree days store positive departures from 65°F in
`temperature_delta`, `°F`; logical amount is mean / 24, including DST days.
Measurement rows retain genuine sample mean/min/max. Rate rows have no invented
extrema. All v3 metadata uses arithmetic mean and `has_sum: false`. VOC remains
in the inventory with blocked admission until its unit contract is established.

## Read logical history

Call `beestat_statistics.get_hourly_coverage` with:

```yaml
config_entry_id: example_entry
contract_version: 3
quantity_ids:
  - thermostat:101:fan_runtime_hours
start: '2026-09-01T00:00:00+00:00'
end: '2026-09-02T00:00:00+00:00'
period: hour
daily_policy: complete_points_else_legacy_day
page_size: 4096
offset: 0
```

`start` is inclusive and `end` exclusive, both whole UTC hours. Select 1–40
distinct explicit quantity IDs and at most 366 elapsed days. `period` is `hour`
or `day`; day boundaries follow the response timezone and must not split stored
UTC hours. No prorating occurs. The response returns at most `page_size` buckets
across all quantities (1–4096), with no silent truncation.

The response contains `contract_version: 3`, `identity`, `method_version`,
`daily_policy`, `start`, `end`, `period`, `timezone`, `timezone_revision`,
`evaluated_at`, `config_revision`, `root_revision`, `source_revision`, `coverage_revision`, `view_token`,
`native_verification: bounded_readback`, `pagination`, `operation`, and `series`. Each series contains `descriptor`,
`buckets`, and `summary`. `pagination` contains `offset`, `next_offset`,
`has_more`, and `total_buckets`. For later pages repeat the original request plus
the returned `view_token`, `evaluated_at`, and `next_offset` as `offset`.
The producer rejects changed views rather than combining revisions. Page size
can change; quantity order, bounds, method, policy and evaluation cannot.

Each bucket contains `start`, `end`, nullable `value`, `min`, `max`, `reason`,
`failure_reason`, `valid_slots`, `expected_slots`, `verified_hours`, `expected_hours`, `closed_hours`,
nullable `observed_amount`, nullable `complete_total`, `source_basis`, `confidence`,
`source_ids`, `method_basis`, and `eligible_intervals` (arrays of exact inclusive-start /
exclusive-end UTC pairs). These intervals are the comparison/averaging basis;
equal counts do not establish equal observations. Verified hourly values require
both committed proof and matching bounded native readback. Native rows alone
are unverified. Missing, provisional, invalid, pending, conflict, unassessed and
unverified native evidence remain distinct and never become zeros.

Amount summaries expose observed amount, verified/expected/closed hours and a
nullable complete total. Measurement summaries expose observed mean/extrema and
their coverage. Zero is an observation only when all twelve valid samples yield
zero; no observations means null. Elapsed time does not establish provider
settlement or complete an import.

For `period: day`, `complete_points_else_legacy_day` chooses exactly one basis:
a complete qualified point day; otherwise a valid closed legacy day with its
actual predecessor and compatible historical calendar; otherwise the partial
point subtotal; otherwise null. It never adds legacy and points or fills missing
hours. Current/adoption partial legacy days and unproven calendars cannot be
complete fallbacks. Local days may contain 23 or 25 hours.

The query is read-only: no source fetch, refresh, adoption, reconciliation,
Recorder submission, journal save or marker update. Pending work suppresses its
affected intervals. Consumers may preserve an explicitly stale last committed
view, but cannot silently substitute guessed/native-only values.

### Exact response types and eligibility

The authoritative response structures are `HistoryIdentity`,
`HistoryRepresentation`, `QuantityDescriptor`, `HistoryOperation`, `HistoryBucket`,
`HistorySummary`, `HistoryPagination`, `HistorySeriesResponse`, and
`HistoryResponse` in [the public contract](../custom_components/beestat_statistics/hourly_history_contract.py).
`NotRequired` keys are absent when inapplicable; nullable keys are present with
JSON null. Timestamps are offset-qualified ISO strings; counters/revisions are
integers; numeric observations are finite numbers. No numeric string or boolean
is an observation. [Synthetic response examples](examples/hourly-history-v3.json)
are generated through the same logical query implementation.

`identity` contains exactly `entry_id: string`, `api_base: string` and
`account_anchors: string[]`. The representation contains exactly
`kind: arithmetic_mean`, `native_field: mean`, `unit_of_measurement: string`,
`unit_class: string|null`, `logical_multiplier: number`, and
`v2_statistic_id: string`. The latter records lineage, not automatic migration.

`admission` is `eligible` or `blocked`. Eligible means the method/unit/physical
contract can be selected; it says nothing about data completeness. Blocked
descriptors contain `blocked_reason`; VOC uses `voc_unit_unresolved`.
`writer_status` is `unselected`, `reserved`, or `adopted`. A reservation excludes
the legacy writer even while an operation is incomplete or blocked. Adopted
means a saved owner exists, not that every requested bucket is verified.

| `bucket.reason` | Value and eligibility |
|---|---|
| `ready` | Complete committed point evidence matches current native readback; value is usable and eligible intervals are explicit |
| `legacy_day` | One qualified complete closed legacy day was selected; usable only with its declared legacy method basis |
| `partial` | Day value is the observed point subtotal or observed point mean; complete total is null and eligible intervals are empty |
| `missing` | Required five-minute slots are missing; value null |
| `invalid` | Present source slots fail quantity validity; value null |
| `provisional` | Hour has not closed or source horizon does not cover it; value null |
| `pending` | A durable unfinished effect suppresses the affected hour/day; value null for the hour |
| `conflict` | Conflicting source evidence is quarantined; value null |
| `unassessed` | No committed assessment exists for the requested hour; value null |
| `unverified_native` | Native rows, metadata or proof do not establish the committed value; value null |
| `blocked` | Quantity/source contract cannot support a value; inspect `failure_reason` |

`failure_reason` is null or one of `voc_unit_unresolved`,
`resource_identity_mismatch`, `source_timestamp_unplaced`,
`source_horizon_unsettled`, `source_conflict`, `invalid_slots`, `missing_slots`,
or `source_evidence_invalid`. An unrecognized internal source failure is exposed
as `source_evidence_invalid`, never an arbitrary provider message.
Point `source_ids` are sealed source-manifest SHA-256 digests. Legacy daily
`source_ids` are the explicit native statistic aliases read for that day; they
are locators, not source-byte hashes.
`source_basis` is `points`, `legacy_daily`, or `unavailable`;
`method_basis` is `five_minute_complete_hour_v3`, `legacy_daily_cumulative`, or
`legacy_sample_mean`. Provenance confidence labels describe qualifications,
including `provider_ordered`, `archive_qualified`, `legacy_native_daily_value`
and `historical_sample_completeness_unproven`; they never override eligibility.

Day buckets add `calendar_start`, `calendar_end`, `calendar_hours`,
`point_observed_amount`, `observed_intervals`, and `coverage_reasons` (hour-state
counts). `start/end` describe only the requested part of that day;
`calendar_start/end` describe the whole local day. `observed_intervals` identify
the actually verified point hours even on a partial day. `eligible_intervals`
is empty on partial days, while a qualified legacy day enumerates its actual
UTC hour set with a legacy method basis. That enumeration is a daily comparison
basis, not reconstructed hourly values. `verified_hours` and slot counts always
describe point proof, including when a legacy day is chosen. Rejected available
legacy evidence adds `legacy_reason: legacy_calendar_mismatch` or
`legacy_day_not_eligible`.

For amount days, `value` and `observed_amount` are the point sum when points are
chosen, or the single legacy daily amount when fallback is chosen.
`point_observed_amount` preserves the separate point subtotal for disclosure;
it must never be added to the chosen legacy value. `complete_total` is non-null
only for a complete eligible day. Measurement day `value` is the mean of verified
point-hour means, or the selected legacy daily sample mean; its amount fields
are null. Nullable genuine min/max are never synthesized for legacy mean-only
records.

Every series `summary` has **`scope: query`**, `start/end` equal to the complete
request, and `verified_hours`, `expected_hours`, `closed_hours`, `eligible_hours`,
`eligible_buckets`, `legacy_days` and point-hour `coverage_reasons`. Amount
summaries add `observed_amount`, `complete_total`, and `eligible_amount`:
observed amount sums the chosen bucket basis including partial subtotals;
eligible amount includes only complete eligible buckets; complete total is null
unless every requested bucket is eligible. Measurement summaries add
`observed_mean`, `observed_min`, `observed_max` and
`summary_basis: verified_point_hours`; they do not infer historical sample
weights for legacy daily means. **Summaries repeat unchanged when a quantity
spans pages. Keep one summary per quantity/view token; never add page summaries.**

`coverage_revision` is the committed proof generation and advances only after
native verification and durable proof publication. `root_revision` also tracks
admission, pending work and operation progress. `source_revision` is a digest
of admitted source/policy, or null before admission; it is not proof of complete
import. `config_revision` binds the detached non-secret configuration.
`view_token` additionally binds pending suppression, native values/metadata,
identity, policy, timezone, query and fixed evaluation. Neither revision alone
is a paging token. The status entity's `hourly_history_revision` and
`hourly_history_status` attributes use the existing entity refresh lifecycle to
invalidate consumer views after closed-history corrections.

`operation` always contains `status`, `root_revision` and `has_pending`.
An unselected entry has `status: unselected` and no operation identity. Accepted
operations also contain `contract_version`, `operation_id`, `plan_digest`,
`cursor`, `batch_count`, `verified_batches`, `remaining_batches`, and nullable
`error`. Status is `accepted`, `in_progress`, `completed` or `blocked`.
Only `completed` with zero remaining batches completes that operation; verified
batches during a blocked operation do not complete the full repair.

If native reads, source objects, ownership or view consistency cannot be proved,
the response-producing action fails with the sanitized Home Assistant
`hourly_statistics_failed` error instead of returning fabricated empty history.
The caller marks its previous snapshot stale/unavailable. There is no successful
`failure` payload whose null values can be treated as zeros, and no implicit
query-side recovery or provider reacquisition.

## Stage an immutable source chunk

Upload through Home Assistant's authenticated `/api/file_upload` route, then call
`beestat_statistics.stage_hourly_source`. The call requires an active admin,
explicit loaded entry, opaque native `file_id`, exact original-byte `sha256`,
and `manifest`. It accepts no path or URL. The upload handle alone supplies no
producer identity or completeness proof.

`manifest` fields are: `contract_version: 3`, `config_entry_id`, `api_base`,
`account_anchors`, `resource` (`runtime_thermostat` or `runtime_sensor`), positive
`resource_id` and `thermostat_id`, `source_kind` (`provider` or `archive`),
`acquisition_id`, zero-based `chunk_index`, positive `chunk_count`, `format`
(`json` or `jsonl`), `start`, `end`, `acquired_at`, `source_end`, and
`unit_contract: beestat_points_v1`, plus `original_sha256`,
`original_byte_count`, and `chunk_byte_offset`. Timestamps include offsets; source acquisition
bounds retain the provider's inclusive-end convention. `source_end` is the last
observed interval start, not the download time.

Each original UTF-8 chunk is bounded to 8 MiB, 10,000 rows and JSON depth 16.
An assembled bundle is bounded to 2,048 chunks, 128 resources and 512 MiB;
the writer also preserves 256 MiB of free disk beyond an admitted object.
JSON supports point arrays/maps or a successful untruncated raw-point export,
including the exact native REST shape
`{"changed_states": [], "service_response": <raw-point export>}`. The changed-state
list must be empty, no additional outer fields are accepted, and the nested export
must pass the same status, completeness, identity and request-bound checks as a
root export. The original-byte hash and byte count bind the entire outer REST
response; the nested export is neither extracted nor reserialized for storage.
JSONL contains one original point object per nonempty line. JSON originals must
fit one chunk. JSONL originals of up to 128 MiB may be split only after existing
LF bytes, preserving whitespace, CRLF and every original byte. The final chunk
may end without a newline. Offsets are byte offsets into the original, not row
numbers. All chunks of an original bind its exact digest and byte count; complete
bundle admission verifies contiguous offsets, no overlaps/gaps, full length and
the hash of the byte-for-byte concatenation before using any rows. Acquisition
chunk order can cover multiple originals and is independent of each original's
byte offset. Reserializing rows does not reproduce an original and cannot pass
its original digest. Arbitrary partial JSON/UTF-8 fragments and compressed archives
are unsupported. Original bytes are retained before the native upload context
closes, including its cleanup. Duplicate identical content is idempotent.

The response contains `contract_version: 3`, `source_id` (sealed manifest digest),
`sha256`, `manifest`, `byte_count`, `row_count` and `confidence`. Staging writes
only immutable original source and sealed manifest objects. It does not change
the active root journal, marker, writer reservations, selection or Recorder.

Provider chunk order within one acquisition preserves last-row-wins, including
invalid rows/tombstones. Equal cross-source points coalesce with provenance.
Separate acquisitions gain no precedence from the order of `source_ids`.
Archive-only points before the first provider point require the explicit archive
policy below and remain qualified: original ordering, overwrites and settlement
are not proven. Conflicting evidence quarantines the affected hour. A shorter or
empty successful response does not delete previously admitted observations;
explicit ordered corrections/tombstones do not resurrect older archive values.

## Plan and apply one exact operation

`beestat_statistics.plan_hourly_history` requires an active admin and:

```yaml
config_entry_id: example_entry
source_ids: [<sealed-source-manifest-sha256>]
quantity_ids: [thermostat:101:fan_runtime_hours]
start: '2026-09-01T00:00:00+00:00'
end: '2026-09-02T00:00:00+00:00'
archive_policy: reject
daily_policy: complete_points_else_legacy_day
operation_id: example-adoption-01
expected_revision: 0
consumer_contract:
  contract_version: 3
  consumers:
    - consumer_id: example-history-display
      version: example-candidate
      history_contract: 3
      daily_policy: complete_points_else_legacy_day
recovery_reference: example-consistent-recovery-set
detail_offset: 0
detail_limit: 100
```

`archive_policy` is `reject` or `before_first_provider`. The consumer and recovery
bindings are explicit operator declarations retained in the plan, not proof
that this service has validated another product or created a backup. Planning
reads immutable sources, detached current configuration/journal and fenced
bounded native rows. It performs no writes or reconciliation. Pending or
unsupported ownership returns a blocked result. It returns the exact normalized
`request` (including fixed `evaluated_at`), `operation_id`, `plan_digest`,
`expected_revision`, `status`, `blocking_reasons`, counts and paged `batches`.
Each batch identifies a quantity/UTC month, bounds, before/intended digests and
counts. Page detail limits are 1–100; changing detail offset does not change the
plan's semantic digest. No giant row response is required.

To adopt/repair, call `beestat_statistics.apply_hourly_history` with the explicit
`config_entry_id`, returned `plan_digest`, and returned `request` as `plan`.
The active admin requirement is checked again. Apply regenerates the intent
from the sealed evidence and verifies the exact digest/current revisions before
durably accepting it. A different plan is never substituted. It returns promptly
with `contract_version`, `operation_id`, `plan_digest`, `status`
(`accepted`, `in_progress`, `completed` or `blocked`), progress and `root_revision`.
Repeating the same ID/digest observes/resumes the original intent; a conflicting
reuse fails. Query/configuration expose progress without effectful polling.

The existing entry worker executes one quantity/month batch at a time, at most
744 hour slots. It saves exact before/intended rows before submission, fences
and compares native readback, persists the proof shard, then advances the root
revision and clears pending. Cancellation cannot turn uncertain effects into
success or a new plan. Recovery uses that same immutable intent; a third native
state or metadata mismatch holds the operation. Partially verified batches do
not mean the full operation is complete. Reservations persist through failures,
disablement and reload; unrelated legacy quantities, including VOC, keep their
existing writer.

V3 adoption changes the journal contract so older releases fail closed. Existing
v2 users are not automatically converted. Recovery includes the entry/marker,
root, immutable sources/proofs/operation, Recorder rows and metadata, consumer
configuration/assets, timezone/method and exact component versions. Replacing
code alone is not a rollback.

## Routine corrections after adoption

The existing entry import cadence also refreshes enabled adopted quantities
within the accepted 45-day policy. A pending operation resumes its sealed
intent before a different acquisition can be accepted. Historical rebuilds
outside this routine path require a new explicit plan.

One acquisition resolves all ordered resource chunks, including invalid rows
and tombstones, before comparison with a named committed source revision and
baseline digest. An empty or shortened response never deletes old observations.
If no observation changes, the pass adds no source object and earns no automatic
coverage or settlement credit. The existing writer may reevaluate retained
observations at a fixed evaluation and import newly closed, qualified hours.
Twelve samples and elapsed time do not replace the required source horizon:
insufficient evidence remains provisional. Exact durable intent, native
readback and durable proof are required before coverage advances, including
after interruption; pending or failed work remains suppressed. Queries perform
none of these effects.

Changed observations use `integration_delta_v1` with evaluation version
`beestat_points_delta_v1`. The sealed envelope binds acquisition identity and
actual source bounds, incoming integration-export byte hashes/counts, baseline
revision/digest and changed original row objects. These hashes commit to incoming
bytes; the delta does not retain or reconstruct the full incoming response.
Externally staged originals retain the exact byte-reassembly contract above.
The manager rechecks the baseline before acceptance; stale input requires a new
comparison, never substitution under an existing operation identity.

Admitted initial originals, accepted deltas, proof and operation dependencies
remain retained. The monthly source index selects operation dependencies while
preserving corrections, tombstones and supersession outside the newest window.
The 2,048-chunk and 512 MiB limits apply to the selected operation bundle.
Internal catalog scans validate complete acquisition metadata before selecting
whole overlapping originals; unrelated retained declarations do not consume the
selected bundle's chunk allowance. Explicit source requests still accept at most
2,048 IDs. Exceeding a selected bundle limit blocks explicitly; the integration
does not evict history or omit relevant evidence to fit the limit. Source
conflicts suppress affected claims until their resolution is proved.
Before replacing a conflicted root, the same immutable operation namespace
durably invalidates its predecessor token. A cold reader checks that record even
if Home Assistant has not yet saved the updated entry marker. The in-memory hold
starts before the write; cancellation and replacement workers wait for the same
write and readback. A hold survives a crash only after its invalidation record
is durably verified. A failed write does not prove that persistence. Retain these
records with the consistent recovery set; changing the marker or restoring only
an older root cannot remove a verified hold.

## Consumer display semantics

A `24h` display requests 24 aligned hourly buckets ending at the next whole UTC
hour, including the current provisional bucket. Label the actual bounds; this
is not an exact continuous now-minus-24-hours window. Week/Month use seven/thirty
local dates ending today. Compare cooling/heating only on the same eligible UTC
interval set and method basis, with stable Cooling/Heating order if incomparable
or tied. Keep fan first and color/legend identities stable.

One explicitly named conditioning-mode average uses its selected mode's own
eligible denominator. For `24h`, select the mode with the larger amount only
when both full displayed eligible hourly interval sets and their method match;
cooling wins a tie. Otherwise select cooling when it has eligible observations,
then heating. For Week/Month, select the mode from the intersection of comparable
eligible complete local dates, with the same tie and fallback rules. That daily
intersection does not establish which mode dominates the whole displayed range.

After selecting the mode, average all its eligible closed hours (`24h`) or
complete eligible local dates (Week/Month), including observed zeros. Do not
restrict the average to the ranking intersection. For example, hourly cooling
amounts `[0.1, 0.3]` and heating `[1.75, null]` have different eligible interval
sets: select cooling and show its 12-minute average. Exclude provisional and
partial observations. A legacy daily bar qualified with
`historical_sample_completeness_unproven` is not eligible for the point-proven
average. Omit the numeric line when the selected mode has no eligible denominator.

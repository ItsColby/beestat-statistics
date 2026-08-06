# Beestat Statistics v2026.8.3

## Changed

- Preserve private Home Assistant and Beestat details at every error boundary
  while retaining a bounded exception fingerprint that identifies the
  integration-owned module, function, and line for local diagnosis.
- Validate the integration against current stable Home Assistant Core
  `2026.8.0` in addition to the minimum supported `2026.7.1` release.
- Preserve complete partial-import counts while retaining at most three
  identifier-free skipped-window examples in current state and diagnostics.
- Refresh enabled mapping Repairs immediately when a referenced entity-registry
  record is removed, renamed, or restored, with listener cleanup on unload.
- Bound ordinary active-alert detail to three examples while preserving the
  complete count and category.

## Fixed

- Install the Home Assistant test harness before upgrading to the exact Core
  release under test, avoiding resolver failures when the newest harness still
  declares a matching beta Core dependency.

## Quality

- Continue using Home Assistant's integration quality scale as a practical
  review reference while optimizing for correctness, privacy, recovery, and
  maintainability in the integration's real private deployment.
- Run the latest Ruff, ShellCheck, actionlint, and zizmor releases directly in
  validation; enable Ruff's stable native, blind-exception, and Pylint
  convention checks; retain ambiguous-Unicode detection; add focused network,
  cryptography, and comprehension checks; ratchet complexity to the current
  demonstrated ceiling; and remove the redundant Ruff GitHub Action dependency.
- Enforce Ruff's exception-name suffix rule to prevent ambiguous custom
  exception names from entering the integration.
- Add zero-baseline Ruff Bugbear, timezone-awareness, and logging checks while
  retaining the existing debugger-statement rule.
- Validate current stable Core `2026.8.0` with test harness `0.13.354`, whose
  dependency metadata now targets that final release directly.
- Exercise the native filter date entity's exact click-boundary attributes,
  update path, and unavailable state with Home Assistant runtime models.

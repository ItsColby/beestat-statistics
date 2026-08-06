# Beestat Statistics v2026.8.3

## Changed

- Preserve private Home Assistant and Beestat details at every error boundary
  while retaining a bounded exception fingerprint that identifies the
  integration-owned module, function, and line for local diagnosis.
- Validate the integration against current stable Home Assistant Core
  `2026.8.0` in addition to the minimum supported `2026.7.1` release.

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
  convention checks; retain ambiguous-Unicode detection; ratchet complexity to
  the current demonstrated ceiling; and remove the redundant Ruff GitHub
  Action dependency.
- Exercise the native filter date entity's exact click-boundary attributes,
  update path, and unavailable state with Home Assistant runtime models.

# AGENTS.md instructions for beestat-statistics

Apply global Codex preferences first. This file owns repo-local guidance for the Beestat Statistics Home Assistant custom integration.

## Start Here

Read `docs/architecture.md` before structural, API-boundary, entity,
config-flow, Recorder/statistics, or release-layout changes.

## Optimization And Quality Target

Optimize for David's private Home Assistant runtime: correctness, privacy,
recoverability, low maintenance, Home Assistant compatibility, and clear Codex
operation. Use Home Assistant's current
[integration quality scale rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/)
as a required starting reference for holistic reviews and material design or
behavior changes, but evaluate each applicable rule by evidence and local value.
The scale is not a certification target or automatic backlog. Do not pursue an
omitted rule, coverage percentage, strict-typing campaign, or architectural
expansion unless it closes a concrete risk or materially improves future work.
`quality_scale.yaml` is descriptive compatibility evidence under this policy.

## Public Privacy Boundary

- This repository is public. Do not commit personal names, private email addresses, local filesystem paths, private Home Assistant entity IDs, household room/device names, private hostnames, private IP addresses, screenshots, diagnostics, logs, tokens, credentials, or local deployment evidence.
- Keep maintainer-specific deployment, mirror, and live-installation workflows outside this public repository. Public repo guidance may describe generic HACS release validation, but not private Home Assistant installations or local paths.
- Use generic fixture names in tests and documentation, such as `zone_a`, `zone_b`, `room_sensor_a`, and `room_sensor_b`. Do not use names copied from a real household.
- Run the static privacy guard before pushing changes that touch tests, docs, workflows, scripts, or metadata.

## Validation

After changing `.github/workflows`, run `actionlint` from the repository root.
Keep `shellcheck` available on `PATH`; actionlint uses it automatically for
Bash and `sh` `run:` steps.

For focused iteration, run the directly affected unittest module, compile the
changed package/test surface, and always run the public-safety guard when the
change touches code, tests, docs, workflows, scripts, or metadata. Example:

```powershell
python -m unittest tests.test_statistics_builder
python -m compileall -q custom_components/beestat_statistics tests
python scripts/check_public_safety.py
```

Before integration or release, use the repo venv when available and run the
full local tier:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m compileall -q custom_components\beestat_statistics tests scripts
.\.venv\Scripts\python.exe scripts\check_beestat_api_surface.py
.\.venv\Scripts\python.exe scripts\check_public_safety.py
```

Run maintainer-specific exact-value scans only from a maintainer-controlled
local publication gate. Never add private values to a tracked test, checker,
fixture, or GitHub Actions secret, even when split or encoded.

Validate JSON metadata after edits to JSON files:

```powershell
.\.venv\Scripts\python.exe -c "import json, pathlib; [json.loads(pathlib.Path(path).read_text(encoding='utf-8')) for path in ['custom_components/beestat_statistics/manifest.json','custom_components/beestat_statistics/translations/en.json','custom_components/beestat_statistics/icons.json','hacs.json','docs/beestat-api-surface.json']]"
```

Home Assistant config-flow test requirements are owned by two explicit lanes:
`requirements-ha-test.txt` proves the minimum supported Core release, while
`requirements-ha-test-current.txt` proves the current released Core used by the
maintainer. Keep both exact and update the current lane after a stable Core
upgrade rather than testing a beta as a release gate.

```powershell
python -m pip install pytest-homeassistant-custom-component==0.13.345
python -m pip install --upgrade -r requirements-ha-test.txt
python -m pytest tests/test_config_flow_ha.py -q
python -m pip install pytest-homeassistant-custom-component==0.13.353
python -m pip install --upgrade -r requirements-ha-test-current.txt
python -m pytest tests/test_config_flow_ha.py -q
```

Install the harness before upgrading to the exact Core under test. The current
harness can temporarily declare a beta Core after the matching final release is
available, which makes a single requirements transaction unresolvable even
though the harness works with the final release.

The Home Assistant harness is Linux-only because Core imports `fcntl`; on
Windows, run these lanes in Docker or defer them to the GitHub workflow. If
local Python does not satisfy a pinned lane, state that the HA-specific pytest
gate is deferred to a compatible environment. Do not weaken either
requirements file just to make an incompatible local venv pass.

Before reporting complete, read back `git status --short --branch` and list any validation that could not run.

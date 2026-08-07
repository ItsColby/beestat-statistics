# AGENTS.md instructions for beestat-statistics

Apply global Codex preferences first. This file owns repo-local guidance for the Beestat Statistics Home Assistant custom integration.

## Start Here

Read `docs/architecture.md` before structural, API-boundary, entity,
config-flow, Recorder/statistics, or release-layout changes.

For David-maintained cross-portfolio engineering practice, use the external
`maintain-ha-custom-integrations` skill when available. This repository's
architecture, tests, privacy boundary, and CI remain authoritative for Beestat
Statistics. Release, HACS, installation, restart, live validation, and rollback
belong to `release-ha-custom-integrations`.

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
Keep the latest `shellcheck` available on `PATH`; the `shellcheck-py` package
provides it and actionlint uses it automatically for Bash and `sh` `run:` steps.

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
.\.venv\Scripts\python.exe -m pip install --upgrade ruff mypy shellcheck-py zizmor
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m compileall -q custom_components\beestat_statistics tests scripts
.\.venv\Scripts\ruff.exe check custom_components tests scripts
.\.venv\Scripts\ruff.exe format --check custom_components tests scripts
.\.venv\Scripts\python.exe -m mypy --strict custom_components/beestat_statistics
$env:GH_TOKEN = gh auth token
if (-not $env:GH_TOKEN) { throw "GitHub CLI authentication required" }
try {
  .\.venv\Scripts\zizmor.exe --strict-collection --persona auditor .
  if ($LASTEXITCODE -ne 0) { throw "zizmor audit failed" }
} finally {
  Remove-Item Env:GH_TOKEN
}
.\.venv\Scripts\python.exe scripts\check_beestat_api_surface.py
.\.venv\Scripts\python.exe scripts\check_public_safety.py
```

Before creating an immutable release, wait for CodeQL analysis of the exact
candidate/default-branch commit and inspect the repository's open code-scanning
alerts. A successful CodeQL workflow proves analysis completed, not that the
result has no findings. Resolve or explicitly disposition candidate-introduced
alerts before tagging.

Run maintainer-specific exact-value scans only from a maintainer-controlled
local publication gate. Never add private values to a tracked test, checker,
fixture, or GitHub Actions secret, even when split or encoded.

Validate JSON metadata after edits to JSON files:

```powershell
.\.venv\Scripts\python.exe -c "import json, pathlib; [json.loads(pathlib.Path(path).read_text(encoding='utf-8')) for path in ['custom_components/beestat_statistics/manifest.json','custom_components/beestat_statistics/translations/en.json','custom_components/beestat_statistics/icons.json','hacs.json','docs/beestat-api-surface.json']]"
```

Home Assistant runtime test requirements are owned by one formal,
dependency-coherent lane. `requirements-ha-test.txt` pins the supported stable
Core release matched by the published harness, currently Core `2026.8.0` with
harness `0.13.354`. Do not test a beta as a release gate.

```powershell
python -m pip install pytest-homeassistant-custom-component==0.13.354
python -m pip install --upgrade -r requirements-ha-test.txt
python -m pip check
python -m pytest tests/test_config_flow_ha.py tests/test_runtime_ha.py -q
```

Keep the harness and exact Core installation as separate steps. The harness
owns its compatible pytest dependency and `requirements-ha-test.txt` pins Core
only. After the final dependency installation, run a literal `python -m pip
check` and treat every conflict as a failure. The maintained instance runs Core
`2026.8.1`, but harness `0.13.354` still pins Core `2026.8.0`. Keep the formal
lane dependency-coherent at `2026.8.0`; bounded direct `2026.8.1` validation is
partial evidence only and does not clear the installed-Core or public-release
gate. Advance the HACS minimum, blueprint minimum, Core requirement, harness,
CI label, documentation, and assertions together only after a compatible
harness is published.

The Home Assistant harness is Linux-only because Core imports `fcntl`; on
Windows, run this lane in Docker or defer it to the GitHub workflow. If local
Python does not satisfy the pin, state that the HA-specific pytest gate is
deferred to a compatible environment. Do not weaken the requirements file just
to make an incompatible local venv pass.

Before reporting complete, read back `git status --short --branch` and list any validation that could not run.

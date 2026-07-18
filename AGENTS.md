# AGENTS.md instructions for beestat-statistics

Apply global Codex preferences first. This file owns repo-local guidance for the Beestat Statistics Home Assistant custom integration.

## Start Here

Read `docs/architecture.md` before structural, API-boundary, entity,
config-flow, Recorder/statistics, or release-layout changes.

## Public Privacy Boundary

- This repository is public. Do not commit personal names, private email addresses, local filesystem paths, private Home Assistant entity IDs, household room/device names, private hostnames, private IP addresses, screenshots, diagnostics, logs, tokens, credentials, or local deployment evidence.
- Keep maintainer-specific deployment, mirror, and live-installation workflows outside this public repository. Public repo guidance may describe generic HACS release validation, but not private Home Assistant installations or local paths.
- Use generic fixture names in tests and documentation, such as `zone_a`, `zone_b`, `room_sensor_a`, and `room_sensor_b`. Do not use names copied from a real household.
- Run the static privacy guard before pushing changes that touch tests, docs, workflows, scripts, or metadata.

## Validation

Use the repo venv when available:

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

Home Assistant config-flow tests require Python `3.14.2` or newer because `homeassistant==2026.7.1` requires it:

```powershell
python -m pip install -r requirements-ha-test.txt
python -m pytest tests/test_config_flow_ha.py -q
```

If local Python is older, state that the HA-specific pytest gate is deferred to the GitHub workflow or a Python `3.14.2+` environment. Do not weaken `requirements-ha-test.txt` just to make an older local venv pass.

Before reporting complete, read back `git status --short --branch` and list any validation that could not run.

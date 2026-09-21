# Developing Beestat Statistics

The [architecture](architecture.md) explains the behavior that changes must
preserve. Runtime code is in `custom_components/beestat_statistics/`; action
schemas and English help live beside it. Keep user operations in the
[user guide](usage.md), and describe release-specific behavior in
[release notes](../RELEASE_NOTES.md).

## Select validation for the change

The default runner mode is `affected`. Preview an exact candidate comparison
before running it:

```powershell
.\scripts\verify-release-local.ps1 -Base <base-commit> -Head HEAD -PlanOnly
.\scripts\verify-release-local.ps1 -Base <base-commit> -Head HEAD
```

For a working edit, use `-ChangedPath scripts/verify-release-local.sh` instead of
refs. On Linux, use `bash scripts/verify-release-local.sh affected container ""`
with `--base <base-commit> --head HEAD`, or repeated `--path <relative-path>`;
add `--plan-only` to inspect the JSON plan without snapshots or installations.
The PowerShell `-PlanOnly` preview uses `python` from PATH, which must be
Python 3.14. Bash planning and snapshot admission find an existing Python 3.14
through `python3.14`, an installed uv runtime, or `VALIDATION_PYTHON`.
The public-safety path guard rejects linked leaves and ancestors before planning
reads source and before the container payload is copied. The planner, guard and
interpreter remain trusted executable tooling. Neither preview downloads a runtime.
Explicit paths describe the complete change being accepted and select checks,
not acquisition scope: planning parses all Python under `custom_components`,
`tests` and `scripts`; execution copies tracked and nonignored files.
The refs mode requires a clean checkout with the candidate as its head and rejects
uncommitted edits. An empty verified comparison selects no jobs. Missing comparison
input and unmapped changes fail with an unresolved applicability message.

The product-owned planner traces local Python imports and reviewed direct-file
consumers. Dependency-light unittest modules stay directly under `tests/`;
HA pytest modules may use subdirectories and retain their relative paths in
selected runs. Changed tests run in their native collector; runtime changes include
the affected success, failure, and recovery consumers in both maintained HA
environments. A support requirements change selects that environment, without
invalidating the unchanged sibling lane. Runner and workflow dependency declarations
are compared against the supplied base, or HEAD for working-path selections;
changed harness, Python image, action, and tool pins select their actual consumers.
The separate API-surface workflow and its retained inventory select the existing
offline checker tests and public-safety checks; workflow edits also select
Actionlint/ShellCheck and workflow security analysis. These checks do not make
upstream API requests or refresh the retained inventory.
An unavailable dependency comparison remains unresolved. The Bash runner remains
the owner of exact local tool versions. Tooling, workflow, public-content and
metadata checks are selected independently of product tests. Parsed `pyproject.toml`
changes select all Python static checks for Ruff settings, or the minimum lane's
product typing checks for mypy settings. Comments and formatting select no tool
consumers; the normal public-safety check still applies. Changes to `pytest.ini`
select both HA test lanes and the static configuration check; native pytest
validates the configuration when those lanes execute. A new pyproject pytest
table has no mapped consumer. Malformed, unavailable, and other unmapped
pyproject configuration changes remain unresolved for explicit review.

Container execution rebuilds the affected plan from the captured payload, retaining
the preview's resolved dependency baseline and selected paths. That plan contains
the exact lane commands, which are reused without reading original source files
again. Ref comparisons also require the captured files to match the clean candidate.
Keep edits stable while the payload is being copied. Native execution uses one
captured plan and requires its working tree to remain stable for the run.

Pull requests and main pushes use this same selection. The stable Release gate
requires the planning job and every selected job to succeed, and accepts skipped
jobs only when the plan excludes them. Manual workflow dispatch explicitly runs
the complete lanes. `all`, `unit`, `minimum`, `current`, and `release` remain
explicit complete-lane requests. Reuse evidence whose source and environment
have not changed; a merge alone does not invalidate it. Local checks do not
replace HACS, authorize publication, or establish live behavior.

When both Home Assistant lanes are selected locally, the container runner starts
them together after selected static checks pass and waits for both before any
selected Hassfest check or snapshot cleanup. Single-lane selections and native
runs remain sequential. To limit local concurrency, run selected lanes separately
with `--only minimum` and `--only current` on the Bash affected route.

## Run complete lanes when applicable

The examples below run complete validation lanes. Use them only when the change
requires that complete scope; use the affected commands above for ordinary changes.

The maintained runner executes the same validation lanes used by CI. On Windows,
install Ubuntu 24.04 under WSL2 with rootless Podman, then run from the checkout:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-release-local.ps1 -Mode all
```

On Linux with Podman:

```bash
bash scripts/verify-release-local.sh all
```

Choose `-Mode unit`, `minimum`, `current` or `release` in PowerShell, or use that
name instead of `all` in the shell command.

| Lane | What it checks |
| --- | --- |
| `unit` | Dependency-light tests, Ruff, compilation, JSON and whitespace, public safety, actionlint, ShellCheck and workflow security checks |
| `minimum` | Native pytest discovery excluding unit-owned modules in the supported-minimum HA environment, plus strict mypy |
| `current` | Native pytest discovery excluding unit-owned modules in the current target HA environment |
| `release` | Hassfest validation of the integration; this lane does not publish |

The minimum is Core `2026.8.0` in
[`requirements-ha-test.txt`](../requirements-ha-test.txt), paired with harness
`pytest-homeassistant-custom-component==0.13.354`. The current target is Core
`2026.9.3` in [`requirements-ha-current.txt`](../requirements-ha-current.txt),
paired with harness `0.13.366`. Each lane installs Core after its matching
harness and runs `python -m pip check` after the final dependency installation.
The HA environments require Linux and Python 3.14.2 or later; native Windows
Python cannot replace them. Hosted jobs select Python 3.14.

The container backend validates one read-only snapshot of tracked and nonignored
new files, including uncommitted edits. Its images and tool versions are pinned
in the runner. `all` runs independent containers concurrently and returns failure
if any lane fails. Each Python environment is isolated; the named Podman pip
volume caches downloads, not validation results. On interruption, the container
runner waits for active lanes before removing the snapshot and returns the
interrupt status; this wait has no shutdown deadline. Only the unit container
provisions Git; the Home Assistant lanes exclude the Git-dependent unit tests.

CI passes `native` as the shell runner's second argument. That backend needs
Python with pip and venv, Go for actionlint and Docker for Hassfest, and runs its
selected lanes sequentially in separate temporary Python environments.
Actionlint provisions the pinned ShellCheck version in its own temporary
environment. The PowerShell wrapper resolves WSL paths and the checkout's Git
directory; it is the supported Windows route to the container checks.

For a quick dependency-light check without containers:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install tzdata
.\.venv\Scripts\python.exe scripts\run_dependency_light_tests.py
```

The unit selector identifies dependency-light `test_*.py` modules by their imports
and fails on invalid or empty discovery. Both HA lanes pass `--home-assistant`
to let pytest discover the complete test tree with the real harness, excluding
only the modules already assigned to `unit`. New pytest-supported file patterns
remain covered by native discovery.
A dependency-light pass does not establish HA compatibility. Report skipped or
unavailable checks separately from passes.

## Keep source contracts verifiable

The [Validate workflow](../.github/workflows/validate.yaml) selects applicable lanes
and the separate hosted HACS check. A manual full dispatch requires all five
to succeed. The [quality inventory](../custom_components/beestat_statistics/quality_scale.yaml)
records claimed HA rules; it is not an official certification or an obligation
to implement every unlisted rule.

The public-safety checker scans current tracked/nonignored files for private
material, unsafe links and unreviewed binaries. It does not audit Git history.
Keep diagnostics, household configuration, databases, credentials and deployment
records outside the public source. Tests and examples use synthetic identities.

For documentation changes, verify claims against their implementation or schema,
check relative links and examples, and preserve translated placeholders and
stable labels. Historical records retain their version-specific facts. Avoid
making prose layout or sentence wording a runtime compatibility requirement.

The API inventory is generated by
[`check_beestat_api_surface.py`](../scripts/check_beestat_api_surface.py):

```powershell
.\.venv\Scripts\python.exe scripts\check_beestat_api_surface.py
```

The checker reads one immutable upstream revision and verifies the watched blobs
before comparing with [the saved inventory](beestat-api-surface.json). Review
actual source drift before using `--update`; that flag replaces the snapshot
only after a complete acquisition. Integration-use decisions belong in the
checker and must remain aligned with the generated inventory. The separate
monthly workflow runs this check without changing the integration's API scope.

## Prepare a release

Keep the manifest version and newest release-note version aligned. Validate the
candidate, then require the protected pull request's plan, selected validation
jobs and aggregate check, together with the configured CodeQL checks. Confirm
that excluded jobs have applicable retained evidence where the release needs it.
Merge through branch protection and require Validate and CodeQL on the resulting
`main` commit. Inspect complete logs and code-scanning findings; a successful
analysis job is not proof that it found no issues.

Publish an immutable version tag and GitHub Release against that validated
commit, matching the manifest. Give the release the appropriate version's notes;
when using GitHub CLI, pass a Markdown file through `--notes-file` and identify
the target explicitly. Verify repository metadata, issues, relevant topics and
the brand icon for HACS distribution. An already immutable release is not an
editable staging area.

Installing through HACS, restarting an HA instance and proving adoption are
separate operational steps. A source test or published release does not prove
which version an instance has loaded or that its consumers recovered.

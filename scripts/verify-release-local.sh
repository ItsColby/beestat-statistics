#!/usr/bin/env bash
set -euo pipefail
mode="${1:-affected}"
backend="${2:-container}"
source_git_dir="${3:-}"
if [[ "$mode" != affected ]] && (( $# > 3 )); then
  echo "Usage: $0 [affected|all|unit|minimum|current|release] [container|native] [git-directory]" >&2
  exit 2
fi
case "$mode" in
  all|unit|minimum|current|release|affected) ;;
  *) echo "Unknown mode: $mode" >&2; exit 2 ;;
esac
case "$backend" in
  container|native) ;;
  *) echo "Unknown backend: $backend" >&2; exit 2 ;;
esac
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$source_root"
# Refuse inherited repository selection before planning or snapshot reads.
for variable in GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CONFIG GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT GIT_OBJECT_DIRECTORY GIT_DIR GIT_WORK_TREE GIT_IMPLICIT_WORK_TREE GIT_GRAFT_FILE GIT_INDEX_FILE GIT_REPLACE_REF_BASE GIT_PREFIX GIT_SHALLOW_FILE GIT_COMMON_DIR GIT_CEILING_DIRECTORIES GIT_DISCOVERY_ACROSS_FILESYSTEM; do
  if [[ -v "$variable" ]]; then
    echo "Inherited local Git overrides are not supported: $variable" >&2
    exit 2
  fi
done
export GIT_NO_REPLACE_OBJECTS=1
source_git=(git --no-replace-objects -C "$source_root")
if [[ -n "$source_git_dir" ]]; then
  if [[ ! -d "$source_git_dir" ]]; then
    echo "The explicit Git directory must be an existing metadata directory." >&2; exit 2
  fi
  source_git=(git --no-replace-objects --git-dir="$source_git_dir" --work-tree="$source_root")
fi
actual_root="$("${source_git[@]}" rev-parse --show-toplevel)"
if [[ "$(cd "$actual_root" && pwd -P)" != "$(cd "$source_root" && pwd -P)" ]]; then
  echo "Git target root does not match the wrapper source root." >&2; exit 2
fi
validation_python="${VALIDATION_PYTHON:-}"
if [[ "$mode" == affected && -z "$validation_python" ]]; then
  if command -v python3.14 >/dev/null 2>&1; then
    validation_python="$(command -v python3.14)"
  elif command -v uv >/dev/null 2>&1; then
    validation_python="$(uv python find 3.14 --no-python-downloads)"
  elif [[ -x "$HOME/.local/bin/uv" ]]; then
    validation_python="$("$HOME/.local/bin/uv" python find 3.14 --no-python-downloads)"
  else
    echo "Affected planning requires Python 3.14; set VALIDATION_PYTHON to an existing interpreter." >&2
    exit 2
  fi
fi
affected_args=()
affected_only=""
affected_plan=""
if [[ "$mode" == affected ]]; then
  if (( $# >= 3 )); then shift 3; else shift "$#"; fi
  plan_only=false
  while (( $# )); do
    case "$1" in
      --only) affected_only="${2:?Missing lane}"; shift 2 ;;
      --plan-only) plan_only=true; shift ;;
      --base|--head)
        # Reuse immutable input for initial planning and every later lane command.
        oid="$("${source_git[@]}" rev-parse --verify --end-of-options "${2:?Missing revision}^{commit}")"
        affected_args+=("$1" "$oid"); shift 2 ;;
      *) affected_args+=("$1"); shift ;;
    esac
  done
  if [[ -n "$source_git_dir" ]]; then affected_args+=(--git-directory "$source_git_dir"); fi
  affected_plan="$("$validation_python" "$source_root/scripts/run_dependency_light_tests.py" --plan "${affected_args[@]}")"

  if [[ -n "$affected_only" && "$affected_only" != unit && "$affected_only" != minimum && "$affected_only" != current && "$affected_only" != release ]]; then
    echo "Unknown affected lane: $affected_only" >&2; exit 2
  fi
  if [[ -n "$affected_only" && "$(printf '%s' "$affected_plan" | "$validation_python" -c 'import json,sys; print(str(json.load(sys.stdin)["jobs"][sys.argv[1]]).lower())' "$affected_only")" != true ]]; then
    echo "Plan did not select $affected_only" >&2; exit 2
  fi
  if [[ "$plan_only" == true ]]; then printf '%s\n' "$affected_plan"; exit 0; fi
  if [[ "$(printf '%s' "$affected_plan" | "$validation_python" -c 'import json,sys; print(any(json.load(sys.stdin)["jobs"].values()))')" == False ]]; then
    echo "No validation jobs apply to the verified empty comparison."; exit 0
  fi
fi

if [[ "$backend" == container ]]; then
  repo_root="$(mktemp -d)"
  # An interrupted wait can leave lanes using the snapshot. Drain this
  # runner's jobs before deleting it, and retain the interrupt exit status.
  trap 'trap "" INT TERM; wait; rm -rf "$repo_root"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  "${source_git[@]}" ls-files --cached --others --exclude-standard -z |
    while IFS= read -r -d '' path; do
      if [[ -e "$source_root/$path" || -L "$source_root/$path" ]]; then
        printf '%s\0' "$path"
      fi
    done |
    tar -C "$source_root" --null --files-from=- --create --file=- |
    tar -C "$repo_root" --extract --file=-
  # The pinned Actionlint image runs as an unprivileged user.
  chmod a+rx "$repo_root"
  # DrvFS exposes regular files as executable unless metadata is enabled.
  find "$repo_root" -type f -exec chmod a-x {} +
  git -C "$repo_root" -c init.templateDir= init -q
  # The curated payload can contain tracked files matching source ignore rules.
  git -C "$repo_root" add -A -f
fi

python_image="docker.io/library/python@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc"
actionlint_image="docker.io/rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
hassfest_image="ghcr.io/home-assistant/hassfest@sha256:8cd7bdb8f82430c2c13703290b1fc38dcc99957dd76ad3f230035ecee70b672d"
run_python() (
  local needs_git="${2:-true}"
  if [[ "$backend" == native ]]; then
    # Keep support lanes isolated, including when running all lanes locally.
    local environment
    environment="$(mktemp -d)"
    trap 'rm -rf "$environment"' EXIT
    python -m venv "$environment"
    cd "$repo_root"
    PATH="$environment/bin:$PATH" PYTHONPYCACHEPREFIX="$environment/pycache" \
      PIP_DISABLE_PIP_VERSION_CHECK=1 bash -euc "$1"
  else
    podman run --rm -e HOME=/tmp/home -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
      -e PIP_ROOT_USER_ACTION=ignore -e DEBIAN_FRONTEND=noninteractive \
      -e PIP_COMPILE=0 -e PIP_CACHE_DIR=/pip-cache \
      -e PYTHONPYCACHEPREFIX=/tmp/pycache -e XDG_CACHE_HOME=/tmp/cache \
      -e RUFF_CACHE_DIR=/tmp/ruff-cache -e MYPY_CACHE_DIR=/dev/null \
      -e 'PYTEST_ADDOPTS=-p no:cacheprovider' \
      -v "$repo_root:/workspace:ro" -w /workspace \
      --mount type=volume,source=beestat-statistics-validation-pip,target=/pip-cache \
      "$python_image" bash -euc \
      'if [[ "$1" == true ]]; then
         apt-get update -qq || exit "$?"
         apt-get install -y -qq --no-install-recommends git >/dev/null || exit "$?"
       fi
       bash -euc "$2"' \
      local-validation "$needs_git" "$1"
  fi
)
run_actionlint() (
  if [[ "$backend" == native ]]; then
    local bin
    bin="$(mktemp -d)"
    trap 'rm -rf "$bin"' EXIT
    python -m venv "$bin"
    "$bin/bin/python" -m pip install "shellcheck-py==0.11.0.1"
    GOBIN="$bin/bin" go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12
    cd "$repo_root"
    PATH="$bin/bin:$PATH" "$bin/bin/actionlint"
  else
    podman run --rm -v "$repo_root:/repo:ro" -w /repo "$actionlint_image"
  fi
)
run_unit() {
  run_actionlint
  run_python '
    python -m pip install "ruff==0.16.2" "shellcheck-py==0.11.0.1" "zizmor==1.29.0"
    zizmor --strict-collection --persona auditor .
    shellcheck scripts/verify-release-local.sh
    python -m ruff format --check custom_components tests scripts
    python -m ruff check custom_components tests scripts
    python scripts/run_dependency_light_tests.py
    python -m compileall -q custom_components/beestat_statistics tests scripts
    python scripts/check_public_safety.py
    python - <<"PY"
import json
from pathlib import Path
for name in ("custom_components/beestat_statistics/manifest.json", "custom_components/beestat_statistics/translations/en.json", "custom_components/beestat_statistics/icons.json", "hacs.json", "docs/beestat-api-surface.json"):
    json.loads(Path(name).read_text(encoding="utf-8"))
paths = [path for root in ("custom_components", "tests", "blueprints", ".github", "scripts", "docs") for path in Path(root).rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix in {".json", ".md", ".py", ".ps1", ".sh", ".yaml", ".yml"}]
paths.extend(Path(name) for name in ("README.md", "hacs.json"))
failures = [f"{path}:{line}" for path in paths for line, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if text.endswith((" ", "\t"))]
if failures:
    raise SystemExit("Trailing whitespace:\n" + "\n".join(failures))
PY
  '
}
run_minimum() {
  local checks='    python -m pip install "mypy==2.3.0"
    python -m pip check
    python -m mypy --strict custom_components/beestat_statistics
    python scripts/run_dependency_light_tests.py --home-assistant'
  if [[ "$mode" == affected ]]; then
    checks="$("$validation_python" "$source_root/scripts/run_dependency_light_tests.py" --plan "${affected_args[@]}" --command minimum)"
  fi
  run_python '
    python -m pip install "pytest-homeassistant-custom-component==0.13.354" || exit "$?"
    python -m pip install --upgrade -r requirements-ha-test.txt || exit "$?"
'"$checks" false
}
run_current() {
  local checks='    python -m pip check
    python scripts/run_dependency_light_tests.py --home-assistant'
  if [[ "$mode" == affected ]]; then
    checks="$("$validation_python" "$source_root/scripts/run_dependency_light_tests.py" --plan "${affected_args[@]}" --command current)"
  fi
  run_python '
    python -m pip install "pytest-homeassistant-custom-component==0.13.365" || exit "$?"
    python -m pip install --upgrade -r requirements-ha-current.txt || exit "$?"
'"$checks" false
}
run_release() {
  if [[ "$backend" == native ]]; then
    docker run --rm -v "$repo_root:/github/workspace:ro" "$hassfest_image"
  else
    podman run --rm -v "$repo_root:/github/workspace:ro" "$hassfest_image"
  fi
}
run_lane() {
  local lane="$1" lane_status
  printf '\nRunning %s validation\n' "$lane"
  # A conditional function call disables errexit inside the entire function.
  # Run each lane in an unconditional subshell so failures cannot become passes.
  set +e
  (
    set -e
    case "$lane" in
      unit) run_unit ;;
      minimum) run_minimum ;;
      current) run_current ;;
      release) run_release ;;
    esac
  )
  lane_status=$?
  set -e
  if (( lane_status == 0 )); then
    printf '%s: PASS\n' "$lane"
  else
    printf '%s: FAIL (exit %s)\n' "$lane" "$lane_status" >&2
  fi
  return "$lane_status"
}


run_parallel_lanes() {
  # Independent containers share only the immutable payload. Reap every worker
  # before returning a failure or allowing the parent to remove that payload.
  local lane lane_pid status=0
  local lane_pids=()
  for lane in "$@"; do
    run_lane "$lane" & lane_pids+=("$!")
  done
  for lane_pid in "${lane_pids[@]}"; do
    wait "$lane_pid" || status=1
  done
  return "$status"
}

run_affected() {
  local lane selected command ha_matrix_done=false
  for lane in unit minimum current release; do
    [[ -z "$affected_only" || "$lane" == "$affected_only" ]] || continue
    selected="$(printf '%s' "$affected_plan" | "$validation_python" -c 'import json,sys; print(str(json.load(sys.stdin)["jobs"][sys.argv[1]]).lower())' "$lane")"
    if [[ "$selected" != true ]]; then
      if [[ -n "$affected_only" ]]; then echo "Plan did not select $lane" >&2; return 2; fi
      continue
    fi
    # Reuse the isolated matrix only when this plan selects both HA lanes.
    # Hosted --only and native runs retain their single-lane/sequential behavior.
    if [[ "$lane" == minimum && "$backend" == container && -z "$affected_only" ]] &&
       [[ "$(printf '%s' "$affected_plan" | "$validation_python" -c 'import json,sys; print(str(json.load(sys.stdin)["jobs"]["current"]).lower())')" == true ]]; then
      run_parallel_lanes minimum current
      ha_matrix_done=true
      continue
    fi
    case "$lane" in
      unit)
        if [[ "$(printf '%s' "$affected_plan" | "$validation_python" -c 'import json,sys; print(str(json.load(sys.stdin)["workflow"]).lower())')" == true ]]; then
          run_actionlint
        fi
        command="$("$validation_python" "$source_root/scripts/run_dependency_light_tests.py" --plan "${affected_args[@]}" --command unit)"
        run_python "$command"
        ;;
      minimum) run_minimum ;;
      current)
        if [[ "$ha_matrix_done" != true ]]; then run_current; fi
        ;;
      release) run_release ;;
    esac
  done
}

if [[ "$mode" == affected ]]; then run_affected; exit 0; fi

lanes=("$mode")
if [[ "$mode" == all ]]; then
  lanes=(unit minimum current release)
fi
status=0
if [[ "$mode" == all && "$backend" == container ]]; then
  run_parallel_lanes "${lanes[@]}"
else
  for lane in "${lanes[@]}"; do
    # Calling run_lane conditionally would suppress its errexit semantics.
    run_lane "$lane" & lane_pid=$!
    wait "$lane_pid" || status=1
  done
fi
exit "$status"

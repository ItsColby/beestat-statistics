#!/usr/bin/env bash
set -euo pipefail
mode="${1:-all}"
backend="${2:-container}"
source_git_dir="${3:-}"
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$source_root"
if [[ "$backend" == container ]]; then
  repo_root="$(mktemp -d)"
  trap 'rm -rf "$repo_root"' EXIT
  source_git=(git -C "$source_root")
  if [[ -n "$source_git_dir" ]]; then
    source_git=(git --git-dir="$source_git_dir" --work-tree="$source_root")
  fi
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
  git -C "$repo_root" init -q
  git -C "$repo_root" config user.name local-validation
  git -C "$repo_root" config user.email local-validation@invalid
  git -C "$repo_root" add -A
  git -C "$repo_root" commit -qm snapshot
elif [[ "$backend" != native ]]; then
  echo "Unknown backend: $backend" >&2
  exit 2
fi

python_image="docker.io/library/python@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc"
actionlint_image="docker.io/rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
hassfest_image="ghcr.io/home-assistant/hassfest@sha256:8cd7bdb8f82430c2c13703290b1fc38dcc99957dd76ad3f230035ecee70b672d"
run_python() {
  if [[ "$backend" == native ]]; then
    (cd "$repo_root" && bash -lc "$1")
  else
    podman run --rm -e HOME=/tmp/home -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
      -e PIP_ROOT_USER_ACTION=ignore -e DEBIAN_FRONTEND=noninteractive \
      -e PYTHONPYCACHEPREFIX=/tmp/pycache -e XDG_CACHE_HOME=/tmp/cache \
      -e RUFF_CACHE_DIR=/tmp/ruff-cache -e MYPY_CACHE_DIR=/tmp/mypy-cache \
      -e 'PYTEST_ADDOPTS=-p no:cacheprovider' \
      -v "$repo_root:/workspace" -w /workspace "$python_image" bash -lc \
      'apt-get update -qq && apt-get install -y -qq --no-install-recommends git >/dev/null && eval "$1"' \
      local-validation "$1"
  fi
}
run_actionlint() {
  if [[ "$backend" == native ]]; then
    local bin
    bin="$(mktemp -d)"
    GOBIN="$bin" go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12
    "$bin/actionlint"
    rm -rf "$bin"
  else
    podman run --rm -v "$repo_root:/repo:ro" -w /repo "$actionlint_image"
  fi
}
run_unit() {
  run_actionlint
  run_python '
    python -m pip install "ruff==0.16.2" "shellcheck-py==0.11.0.1" "zizmor==1.29.0" &&
    zizmor --strict-collection --persona auditor . &&
    python -m ruff format --check custom_components tests scripts &&
    python -m ruff check custom_components tests scripts &&
    python scripts/run_dependency_light_tests.py &&
    python -m compileall -q custom_components/beestat_statistics tests scripts &&
    python scripts/check_public_safety.py &&
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
  run_python '
    python -m pip install "pytest-homeassistant-custom-component==0.13.354" &&
    python -m pip install --upgrade -r requirements-ha-test.txt &&
    python -m pip install "mypy==2.3.0" &&
    python -m pip check &&
    python -m mypy --strict custom_components/beestat_statistics &&
    pytest tests -q
  '
}
run_current() {
  run_python '
    python -m pip install "pytest-homeassistant-custom-component==0.13.355" &&
    python -m pip install --upgrade -r requirements-ha-current.txt &&
    python -m pip check &&
    pytest tests -q
  '
}
run_release() {
  if [[ "$backend" == native ]]; then
    docker run --rm -v "$repo_root:/github/workspace:ro" "$hassfest_image"
  else
    podman run --rm -v "$repo_root:/github/workspace:ro" "$hassfest_image"
  fi
}
case "$mode" in
  all) run_unit; run_minimum; run_current; run_release ;;
  unit) run_unit ;;
  minimum) run_minimum ;;
  current) run_current ;;
  release) run_release ;;
  *) echo "Unknown mode: $mode" >&2; exit 2 ;;
esac

#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || "$1" != /* ]]; then
  echo "usage: $0 /absolute/path/to/prime-rl" >&2
  exit 2
fi

prime_checkout=$1
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
orchestrator_python="$prime_checkout/.venv/bin/python"
requirements="$project_root/integrations/prime_rl/orchestrator-overrides.txt"
uv_bin=${UV_BIN:-$(command -v uv)}

if [[ ! -f "$prime_checkout/pyproject.toml" ]]; then
  echo "Prime-RL checkout is missing pyproject.toml: $prime_checkout" >&2
  exit 1
fi

(
  cd "$prime_checkout"
  "$uv_bin" sync --no-dev
)

isolated_dir=$(mktemp -d)
trap 'rmdir "$isolated_dir"' EXIT
(
  cd "$isolated_dir"
  "$uv_bin" pip install \
    --python "$orchestrator_python" \
    "torch==2.11.0" \
    --index https://download.pytorch.org/whl/cpu
  "$uv_bin" pip install \
    --python "$orchestrator_python" \
    -r "$requirements"
  "$uv_bin" pip install \
    --python "$orchestrator_python" \
    --no-deps \
    -e "$project_root" \
    -e "$project_root/environments/rca_student"
)

"$orchestrator_python" - <<'PY'
from importlib.metadata import version

expected = {
    "prime-rl": "0.9.0",
    "prometheus-client": "0.22.1",
    "rca-lab": "0.1.0",
    "rca-student": "0.1.0",
    "torch": "2.11.0+cpu",
    "transformers": "5.15.1",
}
actual = {distribution: version(distribution) for distribution in expected}
if actual != expected:
    raise SystemExit(f"Prime orchestrator runtime does not match: {actual} != {expected}")
print("Prime orchestrator runtime verified:", actual)
PY

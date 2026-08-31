#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || "$1" != /* ]]; then
  echo "usage: $0 /absolute/path/to/trainer/python" >&2
  exit 2
fi

trainer_python=$1
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
requirements="$project_root/integrations/prime_rl/trainer-overrides.txt"
uv_bin=${UV_BIN:-$(command -v uv)}

if [[ ! -x "$trainer_python" ]]; then
  echo "trainer Python is not executable: $trainer_python" >&2
  exit 1
fi

# Prime-RL declares Transformers in [tool.uv].override-dependencies. Running
# `uv pip install` anywhere below that checkout silently reapplies Prime's
# version instead of this integration's Muse-capable trainer version. Resolve
# the post-install overrides from an isolated directory with no parent uv
# workspace, then verify the installed contract explicitly.
isolated_dir=$(mktemp -d)
trap 'rmdir "$isolated_dir"' EXIT
(
  cd "$isolated_dir"
  "$uv_bin" pip install --python "$trainer_python" -r "$requirements"
)

"$trainer_python" - <<'PY'
from importlib.metadata import version

expected = {
    "prometheus-client": "0.22.1",
    "transformers": "5.15.1",
}
actual = {distribution: version(distribution) for distribution in expected}
if actual != expected:
    raise SystemExit(f"Prime trainer overrides do not match: {actual} != {expected}")
print("Prime trainer overrides verified:", actual)
PY

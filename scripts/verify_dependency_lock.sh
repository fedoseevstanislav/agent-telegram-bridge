#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

command -v uv >/dev/null 2>&1 || {
  echo "uv is required" >&2
  exit 1
}

uv lock --check
python3 scripts/generate_runtime_manifest.py --check

audit_tmp="$(mktemp -d)"
trap 'rm -rf "$audit_tmp"' EXIT
uv export \
  --frozen \
  --all-extras \
  --no-emit-project \
  --no-header \
  --format requirements.txt \
  --output-file "$audit_tmp/dev-requirements.txt" >/dev/null
uv run --frozen --python 3.12 --extra dev pip-audit \
  --require-hashes \
  --strict \
  --requirement "$audit_tmp/dev-requirements.txt"

echo "Bridge runtime manifest, dependency lock, and vulnerability audit are current."

#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python3 scripts/generate_runtime_manifest.py --check --check-host

if ! git diff --quiet -- bridge bin systemd skill security/runtime-manifest.json; then
  echo "refusing deployment: runtime files differ from the reviewed Git commit" >&2
  exit 1
fi
if ! git diff --cached --quiet -- bridge bin systemd skill security/runtime-manifest.json; then
  echo "refusing deployment: staged runtime changes are not committed" >&2
  exit 1
fi

echo "Bridge deployment snapshot matches its reviewed source manifest."

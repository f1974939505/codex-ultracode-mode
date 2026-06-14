#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: python3 not found. Set PYTHON=/path/to/python3 or install Python 3." >&2
  exit 127
fi

# Default scope is user. Pass through --scope project --project-root <path> and/or --dry-run.
"$PYTHON_BIN" "$ROOT/ultracode/scripts/install.py" --uninstall --scope user "$@"

cat <<'EOF2'

$ultracode removed (skill, mirror, agents, profile, and ultracode hook groups in hooks.json).
A timestamped hooks.json.bak-* backup was written if hooks were pruned.
EOF2

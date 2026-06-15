#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

cd "$ROOT"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: python3 not found. Set PYTHON=/path/to/python3 or install Python 3." >&2
  exit 127
fi

"$PYTHON_BIN" ultracode/scripts/uc_check_package.py --package-root "$ROOT"

"$PYTHON_BIN" ultracode/scripts/install.py \
  --package-root "$ROOT" \
  --scope user \
  --with-hooks \
  --with-agents \
  --with-profile \
  --archive-old-name \
  "$@"

cat <<'EOF2'

$ultracode installed.

Next step inside Codex:
  /hooks

Review and trust the installed hook definitions. Then invoke by adding exactly this skill mention to a normal task:
  $ultracode <your normal prompt>

EOF2

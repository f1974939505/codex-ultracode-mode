$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

# Default scope is user. Pass through --scope project --project-root <path> and/or --dry-run.
& $Python "$Root/ultracode/scripts/install.py" --uninstall --scope user @args

Write-Host ""
Write-Host "`$ultracode removed (skill, mirror, agents, profile, and ultracode hook groups in hooks.json)."
Write-Host "A timestamped hooks.json.bak-* backup was written if hooks were pruned."

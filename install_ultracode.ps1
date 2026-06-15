$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

Set-Location $Root

& $Python "ultracode/scripts/uc_check_package.py" --package-root $Root

& $Python "ultracode/scripts/install.py" `
  --package-root $Root `
  --scope user `
  --with-hooks `
  --with-agents `
  --with-profile `
  --archive-old-name `
  @args

Write-Host ""
Write-Host "`$ultracode installed."
Write-Host "Next step inside Codex: /hooks"
Write-Host "Invoke with: `$ultracode <your normal prompt>"

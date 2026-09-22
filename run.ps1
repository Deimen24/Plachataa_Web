<#
.SYNOPSIS
Start the Plachataa Web server and open the browser.

  .\run.ps1                 http://localhost:7870
  .\run.ps1 --listen        reachable from other devices on the LAN
  .\run.ps1 --port 8000
  .\run.ps1 --preload v1    load a model family at start
Set PLACHATAA_NO_BROWSER=1 to keep the browser closed.
#>
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Vpy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Vpy)) { Write-Host "run install.bat first" -ForegroundColor Red; exit 1 }

$env:PYTHONUNBUFFERED = "1"
$envfile = Join-Path $Root ".env"
if (Test-Path $envfile) {
	Get-Content $envfile | ForEach-Object {
		if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
			[Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim('"'), "Process")
		}
	}
}

$open = @("--open")
if ($env:PLACHATAA_NO_BROWSER -and $env:PLACHATAA_NO_BROWSER -ne "0") { $open = @() }

& $Vpy -m server.main @open @args
exit $LASTEXITCODE

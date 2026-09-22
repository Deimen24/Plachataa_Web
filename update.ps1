<#
.SYNOPSIS
Update Plachataa Web: pull this repo, move seed-vc to the pinned
revision, upgrade Python dependencies and re-check the NVIDIA driver.

.PARAMETER UpdateDriver
Install/upgrade the NVIDIA driver if it is missing or too old.

.PARAMETER Torch
Also reinstall torch (only needed if the pin in install.ps1 changed).

.PARAMETER DownloadModels
Pre-fetch any missing checkpoints afterwards.
#>
[CmdletBinding()]
param(
	[switch]$UpdateDriver,
	[switch]$Torch,
	[switch]$DownloadModels,
	[switch]$Cpu,
	[switch]$Yes
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Vpy = Join-Path $Root ".venv\Scripts\python.exe"
$Vendor = Join-Path $Root "vendor\seed-vc"

function Log($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "WARN: $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

if (-not (Test-Path $Vpy)) { Die "no .venv found; run install.bat first" }

function Update-Repo {
	if (-not (Test-Path (Join-Path $Root ".git"))) {
		Warn "not a git checkout; skipping self-update"
		return
	}
	Log "Updating Plachataa Web"
	$branch = (git rev-parse --abbrev-ref HEAD).Trim()
	git diff --quiet
	if ($LASTEXITCODE -ne 0) {
		Warn "local changes present; stashing them"
		git stash push -q -m "update.ps1 $(Get-Date -Format s)"
	}
	git pull --ff-only origin $branch
	if ($LASTEXITCODE -ne 0) { Die "git pull failed" }
}

function Update-SeedVC {
	$lock = @{}
	Get-Content (Join-Path $Root "seedvc.lock") | ForEach-Object {
		if ($_ -match '^\s*([A-Z_]+)=(.+)$') { $lock[$matches[1]] = $matches[2].Trim() }
	}
	Log "Updating seed-vc to $($lock.SEEDVC_COMMIT.Substring(0, 10))"
	if (-not (Test-Path (Join-Path $Vendor ".git"))) {
		git clone --quiet $lock.SEEDVC_REPO $Vendor
	}
	git -C $Vendor fetch --quiet origin
	git -C $Vendor checkout --quiet $lock.SEEDVC_COMMIT
}

function Install-Args {
	$a = @()
	if ($UpdateDriver) { $a += "-UpdateDriver" }
	if ($DownloadModels) { $a += "-DownloadModels" }
	if ($Cpu) { $a += "-Cpu" }
	if ($Yes) { $a += "-Yes" }
	return $a
}

Update-Repo
Update-SeedVC
if ($Torch) {
	Log "Reinstalling torch via install.ps1"
	& (Join-Path $Root "install.ps1") @(Install-Args)
	exit $LASTEXITCODE
}
Log "Upgrading Python dependencies"
& $Vpy -m pip install --quiet --upgrade pip
& $Vpy -m pip install --upgrade -r requirements-seedvc.txt -r requirements-web.txt
if ($LASTEXITCODE -ne 0) { Die "dependency upgrade failed" }

Log "Driver / environment check"
& $Vpy tools\check_env.py --torch --cpu-ok --ignore-python
if ($LASTEXITCODE -ne 0 -or $UpdateDriver) {
	Warn "running install.ps1 to repair / update the driver"
	& (Join-Path $Root "install.ps1") @(Install-Args)
	exit $LASTEXITCODE
}
if ($DownloadModels) { & $Vpy tools\download_models.py }
Log "Update complete."

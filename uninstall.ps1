<#
.SYNOPSIS
Remove Plachataa Web from this PC.

.DESCRIPTION
Removes the startup task and firewall rule, the Python environment
(.venv) and the seed-vc checkout (vendor).  Asks before deleting data\
(saved voices, outputs, model cache) unless -KeepData or -Purge is
given.  -RemoveDir also deletes this whole folder afterwards.
System packages (Python, Git) and the NVIDIA driver are not touched.

  uninstall.bat [-KeepData] [-Purge] [-RemoveDir] [-Yes]
#>
param(
	[switch]$KeepData,
	[switch]$Purge,
	[switch]$RemoveDir,
	[switch]$Yes
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Log($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "WARN: $msg" -ForegroundColor Yellow }

function Confirm-Step($question) {
	if ($Yes) { return $true }
	return (Read-Host "$question [y/N]") -match '^[Yy]'
}

function Remove-Service {
	$t = Get-ScheduledTask -TaskName "PlachataaWeb" -ErrorAction SilentlyContinue
	$r = Get-NetFirewallRule -DisplayName "Plachataa Web" -ErrorAction SilentlyContinue
	if ($t -or $r) {
		& (Join-Path $Root "service.ps1") uninstall
	}
}

function Stop-Running {
	Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
		Where-Object { $_.CommandLine -match "server\.main" -and $_.CommandLine -match [regex]::Escape($Root) } |
		ForEach-Object { Log "Stopping server (pid $($_.ProcessId))"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Remove-Sub($name) {
	$p = Join-Path $Root $name
	if (Test-Path $p) {
		Log "Removing $name"
		Remove-Item -Recurse -Force $p
	}
}

function Remove-Data {
	$p = Join-Path $Root "data"
	if (-not (Test-Path $p)) { return }
	$delete = $false
	if ($Purge) { $delete = $true }
	elseif (-not $KeepData) {
		$mb = [math]::Round((Get-ChildItem $p -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
		$delete = Confirm-Step "Delete data\ ($mb MB: saved voices, outputs, model cache)?"
	}
	if ($delete) { Remove-Sub "data"; $script:KeepData = $false } else { Log "Keeping data\"; $script:KeepData = $true }
}

function Remove-Self {
	if (-not $RemoveDir) { return }
	if ($script:KeepData -and (Test-Path (Join-Path $Root "data"))) {
		Warn "-RemoveDir ignored because data\ is kept"
		return
	}
	if (-not (Confirm-Step "Delete the folder $Root entirely?")) { return }
	Log "The folder will be deleted after this window closes."
	Set-Location $env:TEMP
	# A script cannot delete the folder it runs from; hand off to cmd.
	Start-Process cmd -ArgumentList "/c", "timeout /t 3 /nobreak >nul & rmdir /s /q `"$Root`"" -WindowStyle Hidden
}

Log "Uninstalling Plachataa Web from $Root"
Remove-Service
Stop-Running
Remove-Sub ".venv"
Remove-Sub "vendor"
Remove-Data
Get-ChildItem $Root -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
	Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Remove-Self
Log "Done. Python, Git and the NVIDIA driver were left in place."

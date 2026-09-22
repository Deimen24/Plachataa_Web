<#
.SYNOPSIS
Manage the Plachataa Web startup task on Windows.

.DESCRIPTION
Registers a Scheduled Task that starts the server at boot (before any
user logs in), restarts it on failure, listens on all interfaces for a
reverse proxy on another machine and opens the port in Windows
Firewall.  Needs administrator rights; the script elevates itself.

  .\service.ps1 install
  .\service.ps1 uninstall
  .\service.ps1 start | stop | restart | status | logs
#>
param(
	[Parameter(Position = 0)]
	[ValidateSet("install", "uninstall", "start", "stop", "restart", "status", "logs")]
	[string]$Command = "status"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "PlachataaWeb"
$RuleName = "Plachataa Web"
$Vpy = Join-Path $Root ".venv\Scripts\python.exe"
$LogFile = Join-Path $Root "data\logs\server.log"

function Log($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "WARN: $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

function Test-Admin {
	$id = [Security.Principal.WindowsIdentity]::GetCurrent()
	return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
		[Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Ensure-Admin {
	if (Test-Admin) { return }
	Log "Requesting administrator rights"
	$argv = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" $Command"
	$p = Start-Process powershell -Verb RunAs -ArgumentList $argv -Wait -PassThru
	exit $p.ExitCode
}

function Get-Port {
	$envfile = Join-Path $Root ".env"
	if (Test-Path $envfile) {
		$m = Get-Content $envfile | Select-String '^PLACHATAA_PORT=(\d+)' | Select-Object -Last 1
		if ($m) { return [int]$m.Matches[0].Groups[1].Value }
	}
	return 7870
}

function Ensure-EnvFile {
	$envfile = Join-Path $Root ".env"
	if (-not (Test-Path $envfile)) {
		Log "Creating .env from .env.example"
		Copy-Item (Join-Path $Root ".env.example") $envfile
	}
}

function Install-Task {
	if (-not (Test-Path $Vpy)) { Die "no .venv; run install.bat first" }
	Ensure-EnvFile
	New-Item -ItemType Directory -Force (Split-Path $LogFile) | Out-Null
	Log "Registering scheduled task $TaskName (at startup, as SYSTEM)"
	$action = New-ScheduledTaskAction -Execute $Vpy `
		-Argument "-m server.main --listen --log-file `"$LogFile`"" `
		-WorkingDirectory $Root
	$trigger = New-ScheduledTaskTrigger -AtStartup
	$settings = New-ScheduledTaskSettingsSet `
		-ExecutionTimeLimit ([TimeSpan]::Zero) `
		-RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
		-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
		-MultipleInstances IgnoreNew -StartWhenAvailable
	$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" `
		-LogonType ServiceAccount -RunLevel Highest
	Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
		-Settings $settings -Principal $principal -Force | Out-Null
	$port = Get-Port
	Log "Opening TCP port $port in Windows Firewall"
	Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
	New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP `
		-LocalPort $port -Action Allow -Profile Any | Out-Null
	Start-ScheduledTask -TaskName $TaskName
	Log "Started. The server listens on 0.0.0.0:$port for your reverse proxy."
	Log "Logs: $LogFile   (.\service.ps1 logs)"
}

function Uninstall-Task {
	$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
	if ($t) {
		Log "Removing scheduled task $TaskName"
		Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
		Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
	} else {
		Log "Task not installed"
	}
	Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}

function Show-Status {
	$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
	if (-not $t) { Write-Host "not installed"; return }
	$i = $t | Get-ScheduledTaskInfo
	Write-Host "task:      $($t.State)"
	Write-Host "last run:  $($i.LastRunTime)  result: $($i.LastTaskResult)"
	Write-Host "next run:  at startup"
	try {
		$port = Get-Port
		$r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/api/health" -TimeoutSec 3
		Write-Host "health:    $($r.Content)"
	} catch { Write-Host "health:    not responding on port $(Get-Port)" }
}

switch ($Command) {
	"install"   { Ensure-Admin; Install-Task }
	"uninstall" { Ensure-Admin; Uninstall-Task }
	"start"     { Ensure-Admin; Start-ScheduledTask -TaskName $TaskName; Show-Status }
	"stop"      { Ensure-Admin; Stop-ScheduledTask -TaskName $TaskName; Show-Status }
	"restart"   { Ensure-Admin; Stop-ScheduledTask -TaskName $TaskName; Start-Sleep 2; Start-ScheduledTask -TaskName $TaskName; Show-Status }
	"status"    { Show-Status }
	"logs"      { if (Test-Path $LogFile) { Get-Content $LogFile -Tail 100 -Wait } else { Write-Host "no log file yet" } }
}

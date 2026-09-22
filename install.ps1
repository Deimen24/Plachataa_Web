<#
.SYNOPSIS
Plachataa Web installer for Windows 10/11 with an NVIDIA RTX GPU.

.DESCRIPTION
Idempotent: re-run any time.  Installs Python 3.10 and Git through
winget if missing, checks the NVIDIA driver against the CUDA 12.1
minimum (528.33) and can update it, vendors seed-vc at the pinned
revision, creates .venv and installs torch (CUDA or CPU) plus all
dependencies.

.PARAMETER Cpu
Force the CPU build of torch.

.PARAMETER DownloadModels
Also pre-fetch every checkpoint (~6 GB).

.PARAMETER UpdateDriver
Install/upgrade the NVIDIA driver when missing or too old.

.PARAMETER Yes
Never prompt.

.PARAMETER NoService
Do not register the startup task that runs the server at boot.

.PARAMETER ProxyIp
Reverse-proxy address whose X-Forwarded-* headers are trusted
(default: any).
#>
[CmdletBinding()]
param(
	[switch]$Cpu,
	[switch]$Cuda,
	[switch]$DownloadModels,
	[switch]$UpdateDriver,
	[switch]$Yes,
	[switch]$NoService,
	[string]$ProxyIp = "*"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Venv = Join-Path $Root ".venv"
$Vendor = Join-Path $Root "vendor\seed-vc"
$TorchPkgs = @("torch==2.4.0", "torchvision==0.19.0", "torchaudio==2.4.0")
$script:Device = "cpu"

function Log($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "WARN: $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

function Confirm-Step($question) {
	if ($Yes) { return $true }
	$ans = Read-Host "$question [y/N]"
	return $ans -match '^[Yy]'
}

function Read-Lock {
	$lock = @{}
	Get-Content (Join-Path $Root "seedvc.lock") | ForEach-Object {
		if ($_ -match '^\s*([A-Z_]+)=(.+)$') { $lock[$matches[1]] = $matches[2].Trim() }
	}
	return $lock
}

function Refresh-Path {
	$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
		[Environment]::GetEnvironmentVariable("Path", "User")
}

function Have-Winget {
	return [bool](Get-Command winget -ErrorAction SilentlyContinue)
}

function Winget-Install($id) {
	if (-not (Have-Winget)) { Die "winget not available; install $id manually" }
	Log "winget install $id"
	winget install --id $id -e --accept-source-agreements --accept-package-agreements --silent
	Refresh-Path
}

# ---- python -------------------------------------------------------------

# A python "command" is e.g. "py -3.10": an executable plus fixed
# leading arguments.  Invoke-Py runs it with extra arguments appended.
function Invoke-Py($cmd, [string[]]$rest) {
	$parts = $cmd.Split(" ")
	$exe = $parts[0]
	$pre = @($parts | Select-Object -Skip 1)
	& $exe @pre @rest
}

function Find-Python {
	foreach ($cmd in @("py -3.10", "py -3.11", "python3.10", "python3.11", "python")) {
		$exe = Get-Command $cmd.Split(" ")[0] -ErrorAction SilentlyContinue
		if (-not $exe) { continue }
		try {
			$out = Invoke-Py $cmd @("-c", "import sys; print(sys.version_info[0], sys.version_info[1])") 2>$null
			if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
			$v = "$out".Trim().Split(" ")
			if ([int]$v[0] -eq 3 -and [int]$v[1] -ge 10 -and [int]$v[1] -le 11) { return $cmd }
		} catch { }
	}
	return $null
}

function Ensure-Python {
	$py = Find-Python
	if ($py) { return $py }
	Warn "Python 3.10/3.11 not found."
	if ($Yes -or (Confirm-Step "Install Python 3.10 with winget?")) {
		Winget-Install "Python.Python.3.10"
		$py = Find-Python
		if ($py) { return $py }
	}
	Die "install Python 3.10 from https://www.python.org/downloads/release/python-31011/ (tick 'Add to PATH') and re-run"
}

function Ensure-Git {
	if (Get-Command git -ErrorAction SilentlyContinue) { return }
	Warn "Git not found."
	if ($Yes -or (Confirm-Step "Install Git with winget?")) {
		Winget-Install "Git.Git"
		if (Get-Command git -ErrorAction SilentlyContinue) { return }
	}
	Die "install Git from https://git-scm.com/download/win and re-run"
}

# ---- seed-vc ------------------------------------------------------------

function Vendor-SeedVC {
	$lock = Read-Lock
	Log "Fetching seed-vc @ $($lock.SEEDVC_COMMIT.Substring(0, 10))"
	New-Item -ItemType Directory -Force (Join-Path $Root "vendor") | Out-Null
	if (-not (Test-Path (Join-Path $Vendor ".git"))) {
		git clone --quiet $lock.SEEDVC_REPO $Vendor
	}
	git -C $Vendor fetch --quiet origin
	git -C $Vendor checkout --quiet $lock.SEEDVC_COMMIT
}

# ---- NVIDIA driver ------------------------------------------------------

function Has-NvidiaGpu {
	try {
		$gpus = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match "NVIDIA" }
		return [bool]$gpus
	} catch { return $false }
}

function Install-NvidiaDriver {
	Log "Updating the NVIDIA driver"
	# The GeForce Game Ready driver is not packaged on winget as a plain
	# driver; the NVIDIA App installs and updates it.  Try that first.
	$done = $false
	if (Have-Winget) {
		try {
			winget install --id Nvidia.GeForceExperience -e --accept-source-agreements --accept-package-agreements
			$done = ($LASTEXITCODE -eq 0)
		} catch { }
	}
	if (-not $done) {
		Warn "Automatic install failed. Opening the NVIDIA driver download page."
		Start-Process "https://www.nvidia.com/Download/index.aspx"
	} else {
		Warn "NVIDIA App installed. Open it, install the latest Game Ready driver."
	}
	Warn "After the driver installs, REBOOT and run install.bat again."
	exit 0
}

function Decide-Device($py) {
	Invoke-Py $py @("tools\check_env.py", "--ignore-python")
	$rc = $LASTEXITCODE
	if ($Cpu) { $script:Device = "cpu"; Log "Device forced to cpu"; return }
	if ($Cuda) { $script:Device = "cuda"; Log "Device forced to cuda"; return }
	switch ($rc) {
		0 { $script:Device = "cuda" }
		{ $_ -in 10, 11 } {
			if (Has-NvidiaGpu) {
				if ($rc -eq 10) { Warn "An NVIDIA GPU is present but no driver (nvidia-smi) was found." }
				else { Warn "The NVIDIA driver is too old for CUDA 12.1." }
				if ($UpdateDriver -or (Confirm-Step "Install/upgrade the NVIDIA driver now?")) {
					Install-NvidiaDriver
				}
				Warn "Continuing with the CPU build; re-run with -UpdateDriver later."
			}
			$script:Device = "cpu"
		}
		default { Die "environment check failed (exit $rc)" }
	}
}

# ---- python environment -------------------------------------------------

function Make-Venv($py) {
	$vpy = Join-Path $Venv "Scripts\python.exe"
	if (-not (Test-Path $vpy)) {
		Log "Creating virtualenv with $py"
		Invoke-Py $py @("-m", "venv", $Venv)
		if ($LASTEXITCODE -ne 0) { Die "venv creation failed" }
	}
	& $vpy -m pip install --quiet --upgrade pip wheel setuptools
	return $vpy
}

function Install-Torch($vpy) {
	$index = "https://download.pytorch.org/whl/cpu"
	if ($script:Device -eq "cuda") { $index = "https://download.pytorch.org/whl/cu121" }
	Log "Installing torch ($script:Device) from $index"
	& $vpy -m pip install @TorchPkgs --index-url $index
	if ($LASTEXITCODE -ne 0) { Die "torch install failed" }
}

function Install-Deps($vpy) {
	Log "Installing seed-vc and web server dependencies"
	& $vpy -m pip install -r requirements-seedvc.txt -r requirements-web.txt
	if ($LASTEXITCODE -ne 0) { Die "dependency install failed" }
}

function Verify($vpy) {
	Log "Verifying"
	& $vpy tools\check_env.py --torch --cpu-ok --ignore-python
	if ($LASTEXITCODE -eq 12) {
		Die "torch cannot see the GPU although the driver looks fine; reboot, or re-run with -Cpu"
	}
	& $vpy -c "import fastapi, uvicorn, librosa, transformers; print('python deps ok')"
	if ($LASTEXITCODE -ne 0) { Die "verification failed" }
}

function Write-Env {
	$envfile = Join-Path $Root ".env"
	if (Test-Path $envfile) { return }
	Log "Creating .env"
	Copy-Item (Join-Path $Root ".env.example") $envfile
	Add-Content $envfile "`nPLACHATAA_FORWARDED_ALLOW_IPS=$ProxyIp"
}

function Main {
	Ensure-Git
	$py = Ensure-Python
	Log "Using $py"
	Vendor-SeedVC
	Decide-Device $py
	$vpy = Make-Venv $py
	Install-Torch $vpy
	Install-Deps $vpy
	Verify $vpy
	if ($DownloadModels) {
		Log "Pre-downloading models (several GB, resumable)"
		& $vpy tools\download_models.py
	}
	Write-Env
	if (-not $NoService) {
		& (Join-Path $Root "service.ps1") install
	}
	Write-Host ""
	if ($NoService) {
		Log "Done. Double-click run.bat (or .\run.ps1) and open http://localhost:7870"
	} else {
		Log "Done. The server runs at boot; manage it with service.bat (status|logs|restart)."
	}
	if ($script:Device -eq "cpu") { Warn "Running on CPU: conversions take minutes, not seconds." }
}

Main

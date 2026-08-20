<#
.SYNOPSIS
    Installs pCloud Sync and registers it as a Windows service.

.DESCRIPTION
    Checks the prerequisites, installs the Python dependencies, then
    registers the application as an NSSM service with log rotation — so it
    starts with the machine and never lets a log file balloon.

    Run from an ADMINISTRATOR PowerShell console.

.EXAMPLE
    .\install.ps1
    Full installation with the service.

.EXAMPLE
    .\install.ps1 -NoService
    Installs the dependencies only. Manual start via run.py.

.EXAMPLE
    .\install.ps1 -Uninstall
    Removes the service. Touches neither the configuration nor the history.
#>

[CmdletBinding()]
param(
    [string]$Service = "PCloudSync",
    [string]$Nssm    = "C:\tools\nssm\win64\nssm.exe",
    [string]$DataDir = "C:\ProgramData\PCloudSync",
    [switch]$NoService,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

function Step($t) { Write-Host "`n$t" -ForegroundColor Cyan }
function OK($t)   { Write-Host "  $t" -ForegroundColor Green }
function Info($t) { Write-Host "  $t" -ForegroundColor DarkGray }
function Warn($t) { Write-Host "  $t" -ForegroundColor Yellow }
function Fail($t) { Write-Host "`n$t`n" -ForegroundColor Red; exit 1 }

# --- administrator rights ------------------------------------------------------

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin -and -not $NoService) {
    Fail "This console does not have administrator rights.`nClose it, then reopen PowerShell with 'Run as administrator'."
}

# --- uninstall -------------------------------------------------------------------

if ($Uninstall) {
    Step "Removing the service"
    if (Get-Service $Service -ErrorAction SilentlyContinue) {
        & $Nssm stop   $Service 2>&1 | Out-Null
        & $Nssm remove $Service confirm 2>&1 | Out-Null
        OK "Service $Service removed."
    } else {
        Info "No service $Service is registered."
    }
    Info "Configuration and history are kept in $DataDir"
    Write-Host ""
    exit 0
}

Write-Host ""
Write-Host "  pCloud Sync — installation" -ForegroundColor White
Write-Host "  ==========================" -ForegroundColor DarkGray

# --- prerequisites ----------------------------------------------------------------

Step "Checking prerequisites"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Fail "Python was not found.`nInstall it with:  winget install Python.Python.3.12`nThen close and reopen PowerShell."
}
$vPy = (& python --version 2>&1) -replace "Python ", ""
if ([version]($vPy -split "\+")[0] -lt [version]"3.10") {
    Fail "Python $vPy is too old. Version 3.10 or newer is required."
}
OK "Python $vPy — $($python.Source)"

$rclone = Get-Command rclone -ErrorAction SilentlyContinue
if (-not $rclone) {
    Fail "rclone was not found.`nInstall it with:  winget install Rclone.Rclone`nThen close and reopen PowerShell."
}
$vRc = ((& rclone version) -split "`n")[0] -replace "rclone ", ""
OK "rclone $vRc — $($rclone.Source)"

$remotes = (& rclone listremotes) -replace ":$", ""
if (-not $remotes) {
    Fail "No rclone remote is configured.`nConnect pCloud with:  rclone config`n(European account: pick eapi.pcloud.com)"
}
OK "Available remotes: $($remotes -join ', ')"

if (-not $NoService) {
    if (-not (Test-Path $Nssm)) {
        Warn "NSSM not found at $Nssm"
        Warn "Point to it with -Nssm, or install without the service with -NoService"
        Fail "Installation aborted."
    }
    OK "NSSM — $Nssm"
}

# --- Python dependencies -------------------------------------------------------------

Step "Installing Python dependencies"
$req = Join-Path $Root "requirements.txt"
& python -m pip install --quiet --disable-pip-version-check -r $req
if ($LASTEXITCODE -ne 0) { Fail "Installing the dependencies failed." }
OK "fastapi, uvicorn, httpx, PyYAML"

# --- data folder ---------------------------------------------------------------------

Step "Preparing the data folder"
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $DataDir "logs") -Force | Out-Null
OK $DataDir

# --- configuration check --------------------------------------------------------------

Step "Checking the configuration"
$cfg = Join-Path $Root "config.yaml"
if (-not (Test-Path $cfg)) { Fail "config.yaml is missing from $Root" }

& python (Join-Path $Root "run.py") --config $cfg --check
$checkCode = $LASTEXITCODE
if ($checkCode -eq 2 -or $checkCode -eq 3) {
    Fail "Fix config.yaml, then run this script again."
}
if ($checkCode -eq 1) {
    Warn "Warnings were reported (see above)."
    Warn "The installation continues: an unplugged drive can explain a missing path."
}
Info "Backups are then created from the web interface."

# --- service --------------------------------------------------------------------------

if ($NoService) {
    Step "Done"
    Info "Manual start:  python `"$Root\run.py`""
    Write-Host ""
    exit 0
}

Step "Registering the $Service service"

if (Get-Service $Service -ErrorAction SilentlyContinue) {
    Info "Existing service, replacing it."
    & $Nssm stop   $Service 2>&1 | Out-Null
    & $Nssm remove $Service confirm 2>&1 | Out-Null
    Start-Sleep -Seconds 2
}

$runPy  = Join-Path $Root "run.py"
$logOut = Join-Path $DataDir "logs\service-out.log"
$logErr = Join-Path $DataDir "logs\service-err.log"

& $Nssm install $Service $python.Source "`"$runPy`" --config `"$cfg`" --no-browser" | Out-Null
& $Nssm set $Service AppDirectory       $Root              | Out-Null
& $Nssm set $Service DisplayName        "pCloud Sync"      | Out-Null
& $Nssm set $Service Description        "Backup to pCloud that recognizes moved files" | Out-Null
& $Nssm set $Service Start              SERVICE_AUTO_START | Out-Null
& $Nssm set $Service AppStdout          $logOut            | Out-Null
& $Nssm set $Service AppStderr          $logErr            | Out-Null

# Rotation: without it a service log ends up weighing gigabytes
& $Nssm set $Service AppRotateFiles     1        | Out-Null
& $Nssm set $Service AppRotateOnline    1        | Out-Null
& $Nssm set $Service AppRotateSeconds   86400    | Out-Null
& $Nssm set $Service AppRotateBytes     10485760 | Out-Null

# Automatic restart, with a delay to avoid fast crash loops
& $Nssm set $Service AppExit Default Restart | Out-Null
& $Nssm set $Service AppRestartDelay 5000    | Out-Null

OK "Service registered, log rotation at 10 MB or 24 h."

Step "Starting"
& $Nssm start $Service 2>&1 | Out-Null
Start-Sleep -Seconds 4

$state = (Get-Service $Service).Status
if ($state -eq "Running") {
    OK "Service started."
} else {
    Warn "Service state: $state"
    Warn "Check $logErr"
}

# --- summary ---------------------------------------------------------------------------

$port = (Select-String -Path $cfg -Pattern "^\s*port:\s*(\d+)" | Select-Object -First 1).Matches[0].Groups[1].Value
if (-not $port) { $port = "8477" }

Write-Host ""
Write-Host "  ------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "  Interface   " -NoNewline -ForegroundColor DarkGray
Write-Host "http://127.0.0.1:$port" -ForegroundColor White
Write-Host "  Data        " -NoNewline -ForegroundColor DarkGray
Write-Host $DataDir -ForegroundColor White
Write-Host "  ------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Useful commands" -ForegroundColor DarkGray
Write-Host "    nssm restart $Service      after changing config.yaml"
Write-Host "    nssm stop $Service"
Write-Host "    .\install.ps1 -Uninstall"
Write-Host ""
Write-Host "  Open the interface to create your first backup." -ForegroundColor DarkGray
Write-Host "  Pause the pCloud client's own backup before any transfer." -ForegroundColor Yellow
Write-Host ""

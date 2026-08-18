[CmdletBinding()]
param(
    [switch]$SkipAdb,
    [switch]$NoStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Find-Executable {
    param([string]$Name, [string[]]$Candidates)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Install-WinGetPackage {
    param([string]$Id, [string]$Label)
    $winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "WinGet is required to install $Label. Install App Installer from the Microsoft Store and run this script again."
    }
    Write-Host "Installing $Label..." -ForegroundColor Cyan
    & $winget.Source install --id $Id -e --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet could not install $Label (exit code $LASTEXITCODE)."
    }
    Refresh-ProcessPath
}

Write-Host "TheDoPixel Windows setup" -ForegroundColor Cyan

$uvCandidates = @(
    (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
    (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe")
)
$uv = Find-Executable "uv.exe" $uvCandidates
if (-not $uv) {
    Install-WinGetPackage "astral-sh.uv" "uv"
    $uv = Find-Executable "uv.exe" $uvCandidates
}
if (-not $uv) { throw "uv was installed but uv.exe could not be found. Open a new PowerShell window and rerun this script." }

$npmCandidates = @(
    (Join-Path $env:ProgramFiles "nodejs\npm.cmd"),
    (Join-Path $env:LOCALAPPDATA "Programs\nodejs\npm.cmd")
)
$npm = Find-Executable "npm.cmd" $npmCandidates
if (-not $npm) {
    Install-WinGetPackage "OpenJS.NodeJS.LTS" "Node.js LTS"
    $npm = Find-Executable "npm.cmd" $npmCandidates
}
if (-not $npm) { throw "Node.js was installed but npm.cmd could not be found. Open a new PowerShell window and rerun this script." }

if (-not $SkipAdb) {
    $adbCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\adb.exe")
    )
    $adb = Find-Executable "adb.exe" $adbCandidates
    if (-not $adb) {
        Install-WinGetPackage "Google.PlatformTools" "Google Android Platform Tools"
        $adb = Find-Executable "adb.exe" $adbCandidates
    }
    if (-not $adb) { throw "Android Platform Tools were installed but adb.exe could not be found. Open a new PowerShell window and rerun this script." }
}

Write-Host "Creating the Python environment..." -ForegroundColor Cyan
& $uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw "Python environment setup failed with exit code $LASTEXITCODE" }

Write-Host "Installing and building the dashboard..." -ForegroundColor Cyan
& $npm --prefix (Join-Path $ProjectDir "frontend") ci
if ($LASTEXITCODE -ne 0) { throw "npm install failed with exit code $LASTEXITCODE" }
& $npm --prefix (Join-Path $ProjectDir "frontend") run build
if ($LASTEXITCODE -ne 0) { throw "Dashboard build failed with exit code $LASTEXITCODE" }

$environmentFile = Join-Path $ProjectDir ".env"
if (-not (Test-Path -LiteralPath $environmentFile)) {
    Copy-Item (Join-Path $ProjectDir ".env.example") $environmentFile
}
if (-not $SkipAdb -and $adb) {
    $adbSetting = $adb.Replace("\", "/")
    $environment = Get-Content -LiteralPath $environmentFile -Raw
    if ($environment -match "(?m)^PIXEL_RELAY_ADB_PATH=.*$") {
        $environment = $environment -replace "(?m)^PIXEL_RELAY_ADB_PATH=.*$", "PIXEL_RELAY_ADB_PATH=$adbSetting"
    } else {
        $environment = "$environment`r`nPIXEL_RELAY_ADB_PATH=$adbSetting`r`n"
    }
    Set-Content -LiteralPath $environmentFile -Value $environment -Encoding UTF8
}

Write-Host "TheDoPixel installation is complete." -ForegroundColor Green
Write-Host "Start it later with: .\pixel-relay.cmd"
if (-not $NoStart) {
    & (Join-Path $ProjectDir "pixel-relay.ps1")
}

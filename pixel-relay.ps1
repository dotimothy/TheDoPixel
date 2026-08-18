[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RelayArguments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Executable {
    param([string]$Name, [string[]]$Candidates)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

$uv = Find-Executable "uv.exe" @(
    (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
    (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe")
)
if (-not $uv) {
    throw "uv is not installed. Run install-windows.ps1 first."
}

if (-not $RelayArguments -or $RelayArguments.Count -eq 0) {
    $RelayArguments = @("serve")
}

Set-Location $ProjectDir
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir "frontend\dist\index.html"))) {
    $npm = Find-Executable "npm.cmd" @(
        (Join-Path $env:ProgramFiles "nodejs\npm.cmd"),
        (Join-Path $env:LOCALAPPDATA "Programs\nodejs\npm.cmd")
    )
    if (-not $npm) {
        throw "Node.js/npm is not installed. Run install-windows.ps1 first."
    }
    & $npm --prefix (Join-Path $ProjectDir "frontend") ci
    if ($LASTEXITCODE -ne 0) { throw "npm install failed with exit code $LASTEXITCODE" }
    & $npm --prefix (Join-Path $ProjectDir "frontend") run build
    if ($LASTEXITCODE -ne 0) { throw "Dashboard build failed with exit code $LASTEXITCODE" }
}

& $uv run --project $ProjectDir pixel-relay-cli @RelayArguments
exit $LASTEXITCODE

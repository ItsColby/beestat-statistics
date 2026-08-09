[CmdletBinding()]
param(
    [ValidateSet("all", "unit", "minimum", "current", "release")]
    [string]$Mode = "all"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$wslInput = $repoRoot -replace "\\", "/"
$linuxRoot = (& wsl.exe -d Ubuntu-24.04 -- wslpath -a -u $wslInput).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxRoot) { throw "Could not map the repository into Ubuntu-24.04." }
& wsl.exe -d Ubuntu-24.04 -- bash "$linuxRoot/scripts/verify-release-local.sh" $Mode
if ($LASTEXITCODE -ne 0) { throw "Local release validation failed with exit code $LASTEXITCODE." }

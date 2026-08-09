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

$gitDir = (& git -C $repoRoot rev-parse --path-format=absolute --git-dir).Trim()
if ($LASTEXITCODE -ne 0 -or -not $gitDir) { throw "Could not resolve the repository Git directory." }
$wslGitInput = $gitDir -replace "\\", "/"
$linuxGitDir = (& wsl.exe -d Ubuntu-24.04 -- wslpath -a -u $wslGitInput).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxGitDir) { throw "Could not map the repository Git directory into Ubuntu-24.04." }

& wsl.exe -d Ubuntu-24.04 -- bash "$linuxRoot/scripts/verify-release-local.sh" $Mode container $linuxGitDir
if ($LASTEXITCODE -ne 0) { throw "Local release validation failed with exit code $LASTEXITCODE." }

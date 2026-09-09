[CmdletBinding()]
param(
    [ValidateSet("all", "unit", "minimum", "current", "release")]
    [string]$Mode = "all"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$wslInput = $repoRoot -replace "\\", "/"
$linuxRootOutput = & wsl.exe -d Ubuntu-24.04 -- wslpath -a -u $wslInput
if ($LASTEXITCODE -ne 0 -or -not $linuxRootOutput) { throw "Could not map the repository into Ubuntu-24.04." }
$linuxRoot = ($linuxRootOutput -join "`n").Trim()

$gitDirOutput = & git -C $repoRoot rev-parse --path-format=absolute --git-dir
if ($LASTEXITCODE -ne 0 -or -not $gitDirOutput) { throw "Could not resolve the repository Git directory." }
$gitDir = ($gitDirOutput -join "`n").Trim()
$wslGitInput = $gitDir -replace "\\", "/"
$linuxGitDirOutput = & wsl.exe -d Ubuntu-24.04 -- wslpath -a -u $wslGitInput
if ($LASTEXITCODE -ne 0 -or -not $linuxGitDirOutput) { throw "Could not map the repository Git directory into Ubuntu-24.04." }
$linuxGitDir = ($linuxGitDirOutput -join "`n").Trim()

& wsl.exe -d Ubuntu-24.04 -- bash "$linuxRoot/scripts/verify-release-local.sh" $Mode container $linuxGitDir
if ($LASTEXITCODE -ne 0) { throw "Local release validation failed with exit code $LASTEXITCODE." }

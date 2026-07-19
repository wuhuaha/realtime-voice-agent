[CmdletBinding()]
param(
    [switch]$SkipFirmware
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

Push-Location $Root
try {
    uv sync --locked --dev
    if (Test-Path -LiteralPath (Join-Path $Root 'server/pyproject.toml')) {
        uv sync --directory (Join-Path $Root 'server') --locked --dev
    }
    if (-not $SkipFirmware) {
        & (Join-Path $Root 'firmware/tools/materialize.ps1')
    }
} finally {
    Pop-Location
}

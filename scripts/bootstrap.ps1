[CmdletBinding()]
param(
    [switch]$SkipFirmware
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

Push-Location $Root
try {
    uv sync --locked --dev
    if ($LASTEXITCODE -ne 0) { throw 'Root dependency sync failed.' }
    if (Test-Path -LiteralPath (Join-Path $Root 'server/pyproject.toml')) {
        uv sync --directory (Join-Path $Root 'server') --locked --all-packages --dev
        if ($LASTEXITCODE -ne 0) { throw 'Server dependency sync failed.' }
    }
    if (-not $SkipFirmware) {
        & (Join-Path $Root 'firmware/reference/xiaozhi-overlay/scripts/materialize-upstream.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Firmware materialization failed.' }
    }
} finally {
    Pop-Location
}

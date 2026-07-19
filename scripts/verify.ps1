[CmdletBinding()]
param(
    [switch]$BuildFirmware
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

Push-Location $Root
try {
    uv run ruff check scripts tests
    uv run pytest
    uv run python scripts/verify_repository.py
    if (Test-Path -LiteralPath (Join-Path $Root 'server/pyproject.toml')) {
        uv run --directory (Join-Path $Root 'server') ruff check .
        uv run --directory (Join-Path $Root 'server') pytest
    }
    if (Test-Path -LiteralPath (Join-Path $Root 'firmware/reference/xiaozhi-overlay/scripts/verify-source-contract.ps1')) {
        & (Join-Path $Root 'firmware/reference/xiaozhi-overlay/scripts/verify-source-contract.ps1')
    }
    if ($BuildFirmware) {
        & (Join-Path $Root 'firmware/reference/xiaozhi-overlay/scripts/build.ps1') -Clean
    }
} finally {
    Pop-Location
}

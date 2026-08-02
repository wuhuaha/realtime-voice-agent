[CmdletBinding()]
param(
    [switch]$BuildFirmware
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

Push-Location $Root
try {
    uv run ruff check scripts tests
    if ($LASTEXITCODE -ne 0) { throw 'Root Ruff validation failed.' }
    uv run pytest
    if ($LASTEXITCODE -ne 0) { throw 'Root tests failed.' }
    uv run python scripts/verify_repository.py
    if ($LASTEXITCODE -ne 0) { throw 'Repository contract validation failed.' }
    uv run python scripts/check_secrets.py
    if ($LASTEXITCODE -ne 0) { throw 'Secret scan failed.' }
    if (Test-Path -LiteralPath (Join-Path $Root 'server/pyproject.toml')) {
        uv run --directory (Join-Path $Root 'server') ruff check .
        if ($LASTEXITCODE -ne 0) { throw 'Server Ruff validation failed.' }
        uv run --directory (Join-Path $Root 'server') pytest
        if ($LASTEXITCODE -ne 0) { throw 'Server tests failed.' }
    }
    if (Test-Path -LiteralPath (Join-Path $Root 'clients/desktop_reference/pyproject.toml')) {
        uv run --directory (Join-Path $Root 'clients/desktop_reference') ruff check src tests
        if ($LASTEXITCODE -ne 0) { throw 'Desktop reference Ruff validation failed.' }
        uv run --directory (Join-Path $Root 'clients/desktop_reference') pytest -m 'not e2e_host'
        if ($LASTEXITCODE -ne 0) { throw 'Desktop reference tests failed.' }
    }
    if ($BuildFirmware) {
        & (Join-Path $Root 'scripts/build-firmware.ps1') `
            -BuildDir 'firmware/apps/voice_terminal/build-verify'
        if ($LASTEXITCODE -ne 0) { throw 'Native firmware build/size check failed.' }
    }
} finally {
    Pop-Location
}

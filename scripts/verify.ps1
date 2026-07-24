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
    uv run python scripts/check_secrets.py
    if (Test-Path -LiteralPath (Join-Path $Root 'server/pyproject.toml')) {
        uv run --directory (Join-Path $Root 'server') ruff check .
        uv run --directory (Join-Path $Root 'server') pytest
    }
    if ($BuildFirmware) {
        Push-Location (Join-Path $Root 'firmware/apps/voice_terminal')
        try {
            idf.py -B build-verify build
            if ($LASTEXITCODE -ne 0) { throw 'Native firmware build failed.' }
            idf.py -B build-verify size
            if ($LASTEXITCODE -ne 0) { throw 'Native firmware size check failed.' }
        } finally {
            Pop-Location
        }
    }
} finally {
    Pop-Location
}

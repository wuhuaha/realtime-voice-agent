[CmdletBinding()]
param()

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
    if (Test-Path -LiteralPath (Join-Path $Root 'clients/desktop_reference/pyproject.toml')) {
        uv sync --directory (Join-Path $Root 'clients/desktop_reference') --locked --extra test
        if ($LASTEXITCODE -ne 0) { throw 'Desktop reference client dependency sync failed.' }
    }
} finally {
    Pop-Location
}

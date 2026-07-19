[CmdletBinding()]
param(
    [int]$WorkerCount = 2
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root '.runtime/local'
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root '.env'))) {
    throw 'Create ignored .env from .env.example before starting local services.'
}

& (Join-Path $Root 'server/scripts/run-local.ps1') -WorkerCount $WorkerCount -RuntimeDirectory $Runtime

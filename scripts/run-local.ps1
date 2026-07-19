[CmdletBinding()]
param(
    [ValidateRange(1, 32)]
    [int]$WorkerCount = 1,
    [ValidateRange(0, 65535)]
    [int]$DirectorPort = 0,
    [ValidateRange(0, 65535)]
    [int]$WorkerBasePort = 0,
    [ValidateRange(0, 65535)]
    [int]$UdpBasePort = 0,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root '.runtime/local'
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root '.env'))) {
    throw 'Create ignored .env from .env.example before starting local services.'
}

& (Join-Path $Root 'server/scripts/run-local.ps1') `
    -WorkerCount $WorkerCount `
    -RuntimeDirectory $Runtime `
    -DirectorPort $DirectorPort `
    -WorkerBasePort $WorkerBasePort `
    -UdpBasePort $UdpBasePort `
    -Stop:$Stop
if ($LASTEXITCODE -ne 0) { throw 'Local server launcher failed.' }

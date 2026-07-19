$ErrorActionPreference = "Stop"

$deviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRoot = (Resolve-Path (Join-Path $deviceRoot "../..")).Path
$contractsRoot = Join-Path $deviceRoot "components/voice_contracts"
$coreRoot = Join-Path $deviceRoot "components/voice_core"
$canonicalFixtures = Join-Path $repoRoot "protocol/xiaozhi_udp_v1/fixtures"

foreach ($fixture in @("positive.json", "negative.json")) {
    if (-not (Test-Path -LiteralPath (Join-Path $canonicalFixtures $fixture))) {
        throw "Canonical UDP fixture is missing from repository root: $fixture"
    }
}

$forbidden = @("lvgl", "freertos", "board.h", "application.h", "audio_service", "websocket_protocol")
foreach ($sourceRoot in @($contractsRoot, $coreRoot)) {
    foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -Recurse -File -Include *.h,*.cc) {
        $content = Get-Content -Raw -LiteralPath $file.FullName
        foreach ($marker in $forbidden) {
            if ($content.Contains($marker, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Transport-neutral component contains forbidden dependency '$marker': $($file.FullName)"
            }
        }
    }
}

$duplicateFixtures = @(Get-ChildItem -LiteralPath $deviceRoot -Recurse -File |
    Where-Object { $_.Name -in @("positive.json", "negative.json") })
if ($duplicateFixtures.Count -ne 0) {
    throw "firmware/device must consume canonical root fixtures rather than copy them"
}

$referenceScripts = Join-Path $repoRoot "firmware/reference/xiaozhi-overlay/scripts"
foreach ($scriptName in @("test-udp-wire-fixtures.ps1", "test-udp-media-source-contract.ps1")) {
    $content = Get-Content -Raw -LiteralPath (Join-Path $referenceScripts $scriptName)
    if (-not $content.Contains('protocol/xiaozhi_udp_v1/fixtures/')) {
        throw "Reference fixture consumer is not rooted at canonical protocol/: $scriptName"
    }
}

& (Join-Path $PSScriptRoot "test-headless-contract.ps1")
if ($LASTEXITCODE -ne 0) { throw "Headless component contract failed" }
Write-Host "Firmware component dependency and canonical fixture boundaries passed."

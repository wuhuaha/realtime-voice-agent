$ErrorActionPreference = "Stop"

$deviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRoot = (Resolve-Path (Join-Path $deviceRoot "../..")).Path
$contractsRoot = Join-Path $repoRoot "firmware/components/voice_contracts"
$coreRoot = Join-Path $repoRoot "firmware/components/voice_core"
$canonicalFixtures = Join-Path $repoRoot "protocol/udp_opus_gcm_v1/fixtures"

foreach ($fixture in @("positive.json", "negative.json")) {
    if (-not (Test-Path -LiteralPath (Join-Path $canonicalFixtures $fixture))) {
        throw "Canonical UDP fixture is missing from repository root: $fixture"
    }
}

$forbidden = @(
    "lvgl",
    "freertos",
    "board.h",
    "application.h",
    "audio_service",
    "websocket_protocol",
    "esp_wifi",
    "esp_netif",
    "lwip/",
    "driver/i2s"
)
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
    throw "Headless harness must consume canonical root fixtures rather than copy them"
}

$hostTestScript = Get-Content -Raw -LiteralPath (
    Join-Path $PSScriptRoot "test-headless-contract.ps1")
foreach ($fixturePath in @(
    "protocol/udp_opus_gcm_v1/fixtures/positive.json",
    "protocol/udp_opus_gcm_v1/fixtures/negative.json"
)) {
    if (-not $hostTestScript.Contains($fixturePath, [StringComparison]::Ordinal)) {
        throw "Target host tests must consume the canonical root fixture: $fixturePath"
    }
}
if ($hostTestScript.Contains("external/", [StringComparison]::OrdinalIgnoreCase) -or
    $hostTestScript.Contains("firmware/reference/", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Target host tests must not depend on reference or external checkouts"
}

$cmakeContent = @(
    Get-Content -Raw -LiteralPath (Join-Path $contractsRoot "CMakeLists.txt")
    Get-Content -Raw -LiteralPath (Join-Path $coreRoot "CMakeLists.txt")
) -join "`n"
foreach ($dependency in @("lvgl", "esp_wifi", "esp_netif", "lwip", "voice_board", "voice_audio", "voice_transport")) {
    if ($cmakeContent.Contains($dependency, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Headless contract components contain forbidden dependency: $dependency"
    }
}

& (Join-Path $PSScriptRoot "test-headless-contract.ps1")
if ($LASTEXITCODE -ne 0) { throw "Headless component contract failed" }
Write-Host "Firmware component dependency and canonical fixture boundaries passed."

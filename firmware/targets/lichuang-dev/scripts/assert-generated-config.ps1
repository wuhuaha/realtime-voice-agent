param(
    [string]$Checkout = "",
    [string]$Config = ""
)

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }
if (-not $Config) { $Config = Join-Path $integration ".env.local" }
$headerPath = Join-Path $Checkout "main/voice_agent_local_config.h"

if (-not (Test-Path -LiteralPath $headerPath)) { throw "Generated header is missing" }
$header = Get-Content -Raw -LiteralPath $headerPath -Encoding utf8
foreach ($marker in @(
    "VOICE_AGENT_LOCAL_LAB 1",
    "VOICE_AGENT_WS_URL",
    "VOICE_AGENT_WS_TOKEN",
    "VOICE_AGENT_BOOTSTRAP_MODE",
    "VOICE_AGENT_DIRECTOR_URL",
    "VOICE_AGENT_BOOTSTRAP_TOKEN",
    "VOICE_AGENT_TENANT_ID",
    "VOICE_AGENT_DEVELOPMENT_DIRECT_FALLBACK",
    "VOICE_AGENT_WIFI_SSID",
    "VOICE_AGENT_WIFI_PASSWORD",
    "VOICE_AGENT_WIFI_FALLBACK_SSID",
    "VOICE_AGENT_WIFI_FALLBACK_PASSWORD"
)) {
    if (-not $header.Contains($marker)) { throw "Generated header is missing marker: $marker" }
}

$secrets = @{}
foreach ($line in Get-Content -LiteralPath $Config -Encoding utf8) {
    $separator = $line.IndexOf("=")
    if ($separator -gt 0) { $secrets[$line.Substring(0, $separator).Trim()] = $line.Substring($separator + 1) }
}
$secretKeys = @("XIAOZHI_WIFI_PASSWORD")
if (-not [string]::IsNullOrWhiteSpace($secrets.XIAOZHI_WIFI_FALLBACK_PASSWORD)) {
    $secretKeys += "XIAOZHI_WIFI_FALLBACK_PASSWORD"
}
foreach ($optionalSecret in @("XIAOZHI_LAB_TOKEN", "XIAOZHI_DEVICE_BOOTSTRAP_TOKEN")) {
    if (-not [string]::IsNullOrWhiteSpace($secrets[$optionalSecret])) {
        $secretKeys += $optionalSecret
    }
}
foreach ($key in $secretKeys) {
    $value = $secrets[$key]
    if ([string]::IsNullOrWhiteSpace($value)) { throw "Missing secret for assertion: $key" }
    & git -C $repoRoot grep -F --quiet -- $value
    if ($LASTEXITCODE -eq 0) { throw "A local secret is present in a tracked file ($key)" }
    if ($LASTEXITCODE -ne 1) { throw "git grep failed while checking tracked files" }
    & git -C $repoRoot grep --cached -F --quiet -- $value
    if ($LASTEXITCODE -eq 0) { throw "A local secret is present in the staged index ($key)" }
    if ($LASTEXITCODE -ne 1) { throw "git grep --cached failed while checking staged files" }
}
Write-Host "Generated configuration contract passed; tracked secret check passed."
$global:LASTEXITCODE = 0

param(
    [string]$Checkout = "",
    [string]$Config = ""
)

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }
if (-not $Config) { $Config = Join-Path $integration ".env.local" }

if (-not (Test-Path -LiteralPath $Config)) {
    throw "Missing ignored local config: $Config (start from .env.local.example)"
}

$values = @{}
foreach ($line in Get-Content -LiteralPath $Config -Encoding utf8) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
    $separator = $trimmed.IndexOf("=")
    if ($separator -lt 1) { throw "Invalid local config line (expected KEY=VALUE)" }
    $values[$trimmed.Substring(0, $separator).Trim()] = $trimmed.Substring($separator + 1)
}

$required = @("XIAOZHI_WS_URL", "XIAOZHI_LAB_TOKEN", "XIAOZHI_WIFI_SSID", "XIAOZHI_WIFI_PASSWORD")
foreach ($key in $required) {
    if (-not $values.ContainsKey($key) -or [string]::IsNullOrWhiteSpace($values[$key])) {
        throw "Missing required local config key: $key"
    }
}
if ($values.XIAOZHI_WS_URL -notmatch '^wss?://[^\s]+/v1/xiaozhi$') {
    throw "XIAOZHI_WS_URL must be ws:// or wss:// and end in /v1/xiaozhi"
}
if ($values.XIAOZHI_LAB_TOKEN -match '^Bearer\s') {
    throw "XIAOZHI_LAB_TOKEN must contain the raw token without the Bearer prefix"
}
$transportMode = if ($values.ContainsKey("XIAOZHI_TRANSPORT_MODE")) {
    $values.XIAOZHI_TRANSPORT_MODE
} else {
    "auto"
}
if ($transportMode -notin @("auto", "force_wss", "force_udp_for_test")) {
    throw "XIAOZHI_TRANSPORT_MODE must be auto, force_wss, or force_udp_for_test"
}

function ConvertTo-CString([string]$value) {
    return $value.Replace('\', '\\').Replace('"', '\"').Replace("`r", '\r').Replace("`n", '\n')
}

$header = @(
    "#pragma once",
    "",
    "// Generated from ignored local configuration. Do not commit or print this file.",
    "#define VOICE_AGENT_LOCAL_LAB 1",
    "#define VOICE_AGENT_WS_URL `"$(ConvertTo-CString $values.XIAOZHI_WS_URL)`"",
    "#define VOICE_AGENT_WS_TOKEN `"$(ConvertTo-CString $values.XIAOZHI_LAB_TOKEN)`"",
    "#define VOICE_AGENT_TRANSPORT_MODE `"$(ConvertTo-CString $transportMode)`"",
    "#define VOICE_AGENT_WIFI_SSID `"$(ConvertTo-CString $values.XIAOZHI_WIFI_SSID)`"",
    "#define VOICE_AGENT_WIFI_PASSWORD `"$(ConvertTo-CString $values.XIAOZHI_WIFI_PASSWORD)`""
) -join "`n"

$destination = Join-Path $Checkout "main/voice_agent_local_config.h"
[System.IO.File]::WriteAllText($destination, $header + "`n", [System.Text.UTF8Encoding]::new($false))
Write-Host "Generated local configuration header (values redacted)."

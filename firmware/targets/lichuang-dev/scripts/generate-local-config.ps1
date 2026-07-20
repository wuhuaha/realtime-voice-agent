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

function Test-CanonicalEndpoint([string]$Value, [string[]]$Schemes, [string]$ExpectedPath) {
    [Uri]$uri = $null
    if ([Text.Encoding]::UTF8.GetByteCount($Value) -gt 255 -or
        -not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri)) { return $false }
    $hasCanonicalScheme = @($Schemes | Where-Object {
        $Value.StartsWith("${_}://", [StringComparison]::Ordinal)
    }).Count -eq 1
    $authorityStart = $Value.IndexOf("://", [StringComparison]::Ordinal) + 3
    $rawPathStart = $Value.IndexOf("/", $authorityStart, [StringComparison]::Ordinal)
    $rawPath = if ($rawPathStart -ge 0) { $Value.Substring($rawPathStart) } else { "" }
    return $hasCanonicalScheme -and $uri.Scheme -in $Schemes -and
        -not [string]::IsNullOrWhiteSpace($uri.Host) -and
        [string]::IsNullOrEmpty($uri.UserInfo) -and
        $rawPath -ceq $ExpectedPath -and
        $uri.AbsolutePath -ceq $ExpectedPath -and
        [string]::IsNullOrEmpty($uri.Query) -and
        [string]::IsNullOrEmpty($uri.Fragment)
}

$transportMode = if ($values.ContainsKey("XIAOZHI_TRANSPORT_MODE")) {
    $values.XIAOZHI_TRANSPORT_MODE
} else {
    "auto"
}
if ($transportMode -notin @("auto", "force_wss", "force_udp_for_test")) {
    throw "XIAOZHI_TRANSPORT_MODE must be auto, force_wss, or force_udp_for_test"
}
$bootstrapMode = if ($values.ContainsKey("XIAOZHI_BOOTSTRAP_MODE")) {
    $values.XIAOZHI_BOOTSTRAP_MODE
} else {
    "direct"
}
if ($bootstrapMode -notin @("direct", "director")) {
    throw "XIAOZHI_BOOTSTRAP_MODE must be direct or director"
}
$directorUrl = if ($values.ContainsKey("XIAOZHI_DIRECTOR_URL")) {
    $values.XIAOZHI_DIRECTOR_URL
} else {
    ""
}
$bootstrapToken = if ($values.ContainsKey("XIAOZHI_DEVICE_BOOTSTRAP_TOKEN")) {
    $values.XIAOZHI_DEVICE_BOOTSTRAP_TOKEN
} else {
    ""
}
if ($bootstrapMode -ne "director") {
    $directorUrl = ""
    $bootstrapToken = ""
}
$tenantId = if ($values.ContainsKey("XIAOZHI_TENANT_ID")) {
    $values.XIAOZHI_TENANT_ID
} else {
    "default"
}
$developmentFallbackText = if ($values.ContainsKey("XIAOZHI_DEVELOPMENT_DIRECT_FALLBACK")) {
    $values.XIAOZHI_DEVELOPMENT_DIRECT_FALLBACK.ToLowerInvariant()
} else {
    "false"
}
if ($developmentFallbackText -notin @("true", "false")) {
    throw "XIAOZHI_DEVELOPMENT_DIRECT_FALLBACK must be true or false"
}
$developmentFallback = if ($developmentFallbackText -eq "true") { 1 } else { 0 }
$requiresDirectCredential = $bootstrapMode -eq "direct" -or $developmentFallback -eq 1
$required = @("XIAOZHI_WIFI_SSID", "XIAOZHI_WIFI_PASSWORD")
if ($requiresDirectCredential) {
    $required += @("XIAOZHI_WS_URL", "XIAOZHI_LAB_TOKEN")
}
foreach ($key in $required) {
    if (-not $values.ContainsKey($key) -or [string]::IsNullOrWhiteSpace($values[$key])) {
        throw "Missing required local config key: $key"
    }
}
$fallbackWifiSsid = if ($values.ContainsKey("XIAOZHI_WIFI_FALLBACK_SSID")) {
    $values.XIAOZHI_WIFI_FALLBACK_SSID
} else {
    ""
}
$fallbackWifiPassword = if ($values.ContainsKey("XIAOZHI_WIFI_FALLBACK_PASSWORD")) {
    $values.XIAOZHI_WIFI_FALLBACK_PASSWORD
} else {
    ""
}
if ([string]::IsNullOrWhiteSpace($fallbackWifiSsid) -xor
    [string]::IsNullOrWhiteSpace($fallbackWifiPassword)) {
    throw "XIAOZHI_WIFI_FALLBACK_SSID and XIAOZHI_WIFI_FALLBACK_PASSWORD must be configured together"
}
if (-not [string]::IsNullOrWhiteSpace($fallbackWifiSsid) -and
    $fallbackWifiSsid -ceq $values.XIAOZHI_WIFI_SSID -and
    $fallbackWifiPassword -cne $values.XIAOZHI_WIFI_PASSWORD) {
    throw "Primary and fallback WiFi cannot use the same SSID with different passwords"
}
$workerUrl = if ($requiresDirectCredential) { $values.XIAOZHI_WS_URL } else { "" }
$labToken = if ($requiresDirectCredential) { $values.XIAOZHI_LAB_TOKEN } else { "" }
if ($requiresDirectCredential -and
    -not (Test-CanonicalEndpoint $workerUrl @("ws", "wss") "/v1/xiaozhi")) {
    throw "XIAOZHI_WS_URL must be canonical ws:// or wss://, no more than 255 bytes, and end in /v1/xiaozhi"
}
if ($labToken -match '^Bearer\s') {
    throw "XIAOZHI_LAB_TOKEN must contain the raw token without the Bearer prefix"
}
if ($bootstrapMode -eq "director") {
    if (-not (Test-CanonicalEndpoint $directorUrl @("http", "https") "/v1/session/bootstrap")) {
        throw "XIAOZHI_DIRECTOR_URL must be http:// or https:// and end in /v1/session/bootstrap"
    }
    if ([string]::IsNullOrWhiteSpace($bootstrapToken)) {
        throw "Missing required local config key: XIAOZHI_DEVICE_BOOTSTRAP_TOKEN"
    }
    if ($bootstrapToken -match '^Bearer\s') {
        throw "XIAOZHI_DEVICE_BOOTSTRAP_TOKEN must contain the raw token without the Bearer prefix"
    }
}
if ($bootstrapMode -ne "director" -and $developmentFallbackText -eq "true") {
    throw "XIAOZHI_DEVELOPMENT_DIRECT_FALLBACK is only valid in director mode"
}
if ($tenantId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$') {
    throw "XIAOZHI_TENANT_ID must satisfy the canonical Identifier contract"
}

function ConvertTo-CString([string]$value) {
    return $value.Replace('\', '\\').Replace('"', '\"').Replace("`r", '\r').Replace("`n", '\n')
}

$header = @(
    "#pragma once",
    "",
    "// Generated from ignored local configuration. Do not commit or print this file.",
    "#define VOICE_AGENT_LOCAL_LAB 1",
    "#define VOICE_AGENT_WS_URL `"$(ConvertTo-CString $workerUrl)`"",
    "#define VOICE_AGENT_WS_TOKEN `"$(ConvertTo-CString $labToken)`"",
    "#define VOICE_AGENT_TRANSPORT_MODE `"$(ConvertTo-CString $transportMode)`"",
    "#define VOICE_AGENT_BOOTSTRAP_MODE `"$(ConvertTo-CString $bootstrapMode)`"",
    "#define VOICE_AGENT_DIRECTOR_URL `"$(ConvertTo-CString $directorUrl)`"",
    "#define VOICE_AGENT_BOOTSTRAP_TOKEN `"$(ConvertTo-CString $bootstrapToken)`"",
    "#define VOICE_AGENT_TENANT_ID `"$(ConvertTo-CString $tenantId)`"",
    "#define VOICE_AGENT_DEVELOPMENT_DIRECT_FALLBACK $developmentFallback",
    "#define VOICE_AGENT_WIFI_SSID `"$(ConvertTo-CString $values.XIAOZHI_WIFI_SSID)`"",
    "#define VOICE_AGENT_WIFI_PASSWORD `"$(ConvertTo-CString $values.XIAOZHI_WIFI_PASSWORD)`"",
    "#define VOICE_AGENT_WIFI_FALLBACK_SSID `"$(ConvertTo-CString $fallbackWifiSsid)`"",
    "#define VOICE_AGENT_WIFI_FALLBACK_PASSWORD `"$(ConvertTo-CString $fallbackWifiPassword)`""
) -join "`n"

$destination = Join-Path $Checkout "main/voice_agent_local_config.h"
[System.IO.File]::WriteAllText($destination, $header + "`n", [System.Text.UTF8Encoding]::new($false))
Write-Host "Generated local configuration header (values redacted)."

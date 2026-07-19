param([string]$Checkout = "")

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }

$sourcePath = Join-Path $Checkout "main/protocols/websocket_protocol.cc"
$source = Get-Content -Raw -LiteralPath $sourcePath

$required = @(
    '#include <http_parser.h>',
    'kMaxWebsocketUrlLength = 255',
    'http_parser_parse_url',
    'scheme != "ws" && scheme != "wss"',
    'host_field.len == 0',
    'Settings voice_agent("voice_agent", false)',
    'voice_agent.GetString("ws_url")',
    'const std::string upstream_token = websocket.GetString("token")',
    'voice_agent.GetString("token_origin")',
    'voice_agent_token_origin == endpoint.origin',
    'local_endpoint.origin == endpoint.origin',
    'upstream_endpoint.origin == endpoint.origin',
    'voice_agent.GetString("ws_url"),',
    'kProtocolVersionKey = "protocol_ver"',
    'voice_agent.GetInt(kProtocolVersionKey, kLocalProtocolVersion)',
    'VOICE_AGENT_WS_URL, VOICE_AGENT_WS_TOKEN',
    'Settings websocket("websocket", false)',
    'const WebsocketEndpointSnapshot endpoint = ResolveWebsocketEndpoint()',
    'endpoint.source, endpoint.host.c_str(), version_',
    'WebsocketProtocol::ParseEndpointOrigin'
)
foreach ($marker in $required) {
    if (-not $source.Contains($marker)) { throw "Endpoint resolver contract missing: $marker" }
}

$ordered = @(
    'Settings voice_agent("voice_agent", false)',
    'VOICE_AGENT_WS_URL, VOICE_AGENT_WS_TOKEN',
    '"upstream_websocket_nvs"'
)
$lastIndex = -1
foreach ($marker in $ordered) {
    $index = $source.IndexOf($marker, [StringComparison]::Ordinal)
    if ($index -le $lastIndex) { throw "Endpoint resolver precedence is invalid at: $marker" }
    $lastIndex = $index
}

if ($source.Contains('Connecting to websocket server: %s')) {
    throw "Endpoint resolver must not log the full WebSocket URL"
}
if ($source.Contains('GetInt("protocol_version"')) {
    throw "NVS key protocol_version exceeds the ESP-IDF 15-character key limit"
}
if ($source.Contains('voice_agent_token = VOICE_AGENT_WS_TOKEN') -or
    $source.Contains('voice_agent_token = upstream_token')) {
    throw "Endpoint credentials must not be inherited without an origin match"
}
if ($source -match 'ESP_LOG[A-Z]*\([^\r\n]*(token|authorization)') {
    throw "Endpoint resolver must not log credentials"
}

$patches = @(Get-ChildItem -LiteralPath (Join-Path $integration "overlay") -Filter "*.patch" -File |
    Sort-Object Name | ForEach-Object Name)
$requiredPrefix = @(
    "0001-local-lab-websocket-config.patch",
    "0002-voice-agent-display.patch",
    "0003-runtime-websocket-endpoint-override.patch"
)
if ($patches.Count -lt $requiredPrefix.Count -or
    (($patches[0..($requiredPrefix.Count - 1)] -join "`n") -ne ($requiredPrefix -join "`n"))) {
    throw "Overlay patch order changed: $($patches -join ', ')"
}

Write-Host "Endpoint resolver static contract passed."

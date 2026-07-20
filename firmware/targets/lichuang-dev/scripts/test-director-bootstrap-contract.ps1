param([string]$Checkout = "")

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }

$bootstrapPath = Join-Path $Checkout "main/protocols/voice_director_bootstrap.h"
$websocketPath = Join-Path $Checkout "main/protocols/websocket_protocol.cc"
$httpClientPath = Join-Path $Checkout "managed_components/78__esp-ml307/src/http_client.cc"
$httpInterfacePath = Join-Path $Checkout "managed_components/78__esp-ml307/include/http.h"
$tcpInterfacePath = Join-Path $Checkout "managed_components/78__esp-ml307/include/tcp.h"
$deadlinePath = Join-Path $Checkout "managed_components/78__esp-ml307/src/esp/esp_connection_deadline.h"
$espTcpPath = Join-Path $Checkout "managed_components/78__esp-ml307/src/esp/esp_tcp.cc"
$espSslPath = Join-Path $Checkout "managed_components/78__esp-ml307/src/esp/esp_ssl.cc"
$ml307HeaderPath = Join-Path $Checkout "managed_components/78__esp-ml307/src/ml307/ml307_http.h"
$ml307SourcePath = Join-Path $Checkout "managed_components/78__esp-ml307/src/ml307/ml307_http.cc"
$atUartPath = Join-Path $Checkout "managed_components/78__esp-ml307/src/at_uart.cc"
$atUartHeaderPath = Join-Path $Checkout "managed_components/78__esp-ml307/include/at_uart.h"
$webSocketHeaderPath = Join-Path $Checkout "managed_components/78__esp-ml307/include/web_socket.h"
$webSocketSourcePath = Join-Path $Checkout "managed_components/78__esp-ml307/src/web_socket.cc"
$wifiStationPath = Join-Path $Checkout "managed_components/78__esp-wifi-connect/wifi_station.cc"
$wifiBoardPath = Join-Path $Checkout "main/boards/common/wifi_board.cc"
$bootstrap = Get-Content -Raw -LiteralPath $bootstrapPath
$websocket = Get-Content -Raw -LiteralPath $websocketPath
$httpClient = Get-Content -Raw -LiteralPath $httpClientPath
$httpInterface = Get-Content -Raw -LiteralPath $httpInterfacePath
$tcpInterface = Get-Content -Raw -LiteralPath $tcpInterfacePath
$deadlineSource = Get-Content -Raw -LiteralPath $deadlinePath
$espTcpSource = Get-Content -Raw -LiteralPath $espTcpPath
$espSslSource = Get-Content -Raw -LiteralPath $espSslPath
$ml307Header = Get-Content -Raw -LiteralPath $ml307HeaderPath
$ml307Source = Get-Content -Raw -LiteralPath $ml307SourcePath
$atUart = Get-Content -Raw -LiteralPath $atUartPath
$atUartHeader = Get-Content -Raw -LiteralPath $atUartHeaderPath
$webSocketHeader = Get-Content -Raw -LiteralPath $webSocketHeaderPath
$webSocketSource = Get-Content -Raw -LiteralPath $webSocketSourcePath
$wifiStation = Get-Content -Raw -LiteralPath $wifiStationPath
$wifiBoard = Get-Content -Raw -LiteralPath $wifiBoardPath

$bootstrapMarkers = @(
    "kTimeoutMs = 5000",
    "kMaxResponseBytes = 8192",
    "kForbiddenFields",
    '"/v1/session/bootstrap"',
    '"/v1/xiaozhi"',
    'CreateHttp(2)',
    'SetHeader("Authorization"',
    'Open("POST", VOICE_AGENT_DIRECTOR_URL)',
    'SetResponseBodyLimit(director_bootstrap_detail::kMaxResponseBytes)',
    'ApplyRemainingTimeout(*http, deadline_us)',
    'CloseWithinDeadline(*http, deadline_us)',
    'response, deadline_us)',
    'esp_timer_get_time()',
    '"tenant_id", VOICE_AGENT_TENANT_ID',
    '"device_id", device_id.c_str()',
    '"supported_profiles"',
    '"wss-opus-v1"',
    '"udp-opus-gcm-v1"',
    '"worker_id"',
    '"worker_wss_url"',
    '"connect_grant"',
    '"session_epoch"',
    '"fencing_token"',
    '"allowed_profiles"',
    '"expires_at"',
    'VOICE_AGENT_DEVELOPMENT_DIRECT_FALLBACK == 1',
    '#if VOICE_AGENT_LOCAL_LAB'
)
foreach ($marker in $bootstrapMarkers) {
    if (-not $bootstrap.Contains($marker)) {
        throw "Director bootstrap source contract missing: $marker"
    }
}
if ($bootstrap.Contains("ReadAll()")) {
    throw "Director bootstrap must use a bounded response read"
}
if ($bootstrap -match 'ESP_LOG[A-Z]*\([^\r\n]*(token|grant|authorization|VOICE_AGENT_DIRECTOR_URL)') {
    throw "Director bootstrap logs must not contain credentials or the full configured URL"
}
if ($httpClient.Contains('HTTP request headers:\n%s') -or
    -not $httpClient.Contains('HTTP request prepared method=%s host=%s path=%s')) {
    throw "HTTP client must not log request header values"
}
$managedMarkers = @(
    @{ Content = $httpInterface; Marker = 'SetResponseBodyLimit(size_t max_bytes)' },
    @{ Content = $tcpInterface; Marker = 'SetTimeout(int timeout_ms)' },
    @{ Content = $deadlineSource; Marker = 'dns_gethostbyname_addrtype' },
    @{ Content = $deadlineSource; Marker = 'tcpip_try_callback(dns_detail::Start, request)' },
    @{ Content = $deadlineSource; Marker = 'references.fetch_sub' },
    @{ Content = $espTcpSource; Marker = 'select(tcp_fd_ + 1' },
    @{ Content = $espTcpSource; Marker = 'std::min(timeout_ms_, 250)' },
    @{ Content = $espTcpSource; Marker = 'WaitForReceiveTask(wait_ticks)' },
    @{ Content = $espSslSource; Marker = 'cfg.common_name = host.c_str()' },
    @{ Content = $espSslSource; Marker = 'cfg.timeout_ms = deadline.RemainingMs()' },
    @{ Content = $espSslSource; Marker = 'SO_SNDTIMEO' },
    @{ Content = $espSslSource; Marker = 'WaitForReceiveTask(wait_ticks)' },
    @{ Content = $httpClient; Marker = 'tcp_->SetTimeout(connect_timeout_ms)' },
    @{ Content = $httpClient; Marker = 'tcp_->SetTimeout(send_timeout_ms)' },
    @{ Content = $ml307Header; Marker = 'response_body_limit_' },
    @{ Content = $ml307Source; Marker = 'total_body_received_ > response_body_limit_' },
    @{ Content = $ml307Source; Marker = 'response_body_limit_exceeded_' },
    @{ Content = $ml307Source; Marker = 'const auto remaining_ms' },
    @{ Content = $ml307Source; Marker = 'MHTTPCFG=\"timeout\"' },
    @{ Content = $ml307Source; Marker = 'std::min(timeout_ms_, 250)' },
    @{ Content = $atUart; Marker = 'const auto remaining_ticks' },
    @{ Content = $atUart; Marker = 'try_lock_for(std::chrono::milliseconds(timeout_ms))' },
    @{ Content = $atUartHeader; Marker = 'std::timed_mutex command_mutex_' },
    @{ Content = $tcpInterface; Marker = 'IsReceiveTask() const' },
    @{ Content = $webSocketHeader; Marker = 'bool Disconnect()' },
    @{ Content = $webSocketHeader; Marker = 'bool IsReceiveTask() const' },
    @{ Content = $webSocketSource; Marker = 'if (!Disconnect())' },
    @{ Content = $webSocketSource; Marker = 'tcp_->Disconnect()' },
    @{ Content = $webSocketSource; Marker = 'tcp_.reset()' },
    @{ Content = $wifiStation; Marker = 'for (const auto& credential : ssid_list)' },
    @{ Content = $wifiBoard; Marker = 'ssid_manager.SetDefaultSsid' },
    @{ Content = $atUart; Marker = 'AT+MHTTPHEADER=<redacted>' }
)
foreach ($managedMarker in $managedMarkers) {
    if (-not $managedMarker.Content.Contains($managedMarker.Marker)) {
        throw "Managed HTTP bootstrap safety contract missing: $($managedMarker.Marker)"
    }
}
if ($espTcpSource.Contains('vTaskDelete(receive_task_handle_)') -or
    $espSslSource.Contains('vTaskDelete(receive_task_handle_)')) {
    throw "TCP receive tasks must acknowledge task-owned shutdown; callers must not force-delete them"
}
if ($webSocketSource.Contains('tcp_->OnStream({})') -or
    $webSocketSource.Contains('tcp_->OnDisconnected({})')) {
    throw "WebSocket must not rewrite transport callbacks before receive-task join is confirmed"
}
foreach ($marker in @(
    'const auto websocket_owner = websocket_;',
    'websocket_owner->IsReceiveTask()',
    'Application::GetInstance().Schedule([this]()',
    'old_websocket->Disconnect()'
)) {
    if (-not $websocket.Contains($marker)) {
        throw "WebSocket owner-task teardown contract missing: $marker"
    }
}
$closeStart = $websocket.IndexOf(
    'void WebsocketProtocol::CloseAudioChannel(bool send_goodbye)',
    [StringComparison]::Ordinal)
$openStart = $websocket.IndexOf(
    'bool WebsocketProtocol::OpenAudioChannel()',
    [StringComparison]::Ordinal)
if ($closeStart -lt 0 -or $openStart -le $closeStart) {
    throw "WebSocket close/open source boundaries are invalid"
}
$closeSource = $websocket.Substring($closeStart, $openStart - $closeStart)
$disconnectIndex = $closeSource.IndexOf(
    'old_websocket->Disconnect()', [StringComparison]::Ordinal)
$releaseIndex = $closeSource.IndexOf(
    'old_websocket.reset()', [StringComparison]::Ordinal)
if ($disconnectIndex -lt 0 -or $releaseIndex -le $disconnectIndex) {
    throw "WebSocket owner must disconnect/join before releasing its final shared owner"
}
if ($atUart.Contains('ESP_LOGI(TAG, ">> %.64s') -and
    -not $atUart.Contains('command.compare(0, 15, "AT+MHTTPHEADER=")')) {
    throw "AT command logging must redact HTTP header commands"
}

$openIndex = $websocket.IndexOf("bool WebsocketProtocol::OpenAudioChannel()", [StringComparison]::Ordinal)
if ($openIndex -lt 0) { throw "WebSocket open path is missing" }
$openPath = $websocket.Substring($openIndex)
$ordered = @(
    "CloseAudioChannel(false)",
    "RequestDirectorBootstrap(GetTransportMode())",
    "DirectorBootstrapStatus::kFailed",
    "DevelopmentDirectFallbackEnabled()",
    "ResolveWebsocketEndpoint()",
    "CreateWebSocket(1)"
)
$lastIndex = -1
foreach ($marker in $ordered) {
    $index = $openPath.IndexOf($marker, [StringComparison]::Ordinal)
    if ($index -le $lastIndex) {
        throw "Director bootstrap/open ordering is invalid at: $marker"
    }
    $lastIndex = $index
}
if (-not $openPath.Contains('"director_bootstrap"')) {
    throw "Director endpoint source label is missing"
}
if ($openPath.IndexOf("Connect(endpoint.url.c_str())", [StringComparison]::Ordinal) -lt $lastIndex) {
    throw "Worker connection must happen after bootstrap and endpoint selection"
}

$generatorPath = Join-Path $PSScriptRoot "generate-local-config.ps1"
$generator = Get-Content -Raw -LiteralPath $generatorPath
foreach ($marker in @(
    'XIAOZHI_BOOTSTRAP_MODE',
    'XIAOZHI_DIRECTOR_URL',
    'XIAOZHI_DEVICE_BOOTSTRAP_TOKEN',
    'XIAOZHI_TENANT_ID',
    'XIAOZHI_DEVELOPMENT_DIRECT_FALLBACK',
    'VOICE_AGENT_BOOTSTRAP_MODE',
    'VOICE_AGENT_DIRECTOR_URL',
    'VOICE_AGENT_BOOTSTRAP_TOKEN',
    'VOICE_AGENT_TENANT_ID',
    'VOICE_AGENT_DEVELOPMENT_DIRECT_FALLBACK'
)) {
    if (-not $generator.Contains($marker)) {
        throw "Director bootstrap config contract missing: $marker"
    }
}

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    "voice-agent-director-contract-" + [guid]::NewGuid().ToString("N"))
$temporaryCheckout = Join-Path $temporaryDirectory "checkout"
$configPath = Join-Path $temporaryDirectory "config.env"
$generatedHeader = Join-Path $temporaryCheckout "main/voice_agent_local_config.h"
$utf8NoBom = [Text.UTF8Encoding]::new($false)
$hostExecutable = (Get-Process -Id $PID).Path
$labToken = "contract-lab-" + [guid]::NewGuid().ToString("N")
$bootstrapToken = "contract-bootstrap-" + [guid]::NewGuid().ToString("N")
$wifiPassword = "contract-wifi-" + [guid]::NewGuid().ToString("N")
$fallbackWifiPassword = "contract-wifi-fallback-" + [guid]::NewGuid().ToString("N")

try {
    [void](New-Item -ItemType Directory -Path (Split-Path -Parent $generatedHeader))
    [IO.File]::WriteAllText($configPath, @"
XIAOZHI_WS_URL=ws://192.0.2.10:8080/v1/xiaozhi
XIAOZHI_LAB_TOKEN=$labToken
XIAOZHI_TRANSPORT_MODE=auto
XIAOZHI_DIRECTOR_URL=https://stale-director.invalid/v1/session/bootstrap
XIAOZHI_DEVICE_BOOTSTRAP_TOKEN=$bootstrapToken
XIAOZHI_WIFI_SSID=contract-ssid
XIAOZHI_WIFI_PASSWORD=$wifiPassword
XIAOZHI_WIFI_FALLBACK_SSID=contract-fallback-ssid
XIAOZHI_WIFI_FALLBACK_PASSWORD=$fallbackWifiPassword
"@, $utf8NoBom)
    $directGenerationOutput = (& $generatorPath -Checkout $temporaryCheckout -Config $configPath 2>&1) -join "`n"
    if ($directGenerationOutput.Contains($bootstrapToken) -or
        $directGenerationOutput.Contains($wifiPassword) -or
        $directGenerationOutput.Contains($fallbackWifiPassword)) {
        throw "Direct configuration generation output exposed a credential"
    }
    $directHeader = Get-Content -Raw -LiteralPath $generatedHeader
    if (-not $directHeader.Contains('#define VOICE_AGENT_BOOTSTRAP_MODE "direct"') -or
        -not $directHeader.Contains('#define VOICE_AGENT_DIRECTOR_URL ""') -or
        -not $directHeader.Contains('#define VOICE_AGENT_BOOTSTRAP_TOKEN ""') -or
        $directHeader.Contains($bootstrapToken) -or
        -not $directHeader.Contains('#define VOICE_AGENT_DEVELOPMENT_DIRECT_FALLBACK 0')) {
        throw "Legacy direct configuration did not preserve the direct Worker path or clear stale Director credentials"
    }
    if (-not $directHeader.Contains('#define VOICE_AGENT_WIFI_FALLBACK_SSID "contract-fallback-ssid"') -or
        -not $directHeader.Contains("VOICE_AGENT_WIFI_FALLBACK_PASSWORD")) {
        throw "Direct configuration did not preserve the fallback WiFi credential"
    }

    [IO.File]::WriteAllText($configPath, @"
XIAOZHI_WS_URL=ws://192.0.2.10:8080/v1/xiaozhi
XIAOZHI_LAB_TOKEN=$labToken
XIAOZHI_WIFI_SSID=contract-ssid
XIAOZHI_WIFI_PASSWORD=$wifiPassword
XIAOZHI_WIFI_FALLBACK_SSID=contract-fallback-ssid
"@, $utf8NoBom)
    & $hostExecutable -NoLogo -NoProfile -NonInteractive -File $generatorPath `
        -Checkout $temporaryCheckout -Config $configPath *> $null
    if ($LASTEXITCODE -eq 0) {
        throw "Configuration accepted an incomplete fallback WiFi credential"
    }

    [IO.File]::WriteAllText($configPath, @"
XIAOZHI_WS_URL=ws://192.0.2.10:8080/v1/xiaozhi
XIAOZHI_LAB_TOKEN=$labToken
XIAOZHI_WIFI_SSID=duplicate-ssid
XIAOZHI_WIFI_PASSWORD=$wifiPassword
XIAOZHI_WIFI_FALLBACK_SSID=duplicate-ssid
XIAOZHI_WIFI_FALLBACK_PASSWORD=$fallbackWifiPassword
"@, $utf8NoBom)
    & $hostExecutable -NoLogo -NoProfile -NonInteractive -File $generatorPath `
        -Checkout $temporaryCheckout -Config $configPath *> $null
    if ($LASTEXITCODE -eq 0) {
        throw "Configuration accepted one SSID with conflicting passwords"
    }

    [IO.File]::WriteAllText($configPath, @"
XIAOZHI_WS_URL=ws://192.0.2.10:8080/v1/xiaozhi
XIAOZHI_LAB_TOKEN=$labToken
XIAOZHI_TRANSPORT_MODE=force_wss
XIAOZHI_BOOTSTRAP_MODE=director
XIAOZHI_DIRECTOR_URL=https://director.invalid/v1/session/bootstrap
XIAOZHI_DEVICE_BOOTSTRAP_TOKEN=$bootstrapToken
XIAOZHI_TENANT_ID=tenant-1
XIAOZHI_DEVELOPMENT_DIRECT_FALLBACK=true
XIAOZHI_WIFI_SSID=contract-ssid
XIAOZHI_WIFI_PASSWORD=$wifiPassword
"@, $utf8NoBom)
    $generationOutput = (& $generatorPath -Checkout $temporaryCheckout -Config $configPath 2>&1) -join "`n"
    if ($generationOutput.Contains($labToken) -or $generationOutput.Contains($bootstrapToken) -or
        $generationOutput.Contains($wifiPassword)) {
        throw "Configuration generation output exposed a credential"
    }
    $directorHeader = Get-Content -Raw -LiteralPath $generatedHeader
    foreach ($marker in @(
        '#define VOICE_AGENT_BOOTSTRAP_MODE "director"',
        '#define VOICE_AGENT_DIRECTOR_URL "https://director.invalid/v1/session/bootstrap"',
        '#define VOICE_AGENT_TENANT_ID "tenant-1"',
        '#define VOICE_AGENT_DEVELOPMENT_DIRECT_FALLBACK 1'
    )) {
        if (-not $directorHeader.Contains($marker)) {
            throw "Generated Director configuration is missing: $marker"
        }
    }

    [IO.File]::WriteAllText($configPath, @"
XIAOZHI_TRANSPORT_MODE=auto
XIAOZHI_BOOTSTRAP_MODE=director
XIAOZHI_DIRECTOR_URL=https://director.invalid/v1/session/bootstrap
XIAOZHI_DEVICE_BOOTSTRAP_TOKEN=$bootstrapToken
XIAOZHI_TENANT_ID=tenant-1
XIAOZHI_DEVELOPMENT_DIRECT_FALLBACK=false
XIAOZHI_WIFI_SSID=contract-ssid
XIAOZHI_WIFI_PASSWORD=$wifiPassword
"@, $utf8NoBom)
    & $generatorPath -Checkout $temporaryCheckout -Config $configPath *> $null
    $directorOnlyHeader = Get-Content -Raw -LiteralPath $generatedHeader
    if (-not $directorOnlyHeader.Contains('#define VOICE_AGENT_WS_URL ""') -or
        -not $directorOnlyHeader.Contains('#define VOICE_AGENT_WS_TOKEN ""') -or
        $directorOnlyHeader.Contains($labToken)) {
        throw "Director-only configuration embedded a static direct Worker credential"
    }

    [IO.File]::WriteAllText($configPath, @"
XIAOZHI_WS_URL=ws://192.0.2.10:8080/v1/xiaozhi
XIAOZHI_LAB_TOKEN=$labToken
XIAOZHI_BOOTSTRAP_MODE=director
XIAOZHI_DIRECTOR_URL=https://director.invalid/v1/session/bootstrap
XIAOZHI_WIFI_SSID=contract-ssid
XIAOZHI_WIFI_PASSWORD=$wifiPassword
"@, $utf8NoBom)
    & $hostExecutable -NoLogo -NoProfile -NonInteractive -File $generatorPath `
        -Checkout $temporaryCheckout -Config $configPath *> $null
    if ($LASTEXITCODE -eq 0) {
        throw "Director mode accepted a missing bootstrap credential"
    }

    foreach ($invalidDirectorUrl in @(
        "HTTPS://director.invalid/v1/session/bootstrap",
        "https://user@director.invalid/v1/session/bootstrap",
        "https://director.invalid/v1/session/bootstrap?debug=1",
        "https://director.invalid/a/../v1/session/bootstrap",
        "https://director.invalid/%76%31/session/bootstrap",
        "https://director.invalid/wrong",
        ("https://" + ("a" * 230) + ".invalid/v1/session/bootstrap")
    )) {
        [IO.File]::WriteAllText($configPath, @"
XIAOZHI_WS_URL=ws://192.0.2.10:8080/v1/xiaozhi
XIAOZHI_LAB_TOKEN=$labToken
XIAOZHI_BOOTSTRAP_MODE=director
XIAOZHI_DIRECTOR_URL=$invalidDirectorUrl
XIAOZHI_DEVICE_BOOTSTRAP_TOKEN=$bootstrapToken
XIAOZHI_WIFI_SSID=contract-ssid
XIAOZHI_WIFI_PASSWORD=$wifiPassword
"@, $utf8NoBom)
        & $hostExecutable -NoLogo -NoProfile -NonInteractive -File $generatorPath `
            -Checkout $temporaryCheckout -Config $configPath *> $null
        if ($LASTEXITCODE -eq 0) {
            throw "Director mode accepted a non-canonical endpoint"
        }
    }

    Write-Host "Director bootstrap source and configuration contract passed."
    $global:LASTEXITCODE = 0
} finally {
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

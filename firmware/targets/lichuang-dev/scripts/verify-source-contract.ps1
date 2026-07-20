param([string]$Checkout = "")

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }
$expectedRevision = "7b190b78e4f8dfef14126f6cd478c134b3cd3cd8"

$actualRevision = (& git -C $Checkout rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualRevision -ne $expectedRevision) { throw "Pinned revision mismatch" }

$controlledLock = Join-Path $repoRoot "firmware/locks/xiaozhi-esp32.dependencies.lock"
$checkoutLock = Join-Path $Checkout "dependencies.lock"
if (-not (Test-Path -LiteralPath $controlledLock)) {
    throw "Controlled Xiaozhi dependency lock is missing: $controlledLock"
}
if (-not (Test-Path -LiteralPath $checkoutLock)) {
    throw "Materialized Xiaozhi dependency lock is missing: $checkoutLock"
}
$controlledLockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $controlledLock).Hash
$checkoutLockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $checkoutLock).Hash
if ($checkoutLockHash -ne $controlledLockHash) {
    throw "Materialized Xiaozhi dependency lock differs from the controlled copy"
}

$checks = @(
    @{ Path = "LICENSE"; Pattern = "MIT License"; Label = "MIT repository license" },
    @{ Path = "main/idf_component.yml"; Pattern = "version: '>=5.5.2'"; Label = "ESP-IDF >=5.5.2" },
    @{ Path = "main/idf_component.yml"; Pattern = "espressif/esp-sr: ~2.3.0"; Label = "ESP-SR ~2.3.0" },
    @{ Path = "main/boards/lichuang-dev/config.h"; Pattern = "#define AUDIO_INPUT_REFERENCE    true"; Label = "physical reference input" },
    @{ Path = "main/boards/lichuang-dev/config.json"; Pattern = '"CONFIG_USE_DEVICE_AEC=y"'; Label = "device AEC board config" },
    @{ Path = "main/audio/audio_service.h"; Pattern = "#define OPUS_FRAME_DURATION_MS 60"; Label = "60 ms Opus" },
    @{ Path = "main/audio/audio_service.cc"; Pattern = "encoder_sample_rate_ = 16000"; Label = "16 kHz encoder" },
    @{ Path = "main/application.cc"; Pattern = "listening_mode_ != kListeningModeRealtime"; Label = "realtime playback capture" },
    @{ Path = "main/application.cc"; Pattern = "aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime"; Label = "AEC selects realtime" },
    @{ Path = "main/protocols/websocket_protocol.cc"; Pattern = 'SetHeader("Authorization"'; Label = "Authorization header" },
    @{ Path = "main/protocols/websocket_protocol.cc"; Pattern = 'SetHeader("Protocol-Version"'; Label = "Protocol-Version header" },
    @{ Path = "main/protocols/websocket_protocol.cc"; Pattern = 'SetHeader("Device-Id"'; Label = "Device-Id header" },
    @{ Path = "main/protocols/websocket_protocol.cc"; Pattern = 'SetHeader("Client-Id"'; Label = "Client-Id header" },
    @{ Path = "main/protocols/websocket_protocol.cc"; Pattern = 'cJSON_AddNumberToObject(audio_params, "sample_rate", 16000)'; Label = "hello sample rate" },
    @{ Path = "main/protocols/websocket_protocol.cc"; Pattern = 'cJSON_AddNumberToObject(audio_params, "frame_duration", OPUS_FRAME_DURATION_MS)'; Label = "hello frame duration" }
)

foreach ($check in $checks) {
    $content = Get-Content -Raw -LiteralPath (Join-Path $Checkout $check.Path)
    if (-not $content.Contains($check.Pattern)) { throw "Pinned source contract missing: $($check.Label)" }
}
Write-Host "Pinned Xiaozhi source and dependency lock contract passed at $expectedRevision."

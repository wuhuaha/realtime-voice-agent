param([string]$Checkout = "")

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }
$Checkout = (Resolve-Path -LiteralPath $Checkout).Path

$paths = @{
    VoiceSource = Join-Path $Checkout "main/protocols/voice_udp_media.cc"
    VoiceHeader = Join-Path $Checkout "main/protocols/voice_udp_media.h"
    WireHeader = Join-Path $Checkout "main/protocols/voice_udp_wire.h"
    WebsocketSource = Join-Path $Checkout "main/protocols/websocket_protocol.cc"
    WebsocketHeader = Join-Path $Checkout "main/protocols/websocket_protocol.h"
    ProtocolSource = Join-Path $Checkout "main/protocols/protocol.cc"
    ProtocolHeader = Join-Path $Checkout "main/protocols/protocol.h"
    MqttSource = Join-Path $Checkout "main/protocols/mqtt_protocol.cc"
    MqttHeader = Join-Path $Checkout "main/protocols/mqtt_protocol.h"
    UdpSource = Join-Path $Checkout "managed_components/78__esp-ml307/src/esp/esp_udp.cc"
    UdpHeader = Join-Path $Checkout "managed_components/78__esp-ml307/src/esp/esp_udp.h"
}
foreach ($entry in $paths.GetEnumerator()) {
    if (-not (Test-Path -LiteralPath $entry.Value)) {
        throw "Final source is not prepared: $($entry.Value)"
    }
}

$voiceSource = (Get-Content -Raw -LiteralPath $paths.VoiceSource).Replace("`r`n", "`n")
$voiceHeader = (Get-Content -Raw -LiteralPath $paths.VoiceHeader).Replace("`r`n", "`n")
$wireHeader = (Get-Content -Raw -LiteralPath $paths.WireHeader).Replace("`r`n", "`n")
$websocketSource = (Get-Content -Raw -LiteralPath $paths.WebsocketSource).Replace("`r`n", "`n")
$websocketHeader = (Get-Content -Raw -LiteralPath $paths.WebsocketHeader).Replace("`r`n", "`n")
$protocolSource = (Get-Content -Raw -LiteralPath $paths.ProtocolSource).Replace("`r`n", "`n")
$protocolHeader = (Get-Content -Raw -LiteralPath $paths.ProtocolHeader).Replace("`r`n", "`n")
$mqttSource = (Get-Content -Raw -LiteralPath $paths.MqttSource).Replace("`r`n", "`n")
$mqttHeader = (Get-Content -Raw -LiteralPath $paths.MqttHeader).Replace("`r`n", "`n")
$udpSource = (Get-Content -Raw -LiteralPath $paths.UdpSource).Replace("`r`n", "`n")
$udpHeader = (Get-Content -Raw -LiteralPath $paths.UdpHeader).Replace("`r`n", "`n")
$build = (Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot "build.ps1")).Replace("`r`n", "`n")
$configAssertion = Get-Content -Raw -LiteralPath (
    Join-Path $PSScriptRoot "assert-generated-config.ps1")
$positiveFixturePath = Join-Path $repoRoot "protocol/udp_opus_gcm_v1/fixtures/positive.json"
$negativeFixturePath = Join-Path $repoRoot "protocol/udp_opus_gcm_v1/fixtures/negative.json"
$errors = [System.Collections.Generic.List[string]]::new()

function Add-MissingMarkers {
    param([string]$Name, [string]$Text, [string[]]$Markers)
    foreach ($marker in $Markers) {
        if (-not $Text.Contains($marker)) {
            $script:errors.Add("$Name missing final-source marker: $marker")
        }
    }
}

function Get-SourceSection {
    param([string]$Text, [string]$Start, [string]$End, [string]$Name)
    $startIndex = $Text.IndexOf($Start)
    $endIndex = if ($startIndex -ge 0) { $Text.IndexOf($End, $startIndex) } else { -1 }
    if ($startIndex -lt 0 -or $endIndex -lt 0) {
        $script:errors.Add("unable to locate final-source section: $Name")
        return ""
    }
    return $Text.Substring($startIndex, $endIndex - $startIndex)
}

function Get-SourceTail {
    param([string]$Text, [string]$Start, [string]$Name)
    $startIndex = $Text.IndexOf($Start)
    if ($startIndex -lt 0) {
        $script:errors.Add("unable to locate final-source section: $Name")
        return ""
    }
    return $Text.Substring($startIndex)
}

function ConvertFrom-Hex {
    param([string]$Hex)
    if (($Hex.Length % 2) -ne 0 -or $Hex -notmatch '^[0-9a-fA-F]*$') {
        throw "Invalid fixture hex"
    }
    $bytes = [byte[]]::new($Hex.Length / 2)
    for ($index = 0; $index -lt $bytes.Length; $index++) {
        $bytes[$index] = [Convert]::ToByte($Hex.Substring($index * 2, 2), 16)
    }
    return ,$bytes
}

function ConvertTo-Hex {
    param([byte[]]$Bytes)
    return [Convert]::ToHexString($Bytes).ToLowerInvariant()
}

function Read-UInt32BigEndian {
    param([byte[]]$Bytes, [int]$Offset)
    return [uint32](([uint32]$Bytes[$Offset] -shl 24) -bor
                    ([uint32]$Bytes[$Offset + 1] -shl 16) -bor
                    ([uint32]$Bytes[$Offset + 2] -shl 8) -bor
                    [uint32]$Bytes[$Offset + 3])
}

Add-MissingMarkers "voice_udp_media.cc" $voiceSource @(
    "constexpr uint32_t kTimestampClockHz = 16000;",
    "missing_before_first * kTimestampSamplesPerFrame",
    "voice_udp_wire::EncodeHeader(header)",
    "voice_udp_wire::ParseDatagram(",
    "voice_udp_wire::MatchesSession(view.header, media_id_, media_epoch_)",
    "voice_udp_wire::MakeNonce(downlink_salt_, sequence)",
    "view.header_bytes, voice_udp_wire::kHeaderBytes",
    "void VoiceUdpMedia::RequestStop()",
    "revoked_.exchange(true, std::memory_order_acq_rel)",
    "kStopRequestedEvent | kDataReadyEvent",
    "std::lock_guard<std::mutex> send_lock(send_mutex_);",
    "this one lower-layer Send call",
    "AdvanceGenerationLocked(generation);",
    "if (++concealed_in_pass >= kMaxConcealmentPerPass)",
    "taskYIELD();",
    "udp_.reset();"
)
Add-MissingMarkers "voice_udp_media.h" $voiceHeader @(
    '#include "voice_udp_wire.h"',
    "kMaxForwardSequenceDistance = 32",
    "kMaxConcealmentPerPass = 2",
    "void RequestStop();",
    "std::atomic<bool> revoked_",
    "std::atomic<TaskHandle_t> reorder_task_",
    "std::mutex lifecycle_mutex_",
    "std::mutex send_mutex_",
    "void AdvanceGenerationLocked"
)
Add-MissingMarkers "voice_udp_wire.h" $wireHeader @(
    "enum class Direction",
    "struct DatagramView",
    "HeaderFieldsValid",
    "EncodeHeader",
    "ParseDatagram",
    "MatchesSession",
    "MakeNonce",
    "return header.payload_length == 0;"
)
Add-MissingMarkers "websocket_protocol.cc" $websocketSource @(
    "ParseServerHello(root, connection_generation);",
    "ParseServerHello(const cJSON* root,",
    "IsCurrentConnectionLocked(connection_generation)",
    "candidate_udp_media->Configure",
    "udp_media_ = std::move(candidate_udp_media);",
    "starting_udp_media->Start();",
    "udp_media_ == starting_udp_media",
    "starting_udp_media->RequestStop();",
    "old_udp_media->RequestStop();",
    "LoadWebsocketOwner(connection_generation)",
    "websocket_generation_ != connection_generation",
    "bool WebsocketProtocol::SendText(const std::string& text,",
    "SendText(message, connection_generation)",
    "old_udp_media = std::move(udp_media_);",
    "ResetUdpStateLocked();",
    "parsed_generation <= playback_generation_",
    "json_callback = on_incoming_json_;",
    "json_callback(root);"
)
Add-MissingMarkers "websocket_protocol.h" $websocketHeader @(
    "ParseServerHello(const cJSON* root, uint32_t connection_generation)",
    "IsCurrentConnectionLocked",
    "ResetUdpStateLocked",
    "std::shared_ptr<WebSocket> websocket_",
    "uint32_t websocket_generation_",
    "mutable std::mutex control_owner_mutex_",
    "LoadWebsocketOwner",
    "std::shared_ptr<VoiceUdpMedia> udp_media_",
    "mutable std::mutex udp_media_mutex_"
)
Add-MissingMarkers "protocol.cc" $protocolSource @(
    "std::atomic_load_explicit(&session_snapshot_",
    "std::atomic_store_explicit(",
    "std::memory_order_acquire",
    "std::memory_order_release"
)
Add-MissingMarkers "protocol.h" $protocolHeader @(
    "struct SessionSnapshot",
    "uint32_t connection_generation = 0",
    "std::shared_ptr<const SessionSnapshot> session_snapshot_",
    "LoadSessionSnapshot() const",
    "StoreSessionSnapshot(SessionSnapshot snapshot)"
)
Add-MissingMarkers "mqtt_protocol.cc" $mqttSource @(
    "bool MqttProtocol::SendText(const std::string& text,",
    "connection_generation_.load(std::memory_order_acquire)",
    "opening_snapshot.connection_generation = connection_generation",
    "candidate_snapshot.connection_generation",
    "SendText(message, snapshot->connection_generation)"
)
Add-MissingMarkers "mqtt_protocol.h" $mqttHeader @(
    "std::atomic<uint32_t> connection_generation_",
    "uint32_t connection_generation) override"
)
Add-MissingMarkers "esp_udp.cc" $udpSource @(
    "task_created != pdPASS",
    "shutdown(udp_fd_, SHUT_RDWR)",
    "pdMS_TO_TICKS(2000)",
    "std::abort();"
)
Add-MissingMarkers "esp_udp.h" $udpHeader @(
    "std::atomic<bool> running_",
    "std::mutex lifecycle_mutex_",
    "std::mutex send_mutex_"
)
Add-MissingMarkers "build.ps1" $build @(
    "test-udp-media-source-contract.ps1",
    "Get-FileHash",
    "config-input"
)
Add-MissingMarkers "assert-generated-config.ps1" $configAssertion @(
    "grep --cached",
    "staged index"
)

if ($websocketSource.Contains("ParseServerHello(root);")) {
    $errors.Add("server hello callback must pass its connection generation")
}
if ($websocketSource.Contains("websocket_->") -or
    $websocketSource.Contains("starting_udp_media->Stop();")) {
    $errors.Add("WebSocket I/O must use a retained generation-bound owner and UDP teardown must revoke")
}
if ($voiceSource.Contains("xTaskNotifyGive(")) {
    $errors.Add("jitter wakeup must not notify a task handle that can self-delete")
}
if ($voiceSource.Contains("WireHeader") -or
    $voiceSource.Contains("ValidateHeaderFields") -or
    $voiceSource.Contains("htonl(") -or $voiceSource.Contains("ntohl(")) {
    $errors.Add("UDP media must use the canonical wire helper without a copied parser/header")
}
foreach ($sourceEntry in @($protocolSource, $mqttSource, $websocketSource)) {
    if ($sourceEntry -match '\b(session_id_|server_sample_rate_|server_frame_duration_)\b') {
        $errors.Add("protocol implementations must read one retained immutable session snapshot")
        break
    }
}
$parseHello = Get-SourceTail $websocketSource `
    "void WebsocketProtocol::ParseServerHello" "ParseServerHello"
$candidateConfigure = $parseHello.IndexOf("candidate_udp_media->Configure")
$helloLock = if ($candidateConfigure -ge 0) {
    $parseHello.IndexOf("std::lock_guard<std::mutex> lock(udp_media_mutex_);", $candidateConfigure)
} else { -1 }
$helloRecheck = if ($helloLock -ge 0) {
    $parseHello.IndexOf("if (!IsCurrentConnectionLocked(connection_generation))", $helloLock)
} else { -1 }
$helloInstall = if ($helloRecheck -ge 0) {
    $parseHello.IndexOf("udp_media_ = std::move(candidate_udp_media);", $helloRecheck)
} else { -1 }
if ($candidateConfigure -lt 0 -or $helloLock -le $candidateConfigure -or
    $helloRecheck -le $helloLock -or $helloInstall -le $helloRecheck) {
    $errors.Add("server hello must recheck generation under lock before installing grant state")
}
$onData = Get-SourceSection $websocketSource `
    "websocket->OnData" "websocket->OnDisconnected" "OnData callback"
if ($onData.IndexOf("std::lock_guard<std::mutex> lock(udp_media_mutex_);") -lt 0 -or
    $onData.IndexOf("if (!IsCurrentConnectionLocked(connection_generation))") -lt 0 -or
    $onData.IndexOf("ParseServerHello(root, connection_generation);") -lt 0) {
    $errors.Add("OnData must perform locked generation rechecks on all stateful branches")
}
$onDisconnected = Get-SourceSection $websocketSource `
    "websocket->OnDisconnected" 'ESP_LOGI(TAG, "Connecting' "OnDisconnected callback"
if ($onDisconnected.IndexOf("std::lock_guard<std::mutex> lock(udp_media_mutex_);") -lt 0 -or
    $onDisconnected.IndexOf("if (!IsCurrentConnectionLocked(connection_generation))") -lt 0) {
    $errors.Add("OnDisconnected must recheck generation while holding the state lock")
}
$openBody = Get-SourceSection $websocketSource `
    "bool WebsocketProtocol::OpenAudioChannel" "std::string WebsocketProtocol::GetHelloMessage" `
    "OpenAudioChannel"
if ($openBody.IndexOf("std::lock_guard<std::mutex> lock(udp_media_mutex_);") -lt 0 -or
    $openBody.IndexOf("ResetUdpStateLocked();") -lt 0) {
    $errors.Add("OpenAudioChannel must initialize transport state while locked")
}
$ownerSnapshot = $openBody.IndexOf("starting_udp_media = udp_media_;")
$startCall = $openBody.IndexOf("starting_udp_media->Start();", $ownerSnapshot)
$startRecheckLock = if ($startCall -ge 0) {
    $openBody.IndexOf("std::lock_guard<std::mutex> lock(udp_media_mutex_);", $startCall)
} else { -1 }
$startOwnerRecheck = if ($startRecheckLock -ge 0) {
    $openBody.IndexOf("udp_media_ == starting_udp_media", $startRecheckLock)
} else { -1 }
$startStop = if ($startOwnerRecheck -ge 0) {
    $openBody.IndexOf("starting_udp_media->RequestStop();", $startOwnerRecheck)
} else { -1 }
if ($ownerSnapshot -lt 0 -or $startCall -le $ownerSnapshot -or
    $startRecheckLock -le $startCall -or $startOwnerRecheck -le $startRecheckLock -or
    $startStop -le $startOwnerRecheck) {
    $errors.Add("UDP Start must run outside the owner lock and be followed by a locked generation/owner recheck")
}
$sendTextBody = Get-SourceSection $websocketSource `
    "bool WebsocketProtocol::SendText" "bool WebsocketProtocol::IsAudioChannelOpened" `
    "WebsocketProtocol::SendText"
$sendTextOwner = $sendTextBody.IndexOf("LoadWebsocketOwner(connection_generation)")
$sendTextGenerationCheck = $sendTextBody.IndexOf(
    "connection_generation_.load(std::memory_order_acquire)", $sendTextOwner)
$sendTextIo = $sendTextBody.IndexOf("websocket->Send(text)", $sendTextGenerationCheck)
if ($sendTextOwner -lt 0 -or $sendTextGenerationCheck -le $sendTextOwner -or
    $sendTextIo -le $sendTextGenerationCheck -or
    $sendTextBody.Contains("control_owner_mutex_")) {
    $errors.Add("control send must validate generation on a retained owner without holding its owner mutex")
}
$closeBody = Get-SourceSection $websocketSource `
    "void WebsocketProtocol::CloseAudioChannel" "bool WebsocketProtocol::OpenAudioChannel" `
    "WebsocketProtocol::CloseAudioChannel"
$closeMoveUdp = $closeBody.IndexOf("old_udp_media = std::move(udp_media_);")
$closeRevoke = $closeBody.IndexOf("old_udp_media->RequestStop();", $closeMoveUdp)
$closeControlLock = $closeBody.IndexOf(
    "std::lock_guard<std::mutex> lock(control_owner_mutex_);", $closeRevoke)
$closeResetOwners = $closeBody.IndexOf("old_udp_media.reset();", $closeControlLock)
if ($closeMoveUdp -lt 0 -or $closeRevoke -le $closeMoveUdp -or
    $closeControlLock -le $closeRevoke -or $closeResetOwners -le $closeControlLock) {
    $errors.Add("teardown must move UDP owner, revoke it, then move/reset control owner without nested locks")
}
$sendBody = Get-SourceSection $websocketSource `
    "bool WebsocketProtocol::SendAudio" "bool WebsocketProtocol::SendText" `
    "WebsocketProtocol::SendAudio"
$sendSnapshot = $sendBody.IndexOf("udp_media = udp_media_;")
$sendIo = $sendBody.IndexOf("udp_media->SendAudio", $sendSnapshot)
$sendRecheckLock = if ($sendIo -ge 0) {
    $sendBody.IndexOf("std::lock_guard<std::mutex> lock(udp_media_mutex_);", $sendIo)
} else { -1 }
if ($sendSnapshot -lt 0 -or $sendIo -le $sendSnapshot -or
    $sendRecheckLock -le $sendIo) {
    $errors.Add("UDP SendAudio must use a retained owner snapshot and perform I/O outside the state lock")
}

$datagramBody = Get-SourceSection $voiceSource `
    "void VoiceUdpMedia::HandleDatagram" "bool VoiceUdpMedia::AcceptSequence" "HandleDatagram"
$cheapCheck = $datagramBody.IndexOf("voice_udp_wire::ParseDatagram")
$firstReplay = $datagramBody.IndexOf("if (!AcceptSequence(sequence))")
$gcm = $datagramBody.IndexOf("mbedtls_gcm_auth_decrypt")
$secondReplay = if ($firstReplay -ge 0) {
    $datagramBody.IndexOf("if (!AcceptSequence(sequence))", $firstReplay + 1)
} else { -1 }
$advance = $datagramBody.IndexOf("AdvanceGenerationLocked(generation)")
$commit = $datagramBody.IndexOf("CommitSequence(sequence);")
if ($cheapCheck -lt 0 -or $firstReplay -le $cheapCheck -or $gcm -le $firstReplay -or
    $secondReplay -le $gcm -or $advance -le $secondReplay -or $commit -le $advance) {
    $errors.Add("UDP admission order must be cheap-check -> replay precheck -> GCM -> locked recheck -> generation -> commit")
}
$requestStopBody = Get-SourceSection $voiceSource `
    "void VoiceUdpMedia::RequestStop()" "void VoiceUdpMedia::Stop()" `
    "VoiceUdpMedia::RequestStop"
if (-not $requestStopBody.Contains("revoked_.exchange") -or
    -not $requestStopBody.Contains("kStopRequestedEvent") -or
    $requestStopBody.Contains("udp_.reset()") -or
    $requestStopBody.Contains("kTaskExitedEvent")) {
    $errors.Add("RequestStop must publish revocation and wake waiters without ingress reset or join")
}
$startBody = Get-SourceSection $voiceSource `
    "bool VoiceUdpMedia::Start()" "void VoiceUdpMedia::RequestStop()" `
    "VoiceUdpMedia::Start"
if (-not $startBody.Contains("kProbeAckEvent | kStopRequestedEvent") -or
    -not $startBody.Contains("revoked_.load(std::memory_order_acquire)")) {
    $errors.Add("UDP Start must observe revocation and wake from probe wait immediately")
}
$packetBody = Get-SourceSection $voiceSource `
    "bool VoiceUdpMedia::SendPacket" "void VoiceUdpMedia::HandleDatagram" `
    "VoiceUdpMedia::SendPacket"
$packetSendLock = $packetBody.IndexOf("std::lock_guard<std::mutex> send_lock(send_mutex_);")
$packetPreSendRevoke = $packetBody.IndexOf(
    "revoked_.load(std::memory_order_acquire)", $packetSendLock)
$packetIo = $packetBody.IndexOf("udp_->Send(send_buffer_)", $packetPreSendRevoke)
$packetPostSendRevoke = if ($packetIo -ge 0) {
    $packetBody.IndexOf("revoked_.load(std::memory_order_acquire)", $packetIo)
} else { -1 }
if ($packetSendLock -lt 0 -or $packetPreSendRevoke -le $packetSendLock -or
    $packetIo -le $packetPreSendRevoke -or $packetPostSendRevoke -le $packetIo) {
    $errors.Add("UDP send must serialize nonce state and fence revocation around lower-layer Send")
}
$stopBody = Get-SourceSection $voiceSource `
    "void VoiceUdpMedia::Stop()" "bool VoiceUdpMedia::SendAudio" "VoiceUdpMedia::Stop"
if ($stopBody.IndexOf("udp_.reset();") -gt $stopBody.IndexOf("kTaskExitedEvent")) {
    $errors.Add("UDP ingress owner must stop before the jitter consumer is joined")
}
if (-not $stopBody.Contains("cleanup_complete_") -or
    -not $stopBody.Contains("std::lock_guard<std::mutex> lifecycle_lock")) {
    $errors.Add("final UDP Stop must be idempotent and serialized")
}

$verifyIndex = $build.IndexOf("verify-source-contract.ps1")
$overlayIndex = $build.IndexOf("apply-overlay.ps1")
$managedIndex = $build.IndexOf("apply-managed-overlay.ps1")
$contractIndex = $build.IndexOf("test-udp-media-source-contract.ps1")
$idfBuildIndex = $build.IndexOf('"-DBOARD_TYPE=lichuang-dev" build')
if ($verifyIndex -lt 0 -or $overlayIndex -le $verifyIndex -or
    $managedIndex -le $overlayIndex -or $contractIndex -le $managedIndex -or
    $idfBuildIndex -le $contractIndex) {
    $errors.Add("build must inspect final sources after normal and managed overlays, before IDF compilation")
}

& (Join-Path $PSScriptRoot "test-udp-wire-fixtures.ps1") -Checkout $Checkout
if ($LASTEXITCODE -ne 0) { $errors.Add("C++ canonical wire fixture parser failed") }

# AES-GCM is a crypto-vector supplement; endpoint parser admission is exercised above in C++.
$positive = Get-Content -Raw -LiteralPath $positiveFixturePath | ConvertFrom-Json
if ($positive.header_bytes -ne 32 -or $positive.tag_bytes -ne 16 -or
    $positive.max_datagram_bytes -ne 1280 -or $positive.max_payload_bytes -ne 1200) {
    $errors.Add("canonical positive fixture limits do not match the endpoint profile")
}
foreach ($vector in $positive.vectors) {
    try {
        [byte[]]$key = ConvertFrom-Hex $vector.key_hex
        [byte[]]$salt = ConvertFrom-Hex $vector.salt_hex
        [byte[]]$headerBytes = ConvertFrom-Hex $vector.header_hex
        [byte[]]$payload = ConvertFrom-Hex $vector.payload_hex
        [byte[]]$expectedDatagram = ConvertFrom-Hex $vector.datagram_hex
        if ((Read-UInt32BigEndian $headerBytes 16) -ne [uint32]$vector.fields.sequence -or
            (Read-UInt32BigEndian $headerBytes 28) -ne [uint32]$payload.Length) {
            throw "header fields do not match fixture metadata"
        }
        [byte[]]$nonce = $salt + $headerBytes[16..19]
        if ((ConvertTo-Hex $nonce) -ne $vector.nonce_hex) { throw "nonce mismatch" }
        [byte[]]$ciphertext = [byte[]]::new($payload.Length)
        [byte[]]$tag = [byte[]]::new(16)
        $aes = [Security.Cryptography.AesGcm]::new($key, 16)
        try { $aes.Encrypt($nonce, $payload, $ciphertext, $tag, $headerBytes) }
        finally { $aes.Dispose() }
        if ((ConvertTo-Hex ($headerBytes + $ciphertext + $tag)) -ne
            (ConvertTo-Hex $expectedDatagram)) {
            throw "AES-GCM datagram mismatch"
        }
    } catch {
        $errors.Add("positive crypto fixture $($vector.id) failed: $($_.Exception.Message)")
    }
}

$negative = Get-Content -Raw -LiteralPath $negativeFixturePath | ConvertFrom-Json
foreach ($vector in $negative.vectors | Where-Object reject_stage -eq "authentication") {
    try {
        [byte[]]$datagram = ConvertFrom-Hex $vector.datagram_hex
        [byte[]]$key = ConvertFrom-Hex $negative.key_hex
        [byte[]]$salt = ConvertFrom-Hex $negative.salt_hex
        [byte[]]$aad = $datagram[0..31]
        [byte[]]$nonce = $salt + $datagram[16..19]
        $payloadLength = [int](Read-UInt32BigEndian $datagram 28)
        [byte[]]$ciphertext = $datagram[32..(31 + $payloadLength)]
        [byte[]]$tag = $datagram[(32 + $payloadLength)..(47 + $payloadLength)]
        [byte[]]$plaintext = [byte[]]::new($payloadLength)
        $authenticated = $true
        $aes = [Security.Cryptography.AesGcm]::new($key, 16)
        try {
            try { $aes.Decrypt($nonce, $ciphertext, $tag, $plaintext, $aad) }
            catch [Security.Cryptography.AuthenticationTagMismatchException] {
                $authenticated = $false
            }
        } finally { $aes.Dispose() }
        if ($authenticated) { throw "tampered datagram authenticated" }
    } catch {
        $errors.Add("negative crypto fixture $($vector.id) failed: $($_.Exception.Message)")
    }
}

if ($errors.Count -gt 0) {
    foreach ($errorMessage in $errors) { Write-Error $errorMessage }
    exit 1
}

Write-Host "UDP final-source, lifecycle, and canonical fixture contract passed."

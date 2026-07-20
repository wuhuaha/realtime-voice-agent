param([string]$Checkout = "")

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }
$Checkout = (Resolve-Path -LiteralPath $Checkout).Path

$application = Get-Content -Raw -LiteralPath (Join-Path $Checkout "main/application.cc")
$audioService = Get-Content -Raw -LiteralPath (Join-Path $Checkout "main/audio/audio_service.cc")
$board = Get-Content -Raw -LiteralPath (Join-Path $Checkout "main/boards/lichuang-dev/lichuang_dev_board.cc")

function Get-SourceBlock {
    param(
        [string]$Source,
        [string]$StartMarker,
        [string]$EndMarker,
        [string]$Description
    )
    $start = $Source.IndexOf($StartMarker)
    $end = $Source.IndexOf($EndMarker, $start + $StartMarker.Length)
    if ($start -lt 0 -or $end -le $start) {
        throw "UI control contract cannot locate $Description"
    }
    return $Source.Substring($start, $end - $start)
}

$requiredApplication = @(
    "constexpr int kMaxAudioPacketsPerPass = 4;",
    "packets_sent < kMaxAudioPacketsPerPass",
    "xEventGroupSetBits(event_group_, MAIN_EVENT_SEND_AUDIO);",
    "skip_audio_send = HandleToggleChatEvent();",
    "if ((bits & MAIN_EVENT_SEND_AUDIO) && !skip_audio_send)"
)
foreach ($pattern in $requiredApplication) {
    if (-not $application.Contains($pattern)) {
        throw "UI control contract missing application pattern: $pattern"
    }
}
$runBlock = Get-SourceBlock $application "void Application::Run()" `
    "void Application::HandleNetworkConnectedEvent" "Application::Run block"
$toggleIndex = $runBlock.IndexOf("if (bits & MAIN_EVENT_TOGGLE_CHAT)")
$audioIndex = $runBlock.IndexOf("if ((bits & MAIN_EVENT_SEND_AUDIO) && !skip_audio_send)")
$wakeIndex = $runBlock.IndexOf("if (bits & MAIN_EVENT_WAKE_WORD_DETECTED)")
if ($toggleIndex -lt 0 -or $audioIndex -lt 0 -or $wakeIndex -lt 0 -or
    $toggleIndex -gt $audioIndex -or $audioIndex -gt $wakeIndex) {
    throw "UI control contract requires toggle handling before the bounded audio dispatch"
}
$audioBlock = $runBlock.Substring($audioIndex, $wakeIndex - $audioIndex)
if ($audioBlock.Contains("while (auto packet")) {
    throw "UI control contract rejects unbounded audio queue draining"
}
$toggleBlock = Get-SourceBlock $application "bool Application::HandleToggleChatEvent()" `
    "void Application::ContinueOpenAudioChannel" "HandleToggleChatEvent block"
foreach ($pattern in @(
    "state == kDeviceStateListening",
    "audio_service_.EnableVoiceProcessing(false);",
    "audio_service_.DiscardPendingUplink();",
    "protocol_->CloseAudioChannel();",
    "return true;"
)) {
    if (-not $toggleBlock.Contains($pattern)) {
        throw "UI control contract missing listening-stop pattern: $pattern"
    }
}
$disableIndex = $toggleBlock.IndexOf("audio_service_.EnableVoiceProcessing(false);")
$discardIndex = $toggleBlock.IndexOf("audio_service_.DiscardPendingUplink();")
$closeIndex = $toggleBlock.IndexOf("protocol_->CloseAudioChannel();")
if ($disableIndex -lt 0 -or $discardIndex -le $disableIndex -or $closeIndex -le $discardIndex) {
    throw "UI control contract requires stop-capture -> discard-uplink -> close-channel ordering"
}

$discardBlock = Get-SourceBlock $audioService "void AudioService::DiscardPendingUplink()" `
    "void AudioService::EnableAudioTesting" "DiscardPendingUplink block"
foreach ($pattern in @(
    "uplink_accepting_ = false;",
    "++uplink_generation_;",
    "audio_send_queue_.clear();",
    "timestamp_queue_.clear();",
    "audio_encode_queue_.erase("
)) {
    if (-not $discardBlock.Contains($pattern)) {
        throw "UI control contract missing stale-uplink fence pattern: $pattern"
    }
}
$voiceProcessingBlock = Get-SourceBlock $audioService "void AudioService::EnableVoiceProcessing(bool enable)" `
    "void AudioService::DiscardPendingUplink()" "EnableVoiceProcessing block"
foreach ($pattern in @(
    "uplink_accepting_ = true;",
    "audio_processor_->Stop();"
)) {
    if (-not $voiceProcessingBlock.Contains($pattern)) {
        throw "UI control contract missing voice-processing lifecycle pattern: $pattern"
    }
}
$codecBlock = Get-SourceBlock $audioService "void AudioService::OpusCodecTask()" `
    "void AudioService::SetDecodeSampleRate" "OpusCodecTask block"
if (-not $codecBlock.Contains("task->uplink_generation == uplink_generation_")) {
    throw "UI control contract missing in-flight encoder generation fence"
}
$enqueueBlock = Get-SourceBlock $audioService "void AudioService::PushTaskToEncodeQueue" `
    "bool AudioService::PushPacketToDecodeQueue" "PushTaskToEncodeQueue block"
foreach ($pattern in @("if (!uplink_accepting_)", "task->uplink_generation = uplink_generation_;")) {
    if (-not $enqueueBlock.Contains($pattern)) {
        throw "UI control contract missing uplink admission pattern: $pattern"
    }
}
if (-not $board.Contains('lv_label_set_text(assistant_role, "AI");')) {
    throw "UI control contract missing AI assistant label"
}
if ($board.Contains('lv_label_set_text(assistant_role, "小智");')) {
    throw "UI control contract retains the deprecated assistant label"
}
$actionBlock = Get-SourceBlock $board "action_button_ = lv_button_create(container_);" `
    "action_icon_ = lv_label_create(action_button_);" "microphone action callback"
if (-not $actionBlock.Contains("Application::GetInstance().ToggleChatState();")) {
    throw "UI control contract missing microphone action routing"
}

Write-Host "UI branding and bounded control-event responsiveness contract passed."

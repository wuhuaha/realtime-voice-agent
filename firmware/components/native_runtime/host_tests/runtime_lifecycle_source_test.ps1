$ErrorActionPreference = "Stop"

$runtimeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$source = Get-Content -Raw (Join-Path $runtimeRoot "voice_runtime.cc")
$header = Get-Content -Raw (Join-Path $runtimeRoot "include/native_runtime/voice_runtime.h")

function Get-SourceSection {
    param(
        [string]$Text,
        [string]$StartMarker,
        [string]$EndMarker
    )
    $start = $Text.IndexOf($StartMarker)
    if ($start -lt 0) { throw "Missing source marker: $StartMarker" }
    $end = $Text.IndexOf($EndMarker, $start + $StartMarker.Length)
    if ($end -lt 0) { throw "Missing source marker: $EndMarker" }
    return $Text.Substring($start, $end - $start)
}

function Assert-Contains {
    param(
        [string]$Text,
        [string]$Expected,
        [string]$Failure
    )
    if (!$Text.Contains($Expected)) { throw $Failure }
}

if ($source.Contains("websocket_event_signal_") -or $header.Contains("websocket_event_signal_")) {
    throw "The callback-only signal name must not survive the supervisor work-signal migration"
}
Assert-Contains $header "SemaphoreHandle_t supervisor_work_signal_ = nullptr;" `
    "VoiceRuntime must own the coalescing supervisor work signal"
Assert-Contains $header "std::atomic<bool> websocket_started_{false};" `
    "VoiceRuntime must distinguish a started client from an allocated owner"

$startSection = Get-SourceSection $source "bool VoiceRuntime::Start(" "void VoiceRuntime::Stop()"
$ownerStart = $startSection.IndexOf("owner_->Start()")
if ($ownerStart -lt 0) { throw "VoiceRuntime::Start must start the WSS owner" }
$preOwnerStart = $startSection.Substring(0, $ownerStart)
if ($preOwnerStart.Contains("CloseWebsocketBounded")) {
    throw "Pre-Start cleanup must not allocate the bounded teardown semaphore/task"
}
$synchronousCloses = [regex]::Matches(
    $preOwnerStart,
    [regex]::Escape("owner_->SupervisorClose(0)"))
if ($synchronousCloses.Count -ne 2) {
    throw "Semaphore and event-group allocation failures must both synchronously destroy the unstarted owner"
}
Assert-Contains $startSection "const bool owner_started = tasks_started && owner_->Start();" `
    "WebSocket start state must be captured without starting after supervisor allocation failure"
Assert-Contains $startSection "websocket_started_.store(owner_started" `
    "VoiceRuntime must retain whether native WebSocket callbacks can exist"

$stopSection = Get-SourceSection $source "void VoiceRuntime::Stop()" "void VoiceRuntime::SupervisorTask("
Assert-Contains $stopSection "xSemaphoreGive(supervisor_work_signal_)" `
    "Stop must wake a blocked supervisor"
Assert-Contains $stopSection "websocket_started_.exchange(false" `
    "Stop must consume the native WebSocket lifecycle state exactly once"
$closePattern = "(?s)websocket_was_started\s*\?\s*CloseWebsocketBounded\(kWebsocketTeardownTimeoutMs\)" +
                ".*:\s*owner_->SupervisorClose\(0\)"
if ($stopSection -notmatch $closePattern) {
    throw "An owner that never started must be destroyed synchronously without teardown allocations"
}

$notifySection = Get-SourceSection `
    $source "void VoiceRuntime::NotifySupervisorWork(" "bool VoiceRuntime::CloseWebsocketBounded("
Assert-Contains $notifySection "xSemaphoreGive(runtime->supervisor_work_signal_)" `
    "WSS callbacks must wake the supervisor"

$publishSection = Get-SourceSection `
    $source "bool VoiceRuntime::PublishPlaybackFact(" "bool VoiceRuntime::DrainPlaybackFacts("
$publishPattern = "(?s)xQueueSend\(playback_fact_queue_, &queued, 0\) != pdTRUE.*" +
                  "xSemaphoreGive\(supervisor_work_signal_\).*return true;"
if ($publishSection -notmatch $publishPattern) {
    throw "Successful playback fact enqueue must wake the supervisor before returning"
}

$supervisorSection = Get-SourceSection `
    $source "void VoiceRuntime::RunSupervisor()" "void VoiceRuntime::HandleControl("
Assert-Contains $supervisorSection "xSemaphoreTake(supervisor_work_signal_" `
    "Supervisor idle wait must consume the shared work signal"

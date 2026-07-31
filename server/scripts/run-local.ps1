[CmdletBinding()]
param(
    [ValidateRange(1, 32)]
    [int]$WorkerCount = 2,
    [string]$RuntimeDirectory,
    [string]$EnvironmentFile,
    [ValidateRange(0, 65535)]
    [int]$DirectorPort = 0,
    [ValidateRange(0, 65535)]
    [int]$WorkerBasePort = 0,
    [ValidateRange(0, 65535)]
    [int]$UdpBasePort = 0,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$ServerRoot = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path -Parent $ServerRoot
if ([string]::IsNullOrWhiteSpace($RuntimeDirectory)) {
    $RuntimeDirectory = Join-Path $RepoRoot '.runtime/local'
}
$RuntimeDirectory = [System.IO.Path]::GetFullPath($RuntimeDirectory)
$PidFile = Join-Path $RuntimeDirectory 'server-processes.json'
$SupervisorScript = Join-Path $PSScriptRoot 'windows_job_supervisor.py'
if ([string]::IsNullOrWhiteSpace($EnvironmentFile)) {
    $EnvironmentFile = Join-Path $RepoRoot '.env'
}
$EnvironmentFile = [System.IO.Path]::GetFullPath($EnvironmentFile)
$Python = Join-Path $ServerRoot '.venv/Scripts/python.exe'
$SupervisorPython = $null
if (Test-Path -LiteralPath $Python) {
    try {
        $SupervisorPythonOutput = @(& $Python -c 'import os, sys; print(os.path.realpath(sys._base_executable))')
        if ($LASTEXITCODE -eq 0 -and $SupervisorPythonOutput.Count -eq 1) {
            $SupervisorPython = [System.IO.Path]::GetFullPath([string]$SupervisorPythonOutput[0])
        }
    }
    catch {
        $SupervisorPython = $null
    }
}

function Resolve-RecordedStartTimeUtcTicks {
    param(
        [string]$StartTimeUtcTicks,
        [object]$StartTimeUtc
    )
    $ticks = 0L
    if (-not [string]::IsNullOrWhiteSpace($StartTimeUtcTicks) -and
        [long]::TryParse($StartTimeUtcTicks, [ref]$ticks) -and $ticks -gt 0) {
        return $ticks
    }
    if ($StartTimeUtc -is [DateTimeOffset]) {
        return $StartTimeUtc.UtcDateTime.Ticks
    }
    if ($StartTimeUtc -is [DateTime]) {
        return $StartTimeUtc.ToUniversalTime().Ticks
    }
    $startTimeText = [string]$StartTimeUtc
    $timestamp = [DateTimeOffset]::MinValue
    if (-not [string]::IsNullOrWhiteSpace($startTimeText) -and
        [DateTimeOffset]::TryParse(
            $startTimeText,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind,
            [ref]$timestamp
        )) {
        return $timestamp.UtcDateTime.Ticks
    }
    $localTimestamp = [DateTime]::MinValue
    if (-not [string]::IsNullOrWhiteSpace($startTimeText) -and
        [DateTime]::TryParse(
            $startTimeText,
            [Globalization.CultureInfo]::CurrentCulture,
            [Globalization.DateTimeStyles]::AssumeLocal,
            [ref]$localTimestamp
        )) {
        return $localTimestamp.ToUniversalTime().Ticks
    }
    return $null
}

function Get-RecordedProcessStatus {
    param(
        [int]$ProcessId,
        [string]$StartTimeUtcTicks,
        [object]$StartTimeUtc,
        [int]$StartTimeToleranceTicks = 0,
        [string]$Executable
    )
    if ($ProcessId -le 0) {
        return [pscustomobject]@{ state = 'missing'; process = $null }
    }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return [pscustomobject]@{ state = 'missing'; process = $null }
    }
    if ($process.HasExited) {
        return [pscustomobject]@{ state = 'missing'; process = $null }
    }
    try {
        $expectedTicks = Resolve-RecordedStartTimeUtcTicks `
            -StartTimeUtcTicks $StartTimeUtcTicks `
            -StartTimeUtc $StartTimeUtc
        $startMatches = $null -ne $expectedTicks -and `
            [Math]::Abs($process.StartTime.ToUniversalTime().Ticks - $expectedTicks) -le $StartTimeToleranceTicks
        $executableMatches = [string]::Equals(
            [System.IO.Path]::GetFullPath($process.Path),
            [System.IO.Path]::GetFullPath($Executable),
            [StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        $startMatches = $false
        $executableMatches = $false
    }
    if (-not $startMatches -or -not $executableMatches) {
        return [pscustomobject]@{ state = 'mismatch'; process = $process }
    }
    return [pscustomobject]@{ state = 'match'; process = $process }
}

function Invoke-VerifiedTermination {
    param(
        [int]$ProcessId,
        [string]$StartTimeUtcTicks,
        [object]$StartTimeUtc,
        [int]$StartTimeToleranceTicks = 0,
        [string]$Executable,
        [ValidateRange(1, 5000)]
        [int]$TimeoutMilliseconds = 5000
    )
    $expectedTicks = Resolve-RecordedStartTimeUtcTicks `
        -StartTimeUtcTicks $StartTimeUtcTicks `
        -StartTimeUtc $StartTimeUtc
    if ($null -eq $expectedTicks -or [string]::IsNullOrWhiteSpace($Executable)) {
        return [pscustomobject]@{ state = 'mismatch'; process_id = $ProcessId; detail = 'invalid recorded identity' }
    }
    if ([string]::IsNullOrWhiteSpace($SupervisorPython) -or -not (Test-Path -LiteralPath $SupervisorPython)) {
        Write-Warning "Cannot verify PID $ProcessId because the server Python environment is missing."
        return [pscustomobject]@{ state = 'error'; process_id = $ProcessId; detail = 'helper Python missing' }
    }
    if (-not (Test-Path -LiteralPath $SupervisorScript)) {
        Write-Warning "Cannot verify PID $ProcessId because the process identity helper is missing."
        return [pscustomobject]@{ state = 'error'; process_id = $ProcessId; detail = 'identity helper missing' }
    }
    try {
        $raw = @(
            & $SupervisorPython $SupervisorScript `
                --terminate-pid $ProcessId `
                --start-time-utc-ticks ([string]$expectedTicks) `
                --start-time-tolerance-ticks $StartTimeToleranceTicks `
                --executable $Executable `
                --timeout-ms $TimeoutMilliseconds 2>&1
        )
        $nativeExitCode = $LASTEXITCODE
        if ($raw.Count -eq 0) {
            throw "identity helper returned no result (exit $nativeExitCode)"
        }
        $jsonResult = @($raw | Where-Object { ([string]$_).TrimStart().StartsWith('{') } | Select-Object -Last 1)
        if ($jsonResult.Count -ne 1) {
            throw "identity helper returned no structured result (exit $nativeExitCode): $($raw -join ' ')"
        }
        $result = [string]$jsonResult[0] | ConvertFrom-Json
        if ($nativeExitCode -ne 0 -and $result.state -notin @('mismatch', 'timeout', 'error')) {
            throw "identity helper failed with exit $nativeExitCode"
        }
        return $result
    }
    catch {
        Write-Warning "Verified termination failed for PID $ProcessId`: $($_.Exception.Message)"
        return [pscustomobject]@{ state = 'error'; process_id = $ProcessId; detail = $_.Exception.Message }
    }
}

function ConvertTo-RecoverableLegacyEntry {
    param([object]$Entry)
    $ticks = Resolve-RecordedStartTimeUtcTicks `
        -StartTimeUtcTicks ([string]$Entry.start_time_utc_ticks) `
        -StartTimeUtc ($Entry.start_time_utc)
    if ($null -eq $ticks) {
        return $Entry
    }
    $startTimeUtc = [string]$Entry.start_time_utc
    if ([string]::IsNullOrWhiteSpace($startTimeUtc)) {
        $startTimeUtc = [DateTime]::new($ticks, [DateTimeKind]::Utc).ToString('O')
    }
    $normalized = [ordered]@{
        name = [string]$Entry.name
        pid = [int]$Entry.pid
        start_time_utc = $startTimeUtc
        start_time_utc_ticks = [string]$ticks
        executable = [System.IO.Path]::GetFullPath([string]$Entry.executable)
        job_managed = $false
    }
    if ($Entry.PSObject.Properties.Name -contains 'start_time_tolerance_ticks') {
        $normalized.start_time_tolerance_ticks = [int]$Entry.start_time_tolerance_ticks
    }
    return [pscustomobject]$normalized
}

function Get-LegacyIdentityKey {
    param([object]$Identity)
    $ticks = Resolve-RecordedStartTimeUtcTicks `
        -StartTimeUtcTicks ([string]$Identity.start_time_utc_ticks) `
        -StartTimeUtc ($Identity.start_time_utc)
    if ($null -eq $ticks -or [string]::IsNullOrWhiteSpace([string]$Identity.executable)) {
        return $null
    }
    $executable = [System.IO.Path]::GetFullPath([string]$Identity.executable)
    return "$([int]$Identity.pid):${ticks}:$executable"
}

function Get-LegacyRemainingTimeoutMilliseconds {
    param([DateTimeOffset]$Deadline, [int]$Maximum = 5000)
    $remaining = [long][Math]::Floor(($Deadline - [DateTimeOffset]::UtcNow).TotalMilliseconds)
    if ($remaining -le 0) {
        return 0
    }
    return [int][Math]::Min([long]$Maximum, $remaining)
}

function Get-LegacyExactProcessGuard {
    param([object]$Identity)
    $identityKey = Get-LegacyIdentityKey -Identity $Identity
    if ($null -eq $identityKey) {
        return [pscustomobject]@{
            state = 'error'
            process = $null
            handle = $null
            identity_key = $null
            detail = 'invalid recorded identity'
        }
    }

    $processId = [int]$Identity.pid
    if ($processId -le 0) {
        return [pscustomobject]@{
            state = 'error'
            process = $null
            handle = $null
            identity_key = $identityKey
            detail = 'invalid recorded PID'
        }
    }

    $process = $null
    try {
        $process = [System.Diagnostics.Process]::GetProcessById($processId)
        $handle = $process.Handle
        if ($process.HasExited) {
            return [pscustomobject]@{
                state = 'missing'
                process = $process
                handle = $handle
                identity_key = $identityKey
            }
        }

        $expectedTicks = Resolve-RecordedStartTimeUtcTicks `
            -StartTimeUtcTicks ([string]$Identity.start_time_utc_ticks) `
            -StartTimeUtc ($Identity.start_time_utc)
        $expectedExecutable = [string]$Identity.executable
        if ($null -eq $expectedTicks -or [string]::IsNullOrWhiteSpace($expectedExecutable)) {
            return [pscustomobject]@{
                state = 'error'
                process = $process
                handle = $handle
                identity_key = $identityKey
                detail = 'invalid recorded identity'
            }
        }
        $toleranceTicks = if ($Identity.PSObject.Properties.Name -contains 'start_time_tolerance_ticks') {
            [int]$Identity.start_time_tolerance_ticks
        }
        else {
            0
        }
        $startMatches = [Math]::Abs(
            $process.StartTime.ToUniversalTime().Ticks - $expectedTicks
        ) -le $toleranceTicks
        $executableMatches = [string]::Equals(
            [System.IO.Path]::GetFullPath($process.Path),
            [System.IO.Path]::GetFullPath($expectedExecutable),
            [StringComparison]::OrdinalIgnoreCase
        )
        $state = if ($startMatches -and $executableMatches) { 'match' } else { 'mismatch' }
        return [pscustomobject]@{
            state = $state
            process = $process
            handle = $handle
            identity_key = $identityKey
        }
    }
    catch [ArgumentException] {
        return [pscustomobject]@{
            state = 'missing'
            process = $process
            handle = $null
            identity_key = $identityKey
        }
    }
    catch {
        return [pscustomobject]@{
            state = 'error'
            process = $process
            handle = $null
            identity_key = $identityKey
            detail = $_.Exception.Message
        }
    }
}

function Close-LegacyExactProcessGuard {
    param([object]$Guard)
    if ($null -eq $Guard -or $null -eq $Guard.process) {
        return
    }
    try {
        $Guard.process.Dispose()
    }
    catch {
        Write-Warning "Cannot close legacy process guard: $($_.Exception.Message)"
    }
}

function Get-LegacyChildIdentityCapture {
    param(
        [object]$ParentIdentity,
        [object]$CreatedNotAfterTicks,
        [DateTimeOffset]$Deadline,
        [string]$RootName,
        [object]$ParentGuard = $null
    )
    $identities = [System.Collections.Generic.List[object]]::new()
    if ($null -eq $CreatedNotAfterTicks) {
        $identityKey = Get-LegacyIdentityKey -Identity $ParentIdentity
        $guardMatches = $null -ne $ParentGuard -and
            $ParentGuard.state -eq 'match' -and
            $null -ne $ParentGuard.process -and
            [string]::Equals(
                [string]$ParentGuard.identity_key,
                [string]$identityKey,
                [StringComparison]::OrdinalIgnoreCase
            )
        if (-not $guardMatches) {
            Write-Warning "Cannot enumerate unbounded descendants of PID $($ParentIdentity.pid) without an exact process guard."
            return [pscustomobject]@{ state = 'error'; identities = @() }
        }
    }
    $parentTicks = Resolve-RecordedStartTimeUtcTicks `
        -StartTimeUtcTicks ([string]$ParentIdentity.start_time_utc_ticks) `
        -StartTimeUtc ($ParentIdentity.start_time_utc)
    if ($null -eq $parentTicks) {
        return [pscustomobject]@{ state = 'error'; identities = @() }
    }

    $remainingMilliseconds = Get-LegacyRemainingTimeoutMilliseconds -Deadline $Deadline
    if ($remainingMilliseconds -le 0) {
        return [pscustomobject]@{ state = 'deadline'; identities = @() }
    }
    $operationTimeoutSeconds = [uint][Math]::Max(
        1.0,
        [Math]::Ceiling($remainingMilliseconds / 1000.0)
    )
    try {
        $children = Get-CimInstance Win32_Process `
            -Filter "ParentProcessId = $([int]$ParentIdentity.pid)" `
            -OperationTimeoutSec $operationTimeoutSeconds `
            -ErrorAction Stop
    }
    catch {
        Write-Warning "Cannot enumerate legacy descendants of PID $($ParentIdentity.pid): $($_.Exception.Message)"
        return [pscustomobject]@{ state = 'error'; identities = @() }
    }

    $captureFailed = $false
    foreach ($child in @($children)) {
        try {
            $childId = [int]$child.ProcessId
            if ($childId -le 0 -or $childId -eq [int]$ParentIdentity.pid) {
                throw "invalid child PID"
            }
            $created = ([DateTime]$child.CreationDate).ToUniversalTime()
            if ($created.Ticks -lt $parentTicks) {
                continue
            }
            if ($null -ne $CreatedNotAfterTicks -and $created.Ticks -gt [long]$CreatedNotAfterTicks) {
                continue
            }
            $executable = [string]$child.ExecutablePath
            if ([string]::IsNullOrWhiteSpace($executable)) {
                throw "CIM did not expose ExecutablePath"
            }
            $identities.Add([pscustomobject]@{
                name = "$RootName-descendant-$childId"
                pid = $childId
                start_time_utc = $created.ToString('O')
                start_time_utc_ticks = [string]$created.Ticks
                start_time_tolerance_ticks = 10
                executable = [System.IO.Path]::GetFullPath($executable)
                job_managed = $false
            })
        }
        catch {
            $captureFailed = $true
            Write-Warning "Cannot capture the exact identity of legacy descendant PID $($child.ProcessId)."
        }
    }
    $captureState = if ($captureFailed) { 'error' } else { 'success' }
    return [pscustomobject]@{ state = $captureState; identities = @($identities) }
}

function Add-LegacyIdentityState {
    param(
        [object]$Identity,
        [object]$States,
        [object]$StateByKey,
        [bool]$Terminated = $false,
        [object]$TerminationUtcTicks = $null
    )
    $key = Get-LegacyIdentityKey -Identity $Identity
    if ($null -eq $key) {
        return $null
    }
    if ($StateByKey.ContainsKey($key)) {
        $existing = $StateByKey[$key]
        $candidateTicks = 0L
        if ($Terminated -and
            $null -ne $TerminationUtcTicks -and
            [long]::TryParse([string]$TerminationUtcTicks, [ref]$candidateTicks)) {
            $existingTicks = 0L
            $hasExistingTicks = $null -ne $existing.termination_utc_ticks -and
                [long]::TryParse([string]$existing.termination_utc_ticks, [ref]$existingTicks)
            if (-not $existing.terminated -or -not $hasExistingTicks -or $candidateTicks -gt $existingTicks) {
                $existing.terminated = $true
                $existing.termination_utc_ticks = $candidateTicks
                $existing.failed = $false
                $existing.retain_marker = $false
            }
        }
        return $null
    }
    $state = [pscustomobject]@{
        key = $key
        identity = $Identity
        terminated = $Terminated
        termination_utc_ticks = $TerminationUtcTicks
        failed = $false
        retain_marker = $false
    }
    $States.Add($state)
    $StateByKey.Add($key, $state)
    return $state
}

function ConvertTo-LegacyScanMarker {
    param([object]$Identity, [object]$TerminationUtcTicks)
    $marker = ConvertTo-RecoverableLegacyEntry -Entry $Identity
    $marker | Add-Member `
        -NotePropertyName legacy_descendant_scan_incomplete `
        -NotePropertyValue $true `
        -Force
    if ($null -ne $TerminationUtcTicks) {
        $marker | Add-Member `
            -NotePropertyName legacy_termination_utc_ticks `
            -NotePropertyValue ([string]$TerminationUtcTicks) `
            -Force
    }
    return $marker
}

function Get-LegacyRecoveryEntries {
    param(
        [object]$States,
        [object]$Passthrough,
        [switch]$IncludeInProgress
    )
    $entries = [System.Collections.Generic.List[object]]::new()
    $keys = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in @($Passthrough)) {
        $key = Get-LegacyIdentityKey -Identity $entry
        if ($null -eq $key -or $keys.Add($key)) {
            $entries.Add($entry)
        }
    }
    foreach ($state in @($States)) {
        if (-not $IncludeInProgress -and $state.terminated -and -not $state.retain_marker) {
            continue
        }
        $marker = ConvertTo-LegacyScanMarker `
            -Identity $state.identity `
            -TerminationUtcTicks $(if ($state.terminated) { $state.termination_utc_ticks } else { $null })
        $markerKey = Get-LegacyIdentityKey -Identity $marker
        if ($null -ne $markerKey -and $keys.Add($markerKey)) {
            $entries.Add($marker)
        }
    }
    return @($entries)
}

function Publish-LegacyRecoveryProgress {
    param(
        [object]$States,
        [object]$Passthrough,
        [scriptblock]$ProgressCallback,
        [switch]$IncludeInProgress
    )
    if ($null -eq $ProgressCallback) {
        return
    }
    $recoveryEntries = Get-LegacyRecoveryEntries `
        -States $States `
        -Passthrough $Passthrough `
        -IncludeInProgress:$IncludeInProgress
    & $ProgressCallback -RecoveryEntries @($recoveryEntries)
}

function Publish-LegacyRecoveryProgressAfterBound {
    param(
        [object]$States,
        [object]$Passthrough,
        [scriptblock]$ProgressCallback,
        [ref]$DeferredError,
        [switch]$IncludeInProgress
    )
    try {
        Publish-LegacyRecoveryProgress `
            -States $States `
            -Passthrough $Passthrough `
            -ProgressCallback $ProgressCallback `
            -IncludeInProgress:$IncludeInProgress
    }
    catch {
        $DeferredError.Value = $_
        Write-Warning (
            "Deferred a legacy recovery journal error after a safe termination bound: " +
            $_.Exception.Message
        )
    }
}

function Stop-LegacyRecordedProcessTrees {
    param(
        [object[]]$Entries,
        [scriptblock]$ProgressCallback = $null
    )
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
    $states = [System.Collections.Generic.List[object]]::new()
    $stateByKey = [System.Collections.Generic.Dictionary[string, object]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $passthrough = [System.Collections.Generic.List[object]]::new()
    $passthroughKeys = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $deferredJournalError = $null

    foreach ($entry in @($Entries)) {
        $isRecoveryMarker = $entry.PSObject.Properties.Name -contains 'legacy_descendant_scan_incomplete'
        $entryTicks = Resolve-RecordedStartTimeUtcTicks `
            -StartTimeUtcTicks ([string]$entry.start_time_utc_ticks) `
            -StartTimeUtc ($entry.start_time_utc)
        $markerTerminationTicks = 0L
        $hasTerminationBound = $isRecoveryMarker -and
            $entry.PSObject.Properties.Name -contains 'legacy_termination_utc_ticks' -and
            [long]::TryParse(
                [string]$entry.legacy_termination_utc_ticks,
                [ref]$markerTerminationTicks
            ) -and
            $null -ne $entryTicks -and
            $markerTerminationTicks -ge $entryTicks
        $identity = ConvertTo-RecoverableLegacyEntry -Entry $entry
        if ($hasTerminationBound) {
            [void](Add-LegacyIdentityState `
                -Identity $identity `
                -States $states `
                -StateByKey $stateByKey `
                -Terminated $true `
                -TerminationUtcTicks $markerTerminationTicks)
            continue
        }

        $state = Add-LegacyIdentityState `
            -Identity $identity `
            -States $states `
            -StateByKey $stateByKey
        if ($null -eq $state) {
            $key = Get-LegacyIdentityKey -Identity $identity
            if ($null -ne $key -and $stateByKey.ContainsKey($key)) {
                continue
            }
            $marker = ConvertTo-LegacyScanMarker `
                -Identity $identity `
                -TerminationUtcTicks $null
            $markerKey = Get-LegacyIdentityKey -Identity $marker
            if ($null -eq $markerKey -or $passthroughKeys.Add($markerKey)) {
                $passthrough.Add($marker)
            }
            continue
        }
        Write-Warning "Stopping legacy manifest entry $($entry.name); restart it to obtain Job Object lifecycle guarantees."
    }

    if ($states.Count -gt 0 -or $passthrough.Count -gt 0) {
        Publish-LegacyRecoveryProgress `
            -States $states `
            -Passthrough $passthrough `
            -ProgressCallback $ProgressCallback `
            -IncludeInProgress
    }

    $madeProgress = $true
    $deadlineExceeded = $false
    while ($madeProgress) {
        $madeProgress = $false
        foreach ($state in @($states)) {
            if ([DateTimeOffset]::UtcNow -ge $deadline) {
                $deadlineExceeded = $true
                break
            }
            if ($state.terminated) {
                if ($null -eq $state.termination_utc_ticks) {
                    $state.retain_marker = $true
                    continue
                }
                $capture = Get-LegacyChildIdentityCapture `
                    -ParentIdentity $state.identity `
                    -CreatedNotAfterTicks $state.termination_utc_ticks `
                    -Deadline $deadline `
                    -RootName ([string]$state.identity.name)
                foreach ($identity in @($capture.identities)) {
                    $added = Add-LegacyIdentityState `
                        -Identity $identity `
                        -States $states `
                        -StateByKey $stateByKey
                    if ($null -ne $added) {
                        $madeProgress = $true
                    }
                    if ($null -ne $added) {
                        Publish-LegacyRecoveryProgress `
                            -States $states `
                            -Passthrough $passthrough `
                            -ProgressCallback $ProgressCallback `
                            -IncludeInProgress
                    }
                }
                if ($capture.state -eq 'success') {
                    $state.retain_marker = $false
                }
                else {
                    $state.retain_marker = $true
                    Publish-LegacyRecoveryProgressAfterBound `
                        -States $states `
                        -Passthrough $passthrough `
                        -ProgressCallback $ProgressCallback `
                        -DeferredError ([ref]$deferredJournalError) `
                        -IncludeInProgress
                }
                continue
            }
            if ($state.failed) {
                continue
            }

            $guard = $null
            try {
                $guard = Get-LegacyExactProcessGuard -Identity $state.identity
                if ($guard.state -ne 'match') {
                    if ($guard.state -eq 'missing') {
                        Write-Warning "Legacy PID $($state.identity.pid) is absent without a safe descendant-scan bound."
                    }
                    else {
                        Write-Warning "Legacy PID $($state.identity.pid) no longer has a verifiable recorded identity."
                    }
                    $state.failed = $true
                    $state.retain_marker = $true
                    Publish-LegacyRecoveryProgress `
                        -States $states `
                        -Passthrough $passthrough `
                        -ProgressCallback $ProgressCallback `
                        -IncludeInProgress
                    continue
                }

            $beforeTermination = Get-LegacyChildIdentityCapture `
                -ParentIdentity $state.identity `
                -CreatedNotAfterTicks $null `
                -Deadline $deadline `
                -RootName ([string]$state.identity.name) `
                -ParentGuard $guard
            if ($beforeTermination.state -ne 'success') {
                $state.failed = $true
                $state.retain_marker = $true
                Publish-LegacyRecoveryProgress `
                    -States $states `
                    -Passthrough $passthrough `
                    -ProgressCallback $ProgressCallback `
                    -IncludeInProgress
                continue
            }
            $capturedBeforeTermination = $false
            foreach ($identity in @($beforeTermination.identities)) {
                $added = Add-LegacyIdentityState `
                    -Identity $identity `
                    -States $states `
                    -StateByKey $stateByKey
                if ($null -ne $added) {
                    $capturedBeforeTermination = $true
                    $madeProgress = $true
                }
            }
            if ($capturedBeforeTermination) {
                Publish-LegacyRecoveryProgress `
                    -States $states `
                    -Passthrough $passthrough `
                    -ProgressCallback $ProgressCallback `
                    -IncludeInProgress
            }

            $remainingMilliseconds = Get-LegacyRemainingTimeoutMilliseconds -Deadline $deadline
            if ($remainingMilliseconds -le 0) {
                $deadlineExceeded = $true
                break
            }
            $termination = Invoke-VerifiedTermination `
                -ProcessId ([int]$state.identity.pid) `
                -StartTimeUtcTicks ([string]$state.identity.start_time_utc_ticks) `
                -StartTimeUtc ($state.identity.start_time_utc) `
                -StartTimeToleranceTicks ([int]$state.identity.start_time_tolerance_ticks) `
                -Executable ([string]$state.identity.executable) `
                -TimeoutMilliseconds $remainingMilliseconds
            $terminationBound = if ($termination.state -eq 'missing') {
                [string]$termination.process_absent_utc_ticks
            }
            elseif ($termination.state -eq 'terminated') {
                [string]$termination.termination_utc_ticks
            }
            else {
                $null
            }
            $identityTicks = Resolve-RecordedStartTimeUtcTicks `
                -StartTimeUtcTicks ([string]$state.identity.start_time_utc_ticks) `
                -StartTimeUtc ($state.identity.start_time_utc)
            $terminationTicks = 0L
            if ($null -eq $terminationBound -or
                -not [long]::TryParse($terminationBound, [ref]$terminationTicks) -or
                $null -eq $identityTicks -or
                $terminationTicks -lt $identityTicks) {
                $state.failed = $true
                $state.retain_marker = $true
                Publish-LegacyRecoveryProgress `
                    -States $states `
                    -Passthrough $passthrough `
                    -ProgressCallback $ProgressCallback `
                    -IncludeInProgress
                continue
            }

            $state.terminated = $true
            $state.termination_utc_ticks = $terminationTicks
            $madeProgress = $true
            Publish-LegacyRecoveryProgressAfterBound `
                -States $states `
                -Passthrough $passthrough `
                -ProgressCallback $ProgressCallback `
                -DeferredError ([ref]$deferredJournalError) `
                -IncludeInProgress

            $afterTermination = Get-LegacyChildIdentityCapture `
                -ParentIdentity $state.identity `
                -CreatedNotAfterTicks $state.termination_utc_ticks `
                -Deadline $deadline `
                -RootName ([string]$state.identity.name) `
                -ParentGuard $guard
            $capturedAfterTermination = $false
            foreach ($identity in @($afterTermination.identities)) {
                $added = Add-LegacyIdentityState `
                    -Identity $identity `
                    -States $states `
                    -StateByKey $stateByKey
                if ($null -ne $added) {
                    $capturedAfterTermination = $true
                    $madeProgress = $true
                }
            }
            if ($capturedAfterTermination) {
                Publish-LegacyRecoveryProgressAfterBound `
                    -States $states `
                    -Passthrough $passthrough `
                    -ProgressCallback $ProgressCallback `
                    -DeferredError ([ref]$deferredJournalError) `
                    -IncludeInProgress
            }
            if ($afterTermination.state -eq 'success') {
                $state.retain_marker = $false
            }
            else {
                $state.retain_marker = $true
                Publish-LegacyRecoveryProgressAfterBound `
                    -States $states `
                    -Passthrough $passthrough `
                    -ProgressCallback $ProgressCallback `
                    -DeferredError ([ref]$deferredJournalError) `
                    -IncludeInProgress
            }
            }
            finally {
                Close-LegacyExactProcessGuard -Guard $guard
            }
        }
        if ($deadlineExceeded) {
            break
        }
    }

    if ($deadlineExceeded -or [DateTimeOffset]::UtcNow -ge $deadline) {
        Write-Warning "Legacy descendant discovery exceeded its global deadline."
        foreach ($state in @($states)) {
            $state.retain_marker = $true
        }
        Publish-LegacyRecoveryProgress `
            -States $states `
            -Passthrough $passthrough `
            -ProgressCallback $ProgressCallback `
            -IncludeInProgress
    }

    $survivors = Get-LegacyRecoveryEntries -States $states -Passthrough $passthrough
    if ($null -ne $ProgressCallback) {
        & $ProgressCallback -RecoveryEntries @($survivors)
    }
    if ($null -ne $deferredJournalError) {
        Write-Warning (
            "Legacy process recovery completed after a transient journal error: " +
            $deferredJournalError.Exception.Message
        )
    }
    return [pscustomobject]@{ survivors = @($survivors) }
}

function Stop-LegacyRecordedProcessTree {
    param([object]$Entry)
    return Stop-LegacyRecordedProcessTrees -Entries @($Entry)
}

function Stop-ProcessEntries {
    param(
        [object[]]$Entries,
        [scriptblock]$LegacyProgressCallback = $null
    )
    $survivors = [System.Collections.Generic.List[object]]::new()
    $legacyEntries = [System.Collections.Generic.List[object]]::new()
    $managedEntries = [System.Collections.Generic.List[object]]::new()
    foreach ($entry in @($Entries)) {
        $hasSupervisor = $entry.PSObject.Properties.Name -contains 'supervisor_pid'
        if (-not $hasSupervisor) {
            $legacyEntries.Add($entry)
            continue
        }
        $managedEntries.Add($entry)
    }
    if ($legacyEntries.Count -gt 0) {
        $legacyResult = Stop-LegacyRecordedProcessTrees `
            -Entries @($legacyEntries) `
            -ProgressCallback $LegacyProgressCallback
        foreach ($survivor in @($legacyResult.survivors)) {
            $survivors.Add($survivor)
        }
    }
    $managedDeadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
    $managedDeadlineExceeded = $false
    foreach ($entry in @($managedEntries)) {
        $remainingMilliseconds = Get-LegacyRemainingTimeoutMilliseconds -Deadline $managedDeadline
        if ($remainingMilliseconds -le 0) {
            $managedDeadlineExceeded = $true
            $survivors.Add($entry)
            continue
        }

        $supervisorResult = Invoke-VerifiedTermination `
            -ProcessId ([int]$entry.supervisor_pid) `
            -StartTimeUtcTicks ([string]$entry.supervisor_start_time_utc_ticks) `
            -StartTimeUtc ($entry.supervisor_start_time_utc) `
            -Executable ([string]$entry.supervisor_executable) `
            -TimeoutMilliseconds $remainingMilliseconds
        if ($supervisorResult.state -in @('error', 'timeout')) {
            $remainingMilliseconds = Get-LegacyRemainingTimeoutMilliseconds -Deadline $managedDeadline
            if ($remainingMilliseconds -gt 0) {
                Start-Sleep -Milliseconds ([Math]::Min(200, $remainingMilliseconds))
            }
            $remainingMilliseconds = Get-LegacyRemainingTimeoutMilliseconds -Deadline $managedDeadline
            if ($remainingMilliseconds -gt 0) {
                $supervisorResult = Invoke-VerifiedTermination `
                    -ProcessId ([int]$entry.supervisor_pid) `
                    -StartTimeUtcTicks ([string]$entry.supervisor_start_time_utc_ticks) `
                    -StartTimeUtc ($entry.supervisor_start_time_utc) `
                    -Executable ([string]$entry.supervisor_executable) `
                    -TimeoutMilliseconds $remainingMilliseconds
            }
            else {
                $managedDeadlineExceeded = $true
            }
        }
        if ($supervisorResult.state -in @('missing', 'terminated')) {
            do {
                $childStatus = Get-RecordedProcessStatus `
                    -ProcessId ([int]$entry.pid) `
                    -StartTimeUtcTicks ([string]$entry.start_time_utc_ticks) `
                    -StartTimeUtc ($entry.start_time_utc) `
                    -Executable ([string]$entry.executable)
                if ($childStatus.state -ne 'match') {
                    break
                }
                $remainingMilliseconds = Get-LegacyRemainingTimeoutMilliseconds -Deadline $managedDeadline
                if ($remainingMilliseconds -le 0) {
                    $managedDeadlineExceeded = $true
                    break
                }
                Start-Sleep -Milliseconds ([Math]::Min(100, $remainingMilliseconds))
            } while ($true)
            if ($childStatus.state -eq 'match') {
                $remainingMilliseconds = Get-LegacyRemainingTimeoutMilliseconds -Deadline $managedDeadline
                if ($remainingMilliseconds -gt 0) {
                    $childResult = Invoke-VerifiedTermination `
                        -ProcessId ([int]$entry.pid) `
                        -StartTimeUtcTicks ([string]$entry.start_time_utc_ticks) `
                        -StartTimeUtc ($entry.start_time_utc) `
                        -Executable ([string]$entry.executable) `
                        -TimeoutMilliseconds $remainingMilliseconds
                }
                else {
                    $managedDeadlineExceeded = $true
                    $childResult = [pscustomobject]@{ state = 'timeout' }
                }
                if ($childResult.state -notin @('missing', 'terminated')) {
                    $survivors.Add($entry)
                }
            }
        }
        else {
            if ($supervisorResult.state -eq 'mismatch') {
                Write-Warning "Supervisor PID $($entry.supervisor_pid) no longer matches its recorded identity."
            }
            Write-Warning "Failed to stop $($entry.name) (state=$($supervisorResult.state)); retaining its recoverable identity in the manifest."
            $survivors.Add($entry)
        }
    }
    if ($managedDeadlineExceeded) {
        Write-Warning "Managed process cleanup exceeded its shared 15 second deadline; exact survivor identities were retained."
    }
    return [pscustomobject]@{ survivors = @($survivors) }
}

function Write-JsonAtomically {
    param([object]$Value)
    New-Item -ItemType Directory -Force -Path $RuntimeDirectory | Out-Null
    $temporary = Join-Path $RuntimeDirectory ".$([Guid]::NewGuid().ToString('N')).manifest.tmp"
    try {
        Write-Utf8NoBom -LiteralPath $temporary -Value ($Value | ConvertTo-Json -Depth 6)
        Move-Item -LiteralPath $temporary -Destination $PidFile -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($LiteralPath, $Value, $encoding)
}

function Stop-RecordedProcesses {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return
    }
    $manifest = Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json
    $recoverable = [System.Collections.Generic.List[object]]::new()
    foreach ($entry in @($manifest.processes)) {
        $recoverable.Add($entry)
    }
    $managedEntries = @(
        $recoverable |
            Where-Object { $_.PSObject.Properties.Name -contains 'supervisor_pid' }
    )
    $legacyProgressCallback = {
        param([object[]]$RecoveryEntries)
        $progressEntries = @($RecoveryEntries) + @($managedEntries)
        if ($progressEntries.Count -eq 0) {
            if (Test-Path -LiteralPath $PidFile) {
                Remove-Item -LiteralPath $PidFile -Force -ErrorAction Stop
            }
            return
        }
        $manifest.processes = @($progressEntries)
        Write-JsonAtomically -Value $manifest
    }.GetNewClosure()
    $stopResult = Stop-ProcessEntries `
        -Entries @($recoverable) `
        -LegacyProgressCallback $legacyProgressCallback
    $survivors = @($stopResult.survivors)
    if ($survivors.Count -eq 0) {
        if (Test-Path -LiteralPath $PidFile) {
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction Stop
        }
    }
    else {
        $manifest.processes = @($survivors)
        Write-JsonAtomically -Value $manifest
        throw "Failed to stop $($survivors.Count) local process group(s); survivor identities remain in $PidFile."
    }
}

if ($Stop) {
    Stop-RecordedProcesses
    return
}

if (Test-Path -LiteralPath $PidFile) {
    $recoveryManifest = Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json
    $requiresLegacyRecovery = $false
    foreach ($entry in @($recoveryManifest.processes)) {
        if ($entry.PSObject.Properties.Name -contains 'legacy_descendant_scan_incomplete') {
            $requiresLegacyRecovery = $true
            break
        }
        if ($entry.PSObject.Properties.Name -contains 'supervisor_pid') {
            continue
        }
        $legacyStatus = Get-RecordedProcessStatus `
            -ProcessId ([int]$entry.pid) `
            -StartTimeUtcTicks ([string]$entry.start_time_utc_ticks) `
            -StartTimeUtc ($entry.start_time_utc) `
            -Executable ([string]$entry.executable)
        if ($legacyStatus.state -ne 'match') {
            $requiresLegacyRecovery = $true
            break
        }
    }
    if ($requiresLegacyRecovery) {
        Stop-RecordedProcesses
    }
}

$EnvFile = $EnvironmentFile
if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Environment file does not exist: $EnvFile"
}
New-Item -ItemType Directory -Force -Path $RuntimeDirectory | Out-Null

if (Test-Path -LiteralPath $PidFile) {
    $existing = Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json
    $running = [System.Collections.Generic.List[object]]::new()
    foreach ($entry in @($existing.processes)) {
        if ($entry.PSObject.Properties.Name -contains 'supervisor_pid') {
            $supervisorStatus = Get-RecordedProcessStatus `
                -ProcessId ([int]$entry.supervisor_pid) `
                -StartTimeUtcTicks ([string]$entry.supervisor_start_time_utc_ticks) `
                -StartTimeUtc ($entry.supervisor_start_time_utc) `
                -Executable ([string]$entry.supervisor_executable)
            if ($supervisorStatus.state -eq 'match') {
                $running.Add($entry)
                continue
            }
        }
        $childStatus = Get-RecordedProcessStatus `
            -ProcessId ([int]$entry.pid) `
            -StartTimeUtcTicks ([string]$entry.start_time_utc_ticks) `
            -StartTimeUtc ($entry.start_time_utc) `
            -Executable ([string]$entry.executable)
        if ($childStatus.state -eq 'match') {
            $running.Add($entry)
        }
    }
    if ($running.Count -gt 0) {
        throw "Local server processes are already running. Stop them with server/scripts/run-local.ps1 -Stop."
    }
    Remove-Item -LiteralPath $PidFile -Force
}

$BaseEnvironment = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#') -or -not $trimmed.Contains('=')) {
        continue
    }
    $name, $value = $trimmed.Split('=', 2)
    $value = $value.Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    $BaseEnvironment[$name.Trim()] = $value
}

function Get-ConfiguredInteger {
    param([string]$Name, [int]$Requested, [int]$Fallback)
    if ($Requested -gt 0) {
        return $Requested
    }
    if ($BaseEnvironment.ContainsKey($Name)) {
        $parsed = 0
        if (-not [int]::TryParse($BaseEnvironment[$Name], [ref]$parsed) -or $parsed -lt 1 -or $parsed -gt 65535) {
            throw "$Name must be an integer from 1 through 65535."
        }
        return $parsed
    }
    return $Fallback
}

function Require-ConfiguredSecret {
    param([string]$Name)
    if (-not $BaseEnvironment.ContainsKey($Name)) {
        throw "$Name is required in the ignored .env file."
    }
    $value = [string]$BaseEnvironment[$Name]
    if ([string]::IsNullOrWhiteSpace($value) -or $value.StartsWith('replace-with-')) {
        throw "$Name still contains an unsafe placeholder."
    }
}

$DirectorPort = Get-ConfiguredInteger 'VOICE_DIRECTOR_BIND_PORT' $DirectorPort 8080
$WorkerBasePort = Get-ConfiguredInteger 'VOICE_WORKER_BIND_PORT' $WorkerBasePort 8081
$UdpBasePort = Get-ConfiguredInteger 'VOICE_UDP_BIND_PORT' $UdpBasePort 8092
$DirectorBindHost = if ($BaseEnvironment.ContainsKey('VOICE_DIRECTOR_BIND_HOST')) {
    $BaseEnvironment['VOICE_DIRECTOR_BIND_HOST']
} else { '127.0.0.1' }
$WorkerBindHost = if ($BaseEnvironment.ContainsKey('VOICE_WORKER_BIND_HOST')) {
    $BaseEnvironment['VOICE_WORKER_BIND_HOST']
} else { '127.0.0.1' }

foreach ($secretName in @(
    'VOICE_INTERNAL_TOKEN',
    'VOICE_GRANT_SIGNING_KEY',
    'VOICE_DEVICE_BOOTSTRAP_TOKEN',
    'VOICE_LAB_TOKEN'
)) {
    Require-ConfiguredSecret $secretName
}
if ($BaseEnvironment['VOICE_RUNNER'] -eq 'livekit') {
    Require-ConfiguredSecret 'VOICE_LLM_API_KEY'
}

$PublicRvaUri = $null
try { $PublicRvaUri = [Uri]$BaseEnvironment['VOICE_RVA_PUBLIC_WS_URL'] }
catch { throw 'VOICE_RVA_PUBLIC_WS_URL must be configured as an absolute ws:// or wss:// URL.' }
if ($PublicRvaUri.Scheme -notin @('ws', 'wss') -or -not $PublicRvaUri.IsAbsoluteUri -or
    [string]::IsNullOrWhiteSpace($PublicRvaUri.Host) -or -not [string]::IsNullOrEmpty($PublicRvaUri.UserInfo) -or
    $PublicRvaUri.AbsolutePath -ne '/v2/voice' -or -not [string]::IsNullOrEmpty($PublicRvaUri.Query) -or
    -not [string]::IsNullOrEmpty($PublicRvaUri.Fragment)) {
    throw 'VOICE_RVA_PUBLIC_WS_URL must use the canonical /v2/voice WebSocket URL.'
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Server virtual environment is missing. Run scripts/bootstrap.ps1 first.'
}
if (-not (Test-Path -LiteralPath $SupervisorScript)) {
    throw "Windows Job Object supervisor is missing: $SupervisorScript"
}
if ([string]::IsNullOrWhiteSpace($SupervisorPython)) {
    throw 'Could not resolve the base Python executable for the local process supervisor.'
}
if (-not (Test-Path -LiteralPath $SupervisorPython)) {
    throw "Base Python executable does not exist: $SupervisorPython"
}
$Started = [System.Collections.Generic.List[object]]::new()
$LaunchStartedAt = [DateTimeOffset]::UtcNow.ToString('O')

function Write-StartedManifest {
    param(
        [bool]$StartupInProgress = $false,
        [bool]$StartupFailed = $false,
        [object[]]$Processes = @($Started)
    )
    Write-JsonAtomically -Value ([pscustomobject]@{
        started_at = $LaunchStartedAt
        director_port = $DirectorPort
        worker_count = $WorkerCount
        startup_in_progress = $StartupInProgress
        startup_failed = $StartupFailed
        processes = @($Processes)
    })
}

function ConvertTo-NativeQuotedArgument {
    param([string]$Value)
    if ($Value.Contains('"')) {
        throw 'Local supervisor paths must not contain a double quote.'
    }
    return '"' + $Value + '"'
}

function Start-LocalProcess {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [hashtable]$Overrides
    )
    $environment = @{}
    foreach ($item in $BaseEnvironment.GetEnumerator()) {
        $environment[$item.Key] = $item.Value
    }
    foreach ($item in $Overrides.GetEnumerator()) {
        $environment[$item.Key] = [string]$item.Value
    }
    $stdout = Join-Path $RuntimeDirectory "$Name.stdout.log"
    $stderr = Join-Path $RuntimeDirectory "$Name.stderr.log"
    $supervisorStdout = Join-Path $RuntimeDirectory "$Name.supervisor.stdout.log"
    $supervisorStderr = Join-Path $RuntimeDirectory "$Name.supervisor.stderr.log"
    $specFile = Join-Path $RuntimeDirectory "$Name.launch.json"
    $handshakeFile = Join-Path $RuntimeDirectory "$Name.handshake.json"
    Remove-Item -LiteralPath $handshakeFile -Force -ErrorAction SilentlyContinue
    [pscustomobject]@{
        executable = [System.IO.Path]::GetFullPath($Python)
        arguments = $Arguments
        working_directory = $ServerRoot
        stdout_file = $stdout
        stderr_file = $stderr
        handshake_file = $handshakeFile
    } | ConvertTo-Json -Depth 4 | ForEach-Object {
        Write-Utf8NoBom -LiteralPath $specFile -Value $_
    }

    $process = Start-Process `
        -FilePath $SupervisorPython `
        -ArgumentList @(
            (ConvertTo-NativeQuotedArgument $SupervisorScript),
            '--spec',
            (ConvertTo-NativeQuotedArgument $specFile)
        ) `
        -WorkingDirectory $ServerRoot `
        -WindowStyle Hidden `
        -Environment $environment `
        -RedirectStandardOutput $supervisorStdout `
        -RedirectStandardError $supervisorStderr `
        -PassThru
    $entry = [pscustomobject]@{
        name = $Name
        pid = 0
        start_time_utc = ''
        start_time_utc_ticks = ''
        executable = [System.IO.Path]::GetFullPath($Python)
        supervisor_pid = $process.Id
        supervisor_start_time_utc = $process.StartTime.ToUniversalTime().ToString('O')
        supervisor_start_time_utc_ticks = [string]$process.StartTime.ToUniversalTime().Ticks
        supervisor_executable = $SupervisorPython
        job_managed = $true
        stdout = $stdout
        stderr = $stderr
        supervisor_stdout = $supervisorStdout
        supervisor_stderr = $supervisorStderr
        launch_spec = $specFile
        handshake = $handshakeFile
        environment = $Overrides
    }
    $Started.Add($entry)
    Write-StartedManifest -StartupInProgress $true

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $handshakeFile)) {
        if ($process.HasExited) {
            throw "$Name Job supervisor exited before launch. Inspect $supervisorStderr."
        }
        if ([DateTimeOffset]::UtcNow -ge $deadline) {
            throw "$Name Job supervisor did not publish its handshake within 10 seconds."
        }
        Start-Sleep -Milliseconds 50
    }
    $handshake = Get-Content -Raw -LiteralPath $handshakeFile | ConvertFrom-Json
    if ($handshake.job_managed -ne $true) {
        throw "$Name Job supervisor did not confirm Job Object ownership."
    }
    $child = Get-Process -Id ([int]$handshake.process_id) -ErrorAction SilentlyContinue
    if ($null -eq $child) {
        throw "$Name child exited before its identity could be recorded."
    }
    $entry.pid = $child.Id
    $entry.start_time_utc = $child.StartTime.ToUniversalTime().ToString('O')
    $entry.start_time_utc_ticks = [string]$child.StartTime.ToUniversalTime().Ticks
    Write-StartedManifest -StartupInProgress $true
    return $process
}

function Wait-LocalHealth {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Name,
        [string]$Url,
        [int]$TimeoutSeconds = 60
    )
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ($Process.HasExited) {
            throw "$Name exited before becoming ready. Inspect $RuntimeDirectory/$Name.stderr.log."
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 200
        }
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "$Name did not become ready at $Url within $TimeoutSeconds seconds."
}

try {
    $directorProcess = Start-LocalProcess -Name 'session-director' -Arguments @('-m', 'session_director') -Overrides @{
        VOICE_DIRECTOR_BIND_HOST = $DirectorBindHost
        VOICE_DIRECTOR_BIND_PORT = $DirectorPort
    }
    Wait-LocalHealth -Process $directorProcess -Name 'session-director' -Url "http://127.0.0.1:$DirectorPort/health/ready"

    for ($index = 0; $index -lt $WorkerCount; $index++) {
        $workerNumber = $index + 1
        $workerPort = $WorkerBasePort + $index
        $udpPort = $UdpBasePort + $index
        if ($workerPort -gt 65535 -or $udpPort -gt 65535) {
            throw 'WorkerCount causes a local port to exceed 65535.'
        }
        $workerOverrides = @{
            VOICE_DIRECTOR_URL = "http://127.0.0.1:$DirectorPort"
            VOICE_HEARTBEAT_ENABLED = 'true'
            VOICE_WORKER_ID = "worker-local-$workerNumber"
            VOICE_WORKER_BIND_HOST = $WorkerBindHost
            VOICE_WORKER_BIND_PORT = $workerPort
            VOICE_UDP_BIND_PORT = $udpPort
            VOICE_UDP_ADVERTISE_PORT = $udpPort
        }
        $publicRvaUriBuilder = [UriBuilder]$PublicRvaUri
        $publicRvaUriBuilder.Port = $workerPort
        $workerOverrides.VOICE_RVA_PUBLIC_WS_URL = $publicRvaUriBuilder.Uri.AbsoluteUri
        $workerProcess = Start-LocalProcess -Name "realtime-worker-$workerNumber" -Arguments @('-m', 'realtime_worker') -Overrides $workerOverrides
        Wait-LocalHealth `
            -Process $workerProcess `
            -Name "realtime-worker-$workerNumber" `
            -Url "http://127.0.0.1:$workerPort/health/ready"
    }

    Write-StartedManifest
    Write-Host "Started Session Director and $WorkerCount Realtime Worker process(es)."
    Write-Host "Process manifest: $PidFile"
}
catch {
    $launchError = $_
    $cleanupError = $null
    $manifestError = $null
    $survivors = @()
    if ($Started.Count -gt 0) {
        try {
            $cleanupResult = Stop-ProcessEntries -Entries @($Started)
            $survivors = @($cleanupResult.survivors)
        }
        catch {
            $cleanupError = $_
            $survivors = @($Started)
        }
        try {
            if ($survivors.Count -eq 0) {
                Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
            }
            else {
                Write-StartedManifest -StartupFailed $true -Processes $survivors
            }
        }
        catch {
            $manifestError = $_
        }
    }
    if ($null -ne $cleanupError -or $survivors.Count -gt 0 -or $null -ne $manifestError) {
        $details = [System.Collections.Generic.List[string]]::new()
        if ($null -ne $cleanupError) {
            $details.Add("cleanup error: $($cleanupError.Exception.Message)")
        }
        if ($survivors.Count -gt 0) {
            $details.Add("$($survivors.Count) survivor identity record(s) remain")
        }
        if ($null -ne $manifestError) {
            $details.Add("manifest recovery error: $($manifestError.Exception.Message)")
        }
        throw "Local server startup failed: $($launchError.Exception.Message) Recovery failed: $($details -join '; ')"
    }
    throw $launchError
}

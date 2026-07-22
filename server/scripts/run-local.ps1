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
        [string]$StartTimeUtc
    )
    $ticks = 0L
    if (-not [string]::IsNullOrWhiteSpace($StartTimeUtcTicks) -and
        [long]::TryParse($StartTimeUtcTicks, [ref]$ticks) -and $ticks -gt 0) {
        return $ticks
    }
    $timestamp = [DateTimeOffset]::MinValue
    if (-not [string]::IsNullOrWhiteSpace($StartTimeUtc) -and
        [DateTimeOffset]::TryParse(
            $StartTimeUtc,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind,
            [ref]$timestamp
        )) {
        return $timestamp.UtcDateTime.Ticks
    }
    return $null
}

function Get-RecordedProcessStatus {
    param(
        [int]$ProcessId,
        [string]$StartTimeUtcTicks,
        [string]$StartTimeUtc,
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
        [string]$StartTimeUtc,
        [int]$StartTimeToleranceTicks = 0,
        [string]$Executable
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
                --timeout-ms 5000 2>&1
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
        -StartTimeUtc ([string]$Entry.start_time_utc)
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

function Get-LegacyProcessTreeSnapshot {
    param([object]$Entry)
    $rootStatus = Get-RecordedProcessStatus `
        -ProcessId ([int]$Entry.pid) `
        -StartTimeUtcTicks ([string]$Entry.start_time_utc_ticks) `
        -StartTimeUtc ([string]$Entry.start_time_utc) `
        -StartTimeToleranceTicks ([int]$Entry.start_time_tolerance_ticks) `
        -Executable ([string]$Entry.executable)
    if ($rootStatus.state -eq 'missing') {
        return [pscustomobject]@{ state = 'missing'; identities = @() }
    }
    if ($rootStatus.state -ne 'match') {
        return [pscustomobject]@{ state = 'mismatch'; identities = @($Entry) }
    }

    $identities = [System.Collections.Generic.List[object]]::new()
    $identities.Add((ConvertTo-RecoverableLegacyEntry -Entry $Entry))
    $pending = [System.Collections.Generic.Queue[int]]::new()
    $pending.Enqueue([int]$Entry.pid)
    while ($pending.Count -gt 0) {
        $parentId = $pending.Dequeue()
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $parentId" -ErrorAction SilentlyContinue
        foreach ($child in @($children)) {
            try {
                $created = ([DateTime]$child.CreationDate).ToUniversalTime()
                $executable = [string]$child.ExecutablePath
                if ([string]::IsNullOrWhiteSpace($executable)) {
                    throw "CIM did not expose ExecutablePath"
                }
                $identities.Add([pscustomobject]@{
                    name = "$($Entry.name)-descendant-$($child.ProcessId)"
                    pid = [int]$child.ProcessId
                    start_time_utc = $created.ToString('O')
                    start_time_utc_ticks = [string]$created.Ticks
                    start_time_tolerance_ticks = 10
                    executable = [System.IO.Path]::GetFullPath($executable)
                    job_managed = $false
                })
                $pending.Enqueue([int]$child.ProcessId)
            }
            catch {
                Write-Warning "Cannot capture the identity of legacy descendant PID $($child.ProcessId)."
            }
        }
    }
    return [pscustomobject]@{ state = 'match'; identities = @($identities) }
}

function Stop-LegacyRecordedProcessTree {
    param([object]$Entry)
    $snapshot = Get-LegacyProcessTreeSnapshot -Entry $Entry
    if ($snapshot.state -eq 'missing') {
        return [pscustomobject]@{ survivors = @() }
    }
    if ($snapshot.state -ne 'match') {
        Write-Warning "Legacy PID $($Entry.pid) no longer matches its recorded identity."
        return [pscustomobject]@{ survivors = @($Entry) }
    }

    Write-Warning "Stopping legacy manifest entry $($Entry.name); restart it to obtain Job Object lifecycle guarantees."
    $identities = @($snapshot.identities)

    $stopOrder = [System.Collections.Generic.List[object]]::new()
    $stopOrder.Add($identities[0])
    for ($index = $identities.Count - 1; $index -ge 1; $index--) {
        $stopOrder.Add($identities[$index])
    }
    $survivors = [System.Collections.Generic.List[object]]::new()
    foreach ($identity in $stopOrder) {
        $result = Invoke-VerifiedTermination `
            -ProcessId ([int]$identity.pid) `
            -StartTimeUtcTicks ([string]$identity.start_time_utc_ticks) `
            -StartTimeUtc ([string]$identity.start_time_utc) `
            -StartTimeToleranceTicks ([int]$identity.start_time_tolerance_ticks) `
            -Executable ([string]$identity.executable)
        if ($result.state -notin @('missing', 'terminated')) {
            $survivors.Add($identity)
        }
    }
    return [pscustomobject]@{ survivors = @($survivors) }
}

function Stop-ProcessEntries {
    param([object[]]$Entries)
    $survivors = [System.Collections.Generic.List[object]]::new()
    foreach ($entry in @($Entries)) {
        $hasSupervisor = $entry.PSObject.Properties.Name -contains 'supervisor_pid'
        if (-not $hasSupervisor) {
            $legacyResult = Stop-LegacyRecordedProcessTree -Entry $entry
            foreach ($survivor in @($legacyResult.survivors)) {
                $survivors.Add($survivor)
            }
            continue
        }
        $supervisorResult = Invoke-VerifiedTermination `
            -ProcessId ([int]$entry.supervisor_pid) `
            -StartTimeUtcTicks ([string]$entry.supervisor_start_time_utc_ticks) `
            -StartTimeUtc ([string]$entry.supervisor_start_time_utc) `
            -Executable ([string]$entry.supervisor_executable)
        if ($supervisorResult.state -in @('error', 'timeout')) {
            Start-Sleep -Milliseconds 200
            $supervisorResult = Invoke-VerifiedTermination `
                -ProcessId ([int]$entry.supervisor_pid) `
                -StartTimeUtcTicks ([string]$entry.supervisor_start_time_utc_ticks) `
                -StartTimeUtc ([string]$entry.supervisor_start_time_utc) `
                -Executable ([string]$entry.supervisor_executable)
        }
        if ($supervisorResult.state -in @('missing', 'terminated')) {
            $deadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
            do {
                $childStatus = Get-RecordedProcessStatus `
                    -ProcessId ([int]$entry.pid) `
                    -StartTimeUtcTicks ([string]$entry.start_time_utc_ticks) `
                    -StartTimeUtc ([string]$entry.start_time_utc) `
                    -Executable ([string]$entry.executable)
                if ($childStatus.state -ne 'match') {
                    break
                }
                Start-Sleep -Milliseconds 100
            } while ([DateTimeOffset]::UtcNow -lt $deadline)
            if ($childStatus.state -eq 'match') {
                $childResult = Invoke-VerifiedTermination `
                    -ProcessId ([int]$entry.pid) `
                    -StartTimeUtcTicks ([string]$entry.start_time_utc_ticks) `
                    -StartTimeUtc ([string]$entry.start_time_utc) `
                    -Executable ([string]$entry.executable)
                if ($childResult.state -notin @('missing', 'terminated')) {
                    $survivors.Add((ConvertTo-RecoverableLegacyEntry -Entry ([pscustomobject]@{
                        name = "$($entry.name)-detached-child"
                        pid = $entry.pid
                        start_time_utc = $entry.start_time_utc
                        start_time_utc_ticks = $entry.start_time_utc_ticks
                        executable = $entry.executable
                    })))
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
    return [pscustomobject]@{ survivors = @($survivors) }
}

function Write-JsonAtomically {
    param([object]$Value)
    New-Item -ItemType Directory -Force -Path $RuntimeDirectory | Out-Null
    $temporary = Join-Path $RuntimeDirectory ".$([Guid]::NewGuid().ToString('N')).manifest.tmp"
    try {
        $Value | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporary -Encoding utf8NoBOM
        Move-Item -LiteralPath $temporary -Destination $PidFile -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Stop-RecordedProcesses {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return
    }
    $manifest = Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json -DateKind String
    $recoverable = [System.Collections.Generic.List[object]]::new()
    $recordedIdentities = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $hasLegacyEntry = $false
    foreach ($entry in @($manifest.processes)) {
        if ($entry.PSObject.Properties.Name -contains 'supervisor_pid') {
            $recoverable.Add($entry)
            continue
        }
        $hasLegacyEntry = $true
        $snapshot = Get-LegacyProcessTreeSnapshot -Entry $entry
        foreach ($identity in @($snapshot.identities)) {
            $ticks = Resolve-RecordedStartTimeUtcTicks `
                -StartTimeUtcTicks ([string]$identity.start_time_utc_ticks) `
                -StartTimeUtc ([string]$identity.start_time_utc)
            $key = "$([int]$identity.pid):${ticks}:$([string]$identity.executable)"
            if ($recordedIdentities.Add($key)) {
                $recoverable.Add($identity)
            }
        }
    }
    if ($hasLegacyEntry) {
        $manifest.processes = @($recoverable)
        Write-JsonAtomically -Value $manifest
    }
    $stopResult = Stop-ProcessEntries -Entries @($recoverable)
    $survivors = @($stopResult.survivors)
    if ($survivors.Count -eq 0) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
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
                -StartTimeUtc ([string]$entry.supervisor_start_time_utc) `
                -Executable ([string]$entry.supervisor_executable)
            if ($supervisorStatus.state -eq 'match') {
                $running.Add($entry)
                continue
            }
        }
        $childStatus = Get-RecordedProcessStatus `
            -ProcessId ([int]$entry.pid) `
            -StartTimeUtcTicks ([string]$entry.start_time_utc_ticks) `
            -StartTimeUtc ([string]$entry.start_time_utc) `
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

$LegacyXiaozhiEnabled = $BaseEnvironment.ContainsKey('VOICE_LEGACY_XIAOZHI_ENABLED') -and
    $BaseEnvironment['VOICE_LEGACY_XIAOZHI_ENABLED'].Equals('true', [StringComparison]::OrdinalIgnoreCase)
$RvaEnabled = -not $BaseEnvironment.ContainsKey('VOICE_RVA_ENABLED') -or
    $BaseEnvironment['VOICE_RVA_ENABLED'].Equals('true', [StringComparison]::OrdinalIgnoreCase)
$PublicWorkerUri = $null
$PublicRvaUri = $null
if ($LegacyXiaozhiEnabled) {
    try { $PublicWorkerUri = [Uri]$BaseEnvironment['VOICE_WORKER_PUBLIC_WS_URL'] }
    catch { throw 'VOICE_WORKER_PUBLIC_WS_URL must be configured as an absolute ws:// or wss:// URL.' }
    if ($PublicWorkerUri.Scheme -notin @('ws', 'wss') -or -not $PublicWorkerUri.IsAbsoluteUri) {
        throw 'VOICE_WORKER_PUBLIC_WS_URL must be configured as an absolute ws:// or wss:// URL.'
    }
}
if ($RvaEnabled) {
    try { $PublicRvaUri = [Uri]$BaseEnvironment['VOICE_RVA_PUBLIC_WS_URL'] }
    catch { throw 'VOICE_RVA_PUBLIC_WS_URL must be configured as an absolute ws:// or wss:// URL.' }
    if ($PublicRvaUri.Scheme -notin @('ws', 'wss') -or -not $PublicRvaUri.IsAbsoluteUri) {
        throw 'VOICE_RVA_PUBLIC_WS_URL must be configured as an absolute ws:// or wss:// URL.'
    }
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
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $specFile -Encoding utf8NoBOM

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
        [int]$TimeoutSeconds = 15
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
        if ($LegacyXiaozhiEnabled) {
            $publicUriBuilder = [UriBuilder]$PublicWorkerUri
            $publicUriBuilder.Port = $workerPort
            $workerOverrides.VOICE_WORKER_PUBLIC_WS_URL = $publicUriBuilder.Uri.AbsoluteUri
        }
        if ($RvaEnabled) {
            $publicRvaUriBuilder = [UriBuilder]$PublicRvaUri
            $publicRvaUriBuilder.Port = $workerPort
            $workerOverrides.VOICE_RVA_PUBLIC_WS_URL = $publicRvaUriBuilder.Uri.AbsoluteUri
        }
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

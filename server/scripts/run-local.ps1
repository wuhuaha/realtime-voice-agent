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
if ([string]::IsNullOrWhiteSpace($EnvironmentFile)) {
    $EnvironmentFile = Join-Path $RepoRoot '.env'
}
$EnvironmentFile = [System.IO.Path]::GetFullPath($EnvironmentFile)

function Get-DescendantProcessIds {
    param([int]$ParentId)
    $descendants = [System.Collections.Generic.List[int]]::new()
    $pending = [System.Collections.Generic.Queue[int]]::new()
    $pending.Enqueue($ParentId)
    while ($pending.Count -gt 0) {
        $currentParent = $pending.Dequeue()
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $currentParent" -ErrorAction SilentlyContinue
        foreach ($child in @($children)) {
            $childId = [int]$child.ProcessId
            $descendants.Add($childId)
            $pending.Enqueue($childId)
        }
    }
    return @($descendants)
}

function Stop-RecordedProcesses {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return
    }
    $manifest = Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json -DateKind String
    $unmatched = [System.Collections.Generic.List[object]]::new()
    foreach ($entry in @($manifest.processes)) {
        $process = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            continue
        }
        try {
            $executable = $process.Path
        }
        catch {
            Write-Warning "Cannot verify process identity for PID $($entry.pid); leaving it running."
            $unmatched.Add($entry)
            continue
        }
        try {
            if ($null -ne $entry.start_time_utc_ticks) {
                $startMatches = $process.StartTime.ToUniversalTime().Ticks -eq [long]$entry.start_time_utc_ticks
            }
            else {
                $recordedStart = [DateTimeOffset]::Parse(
                    [string]$entry.start_time_utc,
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::AssumeUniversal
                ).UtcDateTime
                $startMatches = $process.StartTime.ToUniversalTime() -eq $recordedStart
            }
            $executableMatches = [string]::Equals(
                [System.IO.Path]::GetFullPath($executable),
                [System.IO.Path]::GetFullPath([string]$entry.executable),
                [StringComparison]::OrdinalIgnoreCase
            )
        }
        catch {
            $startMatches = $false
            $executableMatches = $false
        }
        if (-not $startMatches -or -not $executableMatches) {
            Write-Warning "PID $($entry.pid) no longer matches the recorded process; leaving it running."
            $unmatched.Add($entry)
            continue
        }
        $descendantIds = @(Get-DescendantProcessIds -ParentId $process.Id)
        $ownedProcessIds = @($descendantIds) + @($process.Id)
        try {
            for ($index = $descendantIds.Count - 1; $index -ge 0; $index--) {
                Stop-Process -Id $descendantIds[$index] -Force -ErrorAction SilentlyContinue
            }
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $ownedProcessIds -Timeout 5 -ErrorAction SilentlyContinue
            if (@(Get-Process -Id $ownedProcessIds -ErrorAction SilentlyContinue).Count -gt 0) {
                throw "process tree did not exit"
            }
        }
        catch {
            Write-Warning "Failed to stop recorded PID $($entry.pid); retaining it in the manifest."
            $unmatched.Add($entry)
        }
    }
    if ($unmatched.Count -eq 0) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
    else {
        $manifest.processes = @($unmatched)
        $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $PidFile -Encoding utf8NoBOM
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
    $running = @($existing.processes | Where-Object { $null -ne (Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue) })
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
$UdpBasePort = Get-ConfiguredInteger 'VOICE_XIAOZHI_UDP_BIND_PORT' $UdpBasePort 8092
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

if (-not $BaseEnvironment.ContainsKey('VOICE_WORKER_PUBLIC_WS_URL')) {
    throw 'VOICE_WORKER_PUBLIC_WS_URL is required so devices receive an explicit reachable endpoint.'
}
try {
    $PublicWorkerUri = [Uri]$BaseEnvironment['VOICE_WORKER_PUBLIC_WS_URL']
}
catch {
    throw 'VOICE_WORKER_PUBLIC_WS_URL must be an absolute ws:// or wss:// URL.'
}
if ($PublicWorkerUri.Scheme -notin @('ws', 'wss') -or -not $PublicWorkerUri.IsAbsoluteUri) {
    throw 'VOICE_WORKER_PUBLIC_WS_URL must be an absolute ws:// or wss:// URL.'
}

$Python = Join-Path $ServerRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Server virtual environment is missing. Run scripts/bootstrap.ps1 first.'
}
$Started = [System.Collections.Generic.List[object]]::new()

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
    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList $Arguments `
        -WorkingDirectory $ServerRoot `
        -WindowStyle Hidden `
        -Environment $environment `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    $Started.Add([pscustomobject]@{
        name = $Name
        pid = $process.Id
        start_time_utc = $process.StartTime.ToUniversalTime().ToString('O')
        start_time_utc_ticks = [string]$process.StartTime.ToUniversalTime().Ticks
        executable = [System.IO.Path]::GetFullPath($Python)
        stdout = $stdout
        stderr = $stderr
        environment = $Overrides
    })
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
        $publicUriBuilder = [UriBuilder]$PublicWorkerUri
        $publicUriBuilder.Port = $workerPort
        $workerProcess = Start-LocalProcess -Name "realtime-worker-$workerNumber" -Arguments @('-m', 'realtime_worker') -Overrides @{
            VOICE_DIRECTOR_URL = "http://127.0.0.1:$DirectorPort"
            VOICE_HEARTBEAT_ENABLED = 'true'
            VOICE_WORKER_ID = "worker-local-$workerNumber"
            VOICE_WORKER_BIND_HOST = $WorkerBindHost
            VOICE_WORKER_BIND_PORT = $workerPort
            VOICE_WORKER_PUBLIC_WS_URL = $publicUriBuilder.Uri.AbsoluteUri
            VOICE_XIAOZHI_UDP_BIND_PORT = $udpPort
            VOICE_XIAOZHI_UDP_ADVERTISE_PORT = $udpPort
        }
        Wait-LocalHealth `
            -Process $workerProcess `
            -Name "realtime-worker-$workerNumber" `
            -Url "http://127.0.0.1:$workerPort/health/ready"
    }

    [pscustomobject]@{
        started_at = [DateTimeOffset]::UtcNow.ToString('O')
        director_port = $DirectorPort
        worker_count = $WorkerCount
        processes = @($Started)
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $PidFile -Encoding utf8NoBOM
    Write-Host "Started Session Director and $WorkerCount Realtime Worker process(es)."
    Write-Host "Process manifest: $PidFile"
}
catch {
    foreach ($entry in $Started) {
        $startedProcess = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
        if ($null -eq $startedProcess) {
            continue
        }
        $descendantIds = @(Get-DescendantProcessIds -ParentId $startedProcess.Id)
        for ($index = $descendantIds.Count - 1; $index -ge 0; $index--) {
            Stop-Process -Id $descendantIds[$index] -Force -ErrorAction SilentlyContinue
        }
        Stop-Process -Id $startedProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    throw
}

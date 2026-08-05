[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BuildDir,
    [Parameter(Mandatory = $true)]
    [string]$Output
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-ExistingDirectory([string]$Path, [string]$Description) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "$Description not found: $resolved"
    }
    return $resolved
}

function Require-File([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description not found: $Path"
    }
    return [IO.Path]::GetFullPath($Path)
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-Utf8Lf([string]$Path, [string]$Content) {
    $normalized = ($Content -replace "`r`n", "`n").TrimEnd("`r", "`n") + "`n"
    [IO.File]::WriteAllText($Path, $normalized, [Text.UTF8Encoding]::new($false))
}

function Get-RepositoryState([string]$Root) {
    $revision = (& git -C $Root rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $revision -notmatch '^[0-9a-fA-F]{40}$') {
        throw 'Unable to resolve Product source revision.'
    }
    $trackedStatus = @(& git -C $Root status --porcelain=v1 --untracked-files=no)
    if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect Product tracked tree state.' }
    $worktreeStatus = @(& git -C $Root status --porcelain=v1 --untracked-files=normal)
    if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect Product worktree state.' }
    return [ordered]@{
        source_revision = $revision.ToLowerInvariant()
        tracked_tree_clean = ($trackedStatus.Count -eq 0)
        worktree_clean = ($worktreeStatus.Count -eq 0)
    }
}

function Assert-PublicSdkconfig([string]$Path) {
    $lines = [IO.File]::ReadAllLines((Require-File $Path 'Generated sdkconfig'))
    if ($lines -notcontains 'CONFIG_IDF_TARGET="esp32s3"') {
        throw 'Generated sdkconfig does not target esp32s3.'
    }
    $publicOnlyKeys = @(
        'CONFIG_RVA_DIRECTOR_BOOTSTRAP_URL',
        'CONFIG_RVA_DEVICE_BOOTSTRAP_TOKEN',
        'CONFIG_RVA_WIFI_PRIMARY_SSID',
        'CONFIG_RVA_WIFI_PRIMARY_PASSWORD',
        'CONFIG_RVA_WIFI_FALLBACK_SSID',
        'CONFIG_RVA_WIFI_FALLBACK_PASSWORD'
    )
    foreach ($key in $publicOnlyKeys) {
        $matches = @($lines | Where-Object { $_ -match "^$([regex]::Escape($key))=" })
        if ($matches.Count -ne 1) { throw "Generated sdkconfig must assign $key exactly once." }
        if ($matches[0] -ne "$key=`"`"") { throw "Public release rejects non-empty $key." }
    }
}

function Resolve-BuildArtifact([string]$BuildRoot, [string]$RelativePath) {
    if ([IO.Path]::IsPathRooted($RelativePath)) {
        throw "Flash artifact path must be relative: $RelativePath"
    }
    $normalized = $RelativePath.Replace('/', [IO.Path]::DirectorySeparatorChar)
    $resolved = [IO.Path]::GetFullPath((Join-Path $BuildRoot $normalized))
    $rootPrefix = $BuildRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Flash artifact escapes build directory: $RelativePath"
    }
    return Require-File $resolved 'Flash artifact'
}

function Assert-PartitionTable([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    $expected = @(
        [ordered]@{ label = 'nvs'; type = 1; subtype = 2; offset = 0x9000; size = 0x6000 },
        [ordered]@{ label = 'phy_init'; type = 1; subtype = 1; offset = 0xf000; size = 0x1000 },
        [ordered]@{ label = 'factory'; type = 0; subtype = 0; offset = 0x10000; size = 0x400000 },
        [ordered]@{ label = 'model'; type = 1; subtype = 0x82; offset = 0x410000; size = 0x80000 },
        [ordered]@{ label = 'font_assets'; type = 1; subtype = 0x40; offset = 0x800000; size = 0x800000 }
    )
    if ($bytes.Length -lt ($expected.Count * 32)) { throw 'partition-table.bin is truncated.' }
    for ($entryIndex = 0; $entryIndex -lt $expected.Count; $entryIndex++) {
        $position = $entryIndex * 32
        $entry = $expected[$entryIndex]
        if ($bytes[$position] -ne 0xaa -or $bytes[$position + 1] -ne 0x50) {
            throw "partition-table.bin entry $entryIndex has invalid magic."
        }
        $label = [Text.Encoding]::ASCII.GetString($bytes, $position + 12, 16).TrimEnd([char]0)
        if ($bytes[$position + 2] -ne $entry.type -or
            $bytes[$position + 3] -ne $entry.subtype -or
            [BitConverter]::ToUInt32($bytes, $position + 4) -ne $entry.offset -or
            [BitConverter]::ToUInt32($bytes, $position + 8) -ne $entry.size -or
            $label -ne $entry.label) {
            throw "partition-table.bin entry $entryIndex does not match tracked partitions.csv."
        }
    }
}

function Assert-FontImage([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    $headerSize = 64
    if ($bytes.Length -lt $headerSize) { throw 'font_assets.bin is shorter than the RVA font header.' }
    if ([Text.Encoding]::ASCII.GetString($bytes, 0, 8) -ne "RVAFNT1`0") {
        throw 'font_assets.bin has an invalid magic.'
    }
    $version = [BitConverter]::ToUInt16($bytes, 8)
    $declaredHeaderSize = [BitConverter]::ToUInt16($bytes, 10)
    $payloadSize = [BitConverter]::ToUInt32($bytes, 12)
    if ($version -ne 1 -or $declaredHeaderSize -ne $headerSize) {
        throw 'font_assets.bin has an unsupported format version.'
    }
    if ($payloadSize -ne ($bytes.Length - $headerSize)) {
        throw 'font_assets.bin payload length does not match its header.'
    }
    $payload = [byte[]]::new($payloadSize)
    [Array]::Copy($bytes, $headerSize, $payload, 0, $payloadSize)
    $actualDigest = [Security.Cryptography.SHA256]::HashData($payload)
    for ($index = 0; $index -lt 32; $index++) {
        if ($bytes[16 + $index] -ne $actualDigest[$index]) {
            throw 'font_assets.bin payload digest does not match its header.'
        }
    }
    $sourceId = [Text.Encoding]::ASCII.GetString($bytes, 48, 16).TrimEnd([char]0)
    if ([string]::IsNullOrWhiteSpace($sourceId)) { throw 'font_assets.bin source identity is empty.' }
    return $sourceId
}

function New-DeterministicZip([string]$Stage, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression
    $stream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try {
        $archive = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Create, $false)
        try {
            $timestamp = [DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
            Get-ChildItem -LiteralPath $Stage -File | Sort-Object Name | ForEach-Object {
                $entry = $archive.CreateEntry($_.Name, [IO.Compression.CompressionLevel]::NoCompression)
                $entry.LastWriteTime = $timestamp
                $input = [IO.File]::OpenRead($_.FullName)
                $entryOutput = $entry.Open()
                try { $input.CopyTo($entryOutput) } finally { $entryOutput.Dispose(); $input.Dispose() }
            }
        } finally { $archive.Dispose() }
    } finally { $stream.Dispose() }
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$buildRoot = Resolve-ExistingDirectory $BuildDir 'Firmware build directory'
$outputPath = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $outputPath) { throw "Output already exists: $outputPath" }
$outputParent = Split-Path $outputPath -Parent
if (-not (Test-Path -LiteralPath $outputParent -PathType Container)) {
    New-Item -ItemType Directory -Path $outputParent -Force | Out-Null
}

$repositoryState = Get-RepositoryState $repoRoot
if (-not $repositoryState.tracked_tree_clean -or -not $repositoryState.worktree_clean) {
    throw 'Public release packaging requires a clean Product worktree.'
}
$provenancePath = Require-File (Join-Path $buildRoot 'build-provenance.json') 'Build provenance'
$provenance = Get-Content -LiteralPath $provenancePath -Raw | ConvertFrom-Json
if ($provenance.schema_version -ne 1 -or -not $provenance.release_eligible) {
    throw 'Build provenance is not release eligible.'
}
if ($provenance.font_asset_source.kind -notin @('explicit_pinned_package', 'pinned_registry_download') -or
    $provenance.font_asset_source.package_sha256 -ne '255868d6e225d08038f38add8f7f2bf2e3567ef7a3b0edcd9703d2101f56e7d5') {
    throw 'Build provenance does not identify the pinned font package.'
}
if ($provenance.source_revision -ne $repositoryState.source_revision -or
    $provenance.build_start.source_revision -ne $repositoryState.source_revision -or
    $provenance.build_end.source_revision -ne $repositoryState.source_revision) {
    throw 'Build provenance source revision does not match current HEAD.'
}
foreach ($state in @($provenance.build_start, $provenance.build_end)) {
    if (-not $state.tracked_tree_clean -or -not $state.worktree_clean) {
        throw 'Build provenance records a dirty Product worktree.'
    }
}

$sdkconfigPath = Require-File (Join-Path $buildRoot 'sdkconfig') 'Generated sdkconfig'
$flasherPath = Require-File (Join-Path $buildRoot 'flasher_args.json') 'flasher_args.json'
$partitionCsv = Require-File (Join-Path $repoRoot 'firmware/apps/voice_terminal/partitions.csv') 'Tracked partitions.csv'
Assert-PublicSdkconfig $sdkconfigPath
if ((Get-Sha256 $sdkconfigPath) -ne $provenance.sdkconfig_sha256) {
    throw 'Generated sdkconfig does not match build provenance.'
}
if ((Get-Sha256 $flasherPath) -ne $provenance.flasher_args_sha256) {
    throw 'flasher_args.json does not match build provenance.'
}
if ((Get-Sha256 $partitionCsv) -ne $provenance.partitions_csv_sha256) {
    throw 'Tracked partitions.csv does not match build provenance.'
}

$flasher = Get-Content -LiteralPath $flasherPath -Raw | ConvertFrom-Json
if ($flasher.extra_esptool_args.chip -ne 'esp32s3') { throw 'Flasher target must be esp32s3.' }
$expected = [ordered]@{
    '0x0'      = [ordered]@{ role = 'bootloader'; file = 'bootloader.bin'; max_bytes = 0x8000; source = 'idf_build_output' }
    '0x8000'   = [ordered]@{ role = 'partition_table'; file = 'partition-table.bin'; max_bytes = 0x1000; source = 'idf_build_output' }
    '0x10000'  = [ordered]@{ role = 'application'; file = 'rva_voice_terminal.bin'; max_bytes = 0x400000; source = 'idf_build_output' }
    '0x410000' = [ordered]@{ role = 'speech_models'; file = 'srmodels.bin'; max_bytes = 0x80000; source = 'idf_build_output' }
    '0x800000' = [ordered]@{ role = 'font_assets'; file = 'font_assets.bin'; max_bytes = 0x800000; source = 'versioned_font_image' }
}
$actualOffsets = @($flasher.flash_files.PSObject.Properties.Name)
if (@($actualOffsets | Where-Object { -not $expected.Contains($_) }).Count -ne 0 -or
    @($expected.Keys | Where-Object { $_ -notin $actualOffsets }).Count -ne 0) {
    throw 'flasher_args.json must contain exactly the five public firmware partitions.'
}
$provenanceByRole = @{}
foreach ($record in $provenance.artifacts) {
    if ($provenanceByRole.ContainsKey([string]$record.role)) { throw 'Build provenance has duplicate artifact roles.' }
    $provenanceByRole[[string]$record.role] = $record
}

$stage = Join-Path ([IO.Path]::GetTempPath()) ('rva-firmware-public-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    $manifestArtifacts = @()
    foreach ($offset in $expected.Keys) {
        $definition = $expected[$offset]
        $relative = [string]$flasher.flash_files.$offset
        $sourcePath = Resolve-BuildArtifact $buildRoot $relative
        $file = Get-Item -LiteralPath $sourcePath
        if ($file.Length -le 0 -or $file.Length -gt $definition.max_bytes) {
            throw "$($definition.role) image size $($file.Length) is outside its partition capacity."
        }
        if (-not $provenanceByRole.ContainsKey($definition.role)) {
            throw "Build provenance is missing $($definition.role)."
        }
        $record = $provenanceByRole[$definition.role]
        if (-not $record.included -or $record.offset -ne $offset -or $record.path -ne $relative -or
            [int64]$record.bytes -ne [int64]$file.Length -or $record.sha256 -ne (Get-Sha256 $sourcePath)) {
            throw "$($definition.role) image does not match build provenance."
        }
        if ($definition.role -eq 'partition_table') { Assert-PartitionTable $sourcePath }
        $sourceId = $null
        if ($definition.role -eq 'font_assets') { $sourceId = Assert-FontImage $sourcePath }
        $destination = Join-Path $stage $definition.file
        Copy-Item -LiteralPath $sourcePath -Destination $destination
        $artifact = [ordered]@{
            role = $definition.role
            offset = $offset
            file = $definition.file
            bytes = [int64]$file.Length
            sha256 = Get-Sha256 $destination
            source = $definition.source
            included = $true
        }
        if ($sourceId) { $artifact['source_id'] = $sourceId }
        $manifestArtifacts += $artifact
    }

    $noticeManifestPath = Require-File (Join-Path $PSScriptRoot 'public_bundle_notices.json') 'Bundle notice manifest'
    $noticeManifest = Get-Content -LiteralPath $noticeManifestPath -Raw | ConvertFrom-Json
    if ($noticeManifest.schema_version -ne 1 -or @($noticeManifest.notices).Count -eq 0) {
        throw 'Bundle notice manifest is invalid.'
    }
    $manifestDocuments = @()
    $seenBundleNames = @{}
    foreach ($notice in @($noticeManifest.notices)) {
        $bundleName = [string]$notice.bundle_file
        $repositoryPath = [string]$notice.source_path
        if ([string]::IsNullOrWhiteSpace($bundleName) -or [IO.Path]::GetFileName($bundleName) -ne $bundleName -or
            $seenBundleNames.ContainsKey($bundleName)) {
            throw 'Bundle notice manifest has an invalid or duplicate bundle filename.'
        }
        $seenBundleNames[$bundleName] = $true
        $sourcePath = Require-File (Join-Path $repoRoot $repositoryPath) 'Release notice'
        $repoPrefix = $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        if (-not $sourcePath.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Release notice escapes Product repository: $repositoryPath"
        }
        $actualNoticeDigest = Get-Sha256 $sourcePath
        $expectedNoticeDigest = if ($notice.PSObject.Properties.Name -contains 'sha256') {
            [string]$notice.sha256
        } else {
            $null
        }
        if ($expectedNoticeDigest -and $expectedNoticeDigest -ne $actualNoticeDigest) {
            throw "Release notice digest mismatch: $repositoryPath"
        }
        $destination = Join-Path $stage $bundleName
        Copy-Item -LiteralPath $sourcePath -Destination $destination
        $manifestDocuments += [ordered]@{
            file = $bundleName
            source = $repositoryPath
            sha256 = $actualNoticeDigest
        }
    }

    $dependencyLock = Require-File (Join-Path $repoRoot 'firmware/apps/voice_terminal/dependencies.lock') 'Firmware dependency lock'
    $supportManifestPath = Require-File (Join-Path $PSScriptRoot 'public_bundle_support.json') 'Bundle support manifest'
    $supportManifest = Get-Content -LiteralPath $supportManifestPath -Raw | ConvertFrom-Json
    if ($supportManifest.schema_version -ne 1 -or @($supportManifest.files).Count -eq 0) {
        throw 'Bundle support manifest is invalid.'
    }
    $manifestSupportFiles = @()
    foreach ($support in @($supportManifest.files)) {
        $bundleName = [string]$support.bundle_file
        $repositoryPath = [string]$support.source_path
        $role = [string]$support.role
        if ([string]::IsNullOrWhiteSpace($role) -or [string]::IsNullOrWhiteSpace($bundleName) -or
            [IO.Path]::GetFileName($bundleName) -ne $bundleName -or $seenBundleNames.ContainsKey($bundleName)) {
            throw 'Bundle support manifest has an invalid or duplicate entry.'
        }
        $seenBundleNames[$bundleName] = $true
        $sourcePath = Require-File (Join-Path $repoRoot $repositoryPath) 'Release support file'
        $repoPrefix = $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        if (-not $sourcePath.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Release support file escapes Product repository: $repositoryPath"
        }
        $destination = Join-Path $stage $bundleName
        Copy-Item -LiteralPath $sourcePath -Destination $destination
        $manifestSupportFiles += [ordered]@{
            role = $role
            file = $bundleName
            source = $repositoryPath
            sha256 = Get-Sha256 $destination
        }
    }
    $manifest = [ordered]@{
        schema_version = 1
        product = 'realtime-voice-agent'
        firmware = 'rva_voice_terminal'
        target = 'esp32s3'
        source_revision = $repositoryState.source_revision
        public_config = $true
        sdkconfig_sha256 = Get-Sha256 $sdkconfigPath
        partitions_csv_sha256 = Get-Sha256 $partitionCsv
        dependency_lock_sha256 = Get-Sha256 $dependencyLock
        font_asset_source = [ordered]@{
            kind = [string]$provenance.font_asset_source.kind
            package_sha256 = [string]$provenance.font_asset_source.package_sha256
        }
        flash_settings = [ordered]@{
            mode = [string]$flasher.flash_settings.flash_mode
            size = [string]$flasher.flash_settings.flash_size
            frequency = [string]$flasher.flash_settings.flash_freq
        }
        provisioning = [ordered]@{
            schema_version = 1
            partition = [ordered]@{
                label = 'nvs'
                offset = '0x9000'
                size = '0x6000'
            }
        }
        artifacts = $manifestArtifacts
        notices = $manifestDocuments
        support_files = $manifestSupportFiles
    }
    $manifestPath = Join-Path $stage 'manifest.json'
    Write-Utf8Lf $manifestPath ($manifest | ConvertTo-Json -Depth 8)
    $checksumLines = @()
    Get-ChildItem -LiteralPath $stage -File | Sort-Object Name | ForEach-Object {
        $checksumLines += "$(Get-Sha256 $_.FullName)  $($_.Name)"
    }
    Write-Utf8Lf (Join-Path $stage 'SHA256SUMS') ($checksumLines -join "`n")
    New-DeterministicZip $stage $outputPath
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}

$bundle = Get-Item -LiteralPath $outputPath
Write-Host "Public firmware bundle: $outputPath"
Write-Host "Bytes: $($bundle.Length)"
Write-Host "SHA-256: $(Get-Sha256 $outputPath)"

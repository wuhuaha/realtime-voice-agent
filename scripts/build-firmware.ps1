[CmdletBinding()]
param(
    [switch]$Clean,
    [string]$BuildDir = "firmware/apps/voice_terminal/build-local",
    [string]$Sdkconfig,
    [switch]$SkipSize,
    [switch]$ReleaseArtifacts,
    [string]$FontPackage
)

$ErrorActionPreference = "Stop"

function Resolve-RepoPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) {
        return [IO.Path]::GetFullPath($Path)
    }
    return [IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
}

function Require-File([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description not found: $Path"
    }
    return [IO.Path]::GetFullPath($Path)
}

function Require-Directory([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Description not found: $Path"
    }
    return [IO.Path]::GetFullPath($Path)
}

function Assert-Under([string]$Path, [string]$Root, [string]$Description) {
    $rootWithSeparator = $Root.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $Path.StartsWith($rootWithSeparator, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Description must stay under ${Root}: $Path"
    }
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-GitBuildState([string]$Root) {
    $revision = (& git -C $Root rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $revision -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Unable to resolve Product source revision."
    }
    $trackedStatus = @(& git -C $Root status --porcelain=v1 --untracked-files=no)
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect Product tracked tree state." }
    $worktreeStatus = @(& git -C $Root status --porcelain=v1 --untracked-files=normal)
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect Product worktree state." }
    return [ordered]@{
        source_revision = $revision.ToLowerInvariant()
        tracked_tree_clean = ($trackedStatus.Count -eq 0)
        worktree_clean = ($worktreeStatus.Count -eq 0)
    }
}

function Write-Utf8Lf([string]$Path, [string]$Content) {
    $normalized = ($Content -replace "`r`n", "`n").TrimEnd("`r", "`n") + "`n"
    [IO.File]::WriteAllText($Path, $normalized, [Text.UTF8Encoding]::new($false))
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$projectRoot = Require-Directory (Join-Path $repoRoot "firmware/apps/voice_terminal") "voice_terminal project"
$sourceVerifier = Require-File (Join-Path $repoRoot "firmware/tools/verify-source.py") "ESP-IDF source verifier"
$fontBuilder = Require-File (Join-Path $projectRoot "tools/build_font_assets.py") "font asset builder"
$buildStateStart = Get-GitBuildState $repoRoot

# The source checkout is kept outside the Product Git tree. RVA_IDF_PATH is the
# portable override; the sibling workspace path is the known local checkout.
$idfCandidates = @()
if ($env:RVA_IDF_PATH) { $idfCandidates += $env:RVA_IDF_PATH }
if ($env:IDF_PATH) { $idfCandidates += $env:IDF_PATH }
$idfCandidates += (Join-Path $repoRoot "../local/toolchains/esp-idf-v5.5.2")
$idfPath = $null
foreach ($candidate in $idfCandidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Container)) {
        $idfPath = Require-Directory $candidate "ESP-IDF checkout"
        break
    }
}
if (-not $idfPath) {
    throw "Pinned ESP-IDF v5.5.2 checkout not found. Set RVA_IDF_PATH to the checkout from third_party/sources.lock.yaml."
}

# Do not call export.ps1 here. It exports every chip tool and fails when an
# unrelated tool (for example riscv32-esp-elf) is absent. This project targets
# ESP32-S3, so use the exact installed tools needed by its build.
$toolsCandidates = @()
if ($env:RVA_IDF_TOOLS_PATH) { $toolsCandidates += $env:RVA_IDF_TOOLS_PATH }
if ($env:IDF_TOOLS_PATH) { $toolsCandidates += $env:IDF_TOOLS_PATH }
$toolsCandidates += "D:\Espressif"
$toolsCandidates += (Join-Path $env:USERPROFILE ".espressif")
$toolsRoot = $null
$pythonPath = $null
foreach ($candidate in $toolsCandidates) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Container)) { continue }
    $pythonCandidates = Get-ChildItem -LiteralPath (Join-Path $candidate "python_env") -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "idf5.5_*" } | Sort-Object Name -Descending
    foreach ($pythonEnv in $pythonCandidates) {
        $candidatePython = Join-Path $pythonEnv.FullName "Scripts/python.exe"
        if (Test-Path -LiteralPath $candidatePython -PathType Leaf) {
            $toolsRoot = [IO.Path]::GetFullPath($candidate)
            $pythonPath = [IO.Path]::GetFullPath($candidatePython)
            break
        }
    }
    if ($pythonPath) { break }
}
if (-not $pythonPath) {
    throw "ESP-IDF Python environment (idf5.5_*) not found. Set RVA_IDF_TOOLS_PATH to the installed tools root."
}

$toolStore = Join-Path $toolsRoot "tools"
if (-not (Test-Path -LiteralPath $toolStore -PathType Container)) { $toolStore = $toolsRoot }
$cmakePath = Get-ChildItem -LiteralPath (Join-Path $toolStore "cmake") -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName "bin/cmake.exe") } | Sort-Object Name -Descending |
    Select-Object -First 1 -ExpandProperty FullName
$ninjaPath = Get-ChildItem -LiteralPath (Join-Path $toolStore "ninja") -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName "ninja.exe") } | Sort-Object Name -Descending |
    Select-Object -First 1 -ExpandProperty FullName
$xtensaRoot = Get-ChildItem -LiteralPath (Join-Path $toolStore "xtensa-esp-elf") -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName "xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc.exe") } |
    Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $cmakePath -or -not $ninjaPath -or -not $xtensaRoot) {
    throw "Required ESP32-S3 tools are incomplete under $toolsRoot (cmake, ninja, xtensa-esp-elf)."
}
$cmakePath = Require-File (Join-Path $cmakePath "bin/cmake.exe") "pinned CMake"
$ninjaPath = Require-File (Join-Path $ninjaPath "ninja.exe") "pinned Ninja"
$xtensaBin = Require-Directory (Join-Path $xtensaRoot "xtensa-esp-elf/bin") "Xtensa toolchain"
$idfPy = Require-File (Join-Path $idfPath "tools/idf.py") "idf.py"

# Verify the source identity with the repository's locked revision before CMake
# sees it. uv is the Product's pinned Python runner and does not depend on IDF
# activation state.
& uv run --project $repoRoot python $sourceVerifier --source-id esp-idf --checkout $idfPath
if ($LASTEXITCODE -ne 0) { throw "Pinned ESP-IDF source verification failed." }

$buildPath = Resolve-RepoPath $BuildDir
Assert-Under $buildPath $projectRoot "Build directory"
if ($Clean -and (Test-Path -LiteralPath $buildPath)) {
    Remove-Item -LiteralPath $buildPath -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $buildPath | Out-Null

if ($Sdkconfig) {
    $sdkconfigPath = Resolve-RepoPath $Sdkconfig
    Assert-Under $sdkconfigPath $projectRoot "SDKCONFIG"
    if (-not (Test-Path -LiteralPath $sdkconfigPath -PathType Leaf)) {
        throw "Explicit SDKCONFIG does not exist: $sdkconfigPath"
    }
    $sdkconfigDefaults = $null
} else {
    $sdkconfigPath = Join-Path $buildPath "sdkconfig"
    $sdkconfigDefaults = Join-Path $projectRoot "sdkconfig.defaults"
}

$env:IDF_PATH = $idfPath
$env:IDF_TOOLS_PATH = $toolsRoot
$env:IDF_PYTHON_ENV_PATH = Split-Path $pythonPath -Parent | Split-Path -Parent
$env:ESP_IDF_VERSION = "5.5.2"
$env:PYTHONUTF8 = "1"
$certifiPath = (& $pythonPath -c "import certifi; print(certifi.where())").Trim()
if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $certifiPath -PathType Leaf)) {
    $env:SSL_CERT_FILE = $certifiPath
}
$romElfs = Get-ChildItem -LiteralPath (Join-Path $toolStore "esp-rom-elfs") -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
if ($romElfs) { $env:ESP_ROM_ELF_DIR = $romElfs.FullName }

# Put pinned tools first so an unrelated system CMake/Ninja cannot win lookup.
$pythonBin = Split-Path $pythonPath -Parent
$cmakeBin = Split-Path $cmakePath -Parent
$ninjaBin = Split-Path $ninjaPath -Parent
$toolPathEntries = @(
    $cmakeBin,
    $ninjaBin,
    $xtensaBin,
    (Join-Path $toolStore "esp32ulp-elf/2.38_20240113/esp32ulp-elf/bin"),
    (Join-Path $toolStore "idf-exe/1.0.3"),
    $pythonBin
)
$env:PATH = (($toolPathEntries | Where-Object { Test-Path -LiteralPath $_ -PathType Container }) -join ";") + ";" + $env:PATH

Push-Location $projectRoot
try {
    $targetConfigured = (Test-Path -LiteralPath $sdkconfigPath -PathType Leaf) -and
        (Select-String -LiteralPath $sdkconfigPath -Pattern '^CONFIG_IDF_TARGET="esp32s3"$' -Quiet)
    if (-not $targetConfigured) {
        $setTargetArgs = @($idfPy, "-B", $buildPath, "-DSDKCONFIG=$sdkconfigPath")
        if ($sdkconfigDefaults) { $setTargetArgs += "-DSDKCONFIG_DEFAULTS=$sdkconfigDefaults" }
        $setTargetArgs += "set-target", "esp32s3"
        & $pythonPath @setTargetArgs
        if ($LASTEXITCODE -ne 0) { throw "idf.py set-target failed." }
    }

    $buildArgs = @($idfPy, "-B", $buildPath, "-DSDKCONFIG=$sdkconfigPath")
    if ($sdkconfigDefaults) { $buildArgs += "-DSDKCONFIG_DEFAULTS=$sdkconfigDefaults" }
    $buildArgs += "build"
    Write-Host "Building Product firmware"
    Write-Host "  source:  $repoRoot"
    Write-Host "  target:  esp32s3"
    Write-Host "  IDF:     $idfPath"
    Write-Host "  Python:  $pythonPath"
    Write-Host "  CMake:   $cmakePath"
    Write-Host "  Ninja:   $ninjaPath"
    Write-Host "  config:  $sdkconfigPath"
    Write-Host "  build:   $buildPath"
    & $pythonPath @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "idf.py build failed." }

    if ($ReleaseArtifacts) {
        if ($FontPackage) {
            $fontPackagePath = Require-File (Resolve-RepoPath $FontPackage) "explicit pinned font package"
            & $pythonPath $fontBuilder `
                --output (Join-Path $buildPath "font_assets.bin") `
                --cache-dir (Join-Path $buildPath "font-assets-cache") `
                --font-package $fontPackagePath
            if ($LASTEXITCODE -ne 0) { throw "Pinned font package verification/build failed." }
        } else {
            $fontArgs = @($idfPy, "-B", $buildPath, "-DSDKCONFIG=$sdkconfigPath")
            if ($sdkconfigDefaults) { $fontArgs += "-DSDKCONFIG_DEFAULTS=$sdkconfigDefaults" }
            $fontArgs += "font-assets"
            & $pythonPath @fontArgs
            if ($LASTEXITCODE -ne 0) { throw "idf.py font-assets failed." }
        }
    }

    if (-not $SkipSize) {
        $sizeArgs = @($idfPy, "-B", $buildPath, "-DSDKCONFIG=$sdkconfigPath", "size")
        & $pythonPath @sizeArgs
        if ($LASTEXITCODE -ne 0) { throw "idf.py size failed." }
    }
} finally {
    Pop-Location
}

$artifact = Require-File (Join-Path $buildPath "rva_voice_terminal.bin") "firmware application artifact"
$digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $artifact).Hash.ToLowerInvariant()
$buildStateEnd = Get-GitBuildState $repoRoot
$flasherPath = Join-Path $buildPath "flasher_args.json"
$partitionCsv = Join-Path $projectRoot "partitions.csv"
$artifactDefinitions = @(
    [ordered]@{ role = "bootloader"; offset = "0x0"; path = "bootloader/bootloader.bin" },
    [ordered]@{ role = "partition_table"; offset = "0x8000"; path = "partition_table/partition-table.bin" },
    [ordered]@{ role = "application"; offset = "0x10000"; path = "rva_voice_terminal.bin" },
    [ordered]@{ role = "speech_models"; offset = "0x410000"; path = "srmodels/srmodels.bin" },
    [ordered]@{ role = "font_assets"; offset = "0x800000"; path = "font_assets.bin" }
)
$provenanceArtifacts = @()
$allArtifactsPresent = $true
foreach ($definition in $artifactDefinitions) {
    $path = Join-Path $buildPath $definition.path
    $present = Test-Path -LiteralPath $path -PathType Leaf
    if (-not $present) { $allArtifactsPresent = $false }
    $record = [ordered]@{
        role = $definition.role
        offset = $definition.offset
        path = $definition.path
        included = $present
    }
    if ($present) {
        $file = Get-Item -LiteralPath $path
        $record["bytes"] = [int64]$file.Length
        $record["sha256"] = Get-Sha256 $path
    }
    $provenanceArtifacts += $record
}
$flasherPresent = Test-Path -LiteralPath $flasherPath -PathType Leaf
$sameRevision = $buildStateStart.source_revision -eq $buildStateEnd.source_revision
$fontPackagePath = if ($FontPackage) { Require-File (Resolve-RepoPath $FontPackage) "explicit pinned font package" } else {
    Join-Path $buildPath "font-assets-cache/78__xiaozhi-fonts-v1.6.0.zip"
}
$fontAssetSource = [ordered]@{
    kind = if ($FontPackage) { "explicit_pinned_package" } else { "pinned_registry_download" }
    package_sha256 = if (Test-Path -LiteralPath $fontPackagePath -PathType Leaf) { Get-Sha256 $fontPackagePath } else { $null }
}
$releaseEligible = (
    $ReleaseArtifacts -and
    $buildStateStart.tracked_tree_clean -and $buildStateStart.worktree_clean -and
    $buildStateEnd.tracked_tree_clean -and $buildStateEnd.worktree_clean -and
    $sameRevision -and $allArtifactsPresent -and $flasherPresent
)
$provenance = [ordered]@{
    schema_version = 1
    source_revision = $buildStateStart.source_revision
    target = "esp32s3"
    release_artifacts_requested = [bool]$ReleaseArtifacts
    release_eligible = [bool]$releaseEligible
    build_start = $buildStateStart
    build_end = $buildStateEnd
    sdkconfig_sha256 = Get-Sha256 $sdkconfigPath
    partitions_csv_sha256 = Get-Sha256 $partitionCsv
    flasher_args_sha256 = if ($flasherPresent) { Get-Sha256 $flasherPath } else { $null }
    font_asset_source = $fontAssetSource
    artifacts = $provenanceArtifacts
}
Write-Utf8Lf (Join-Path $buildPath "build-provenance.json") ($provenance | ConvertTo-Json -Depth 8)
Write-Host "Build succeeded: $artifact"
Write-Host "SHA-256: $digest"
Write-Host "Release eligible: $releaseEligible"

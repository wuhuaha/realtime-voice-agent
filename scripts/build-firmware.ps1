[CmdletBinding()]
param(
    [switch]$Clean,
    [string]$BuildDir = "firmware/apps/voice_terminal/build-local",
    [string]$Sdkconfig,
    [switch]$SkipSize
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

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$projectRoot = Require-Directory (Join-Path $repoRoot "firmware/apps/voice_terminal") "voice_terminal project"
$sourceVerifier = Require-File (Join-Path $repoRoot "firmware/tools/verify-source.py") "ESP-IDF source verifier"

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
Write-Host "Build succeeded: $artifact"
Write-Host "SHA-256: $digest"

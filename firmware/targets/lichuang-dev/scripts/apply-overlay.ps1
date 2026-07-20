param(
    [string]$Checkout = "",
    [string]$Config = ""
)

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }
$Checkout = (Resolve-Path -LiteralPath $Checkout).Path
if ($Config) { $Config = (Resolve-Path -LiteralPath $Config).Path }
$expectedRevision = "7b190b78e4f8dfef14126f6cd478c134b3cd3cd8"
$overlayDirectory = Join-Path $integration "overlay"

if (-not (Test-Path -LiteralPath (Join-Path $Checkout ".git"))) { throw "Xiaozhi checkout not found: $Checkout" }
$actualRevision = (& git -C $Checkout rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualRevision -ne $expectedRevision) {
    throw "Xiaozhi revision mismatch. Expected $expectedRevision, found $actualRevision"
}

$patches = @(Get-ChildItem -LiteralPath $overlayDirectory -Filter "*.patch" -File | Sort-Object Name)
if ($patches.Count -eq 0) { throw "No overlay patches found: $overlayDirectory" }

function Get-AppliedOverlayPatchNames {
    $temporaryIndex = [System.IO.Path]::GetTempFileName()
    Remove-Item -LiteralPath $temporaryIndex
    $previousIndex = $env:GIT_INDEX_FILE
    $applied = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    try {
        $env:GIT_INDEX_FILE = $temporaryIndex
        & git -C $Checkout read-tree HEAD >$null 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Failed to initialize temporary overlay index" }
        & git -C $Checkout add -u -- . >$null 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Failed to snapshot Xiaozhi working tree" }

        foreach ($patch in @($patches | Sort-Object Name -Descending)) {
            & git -C $Checkout apply --cached --reverse --check $patch.FullName 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            & git -C $Checkout apply --cached --reverse $patch.FullName >$null 2>$null
            if ($LASTEXITCODE -ne 0) { throw "Failed to inspect applied overlay: $($patch.Name)" }
            [void]$applied.Add($patch.Name)
        }
        return $applied
    } finally {
        $env:GIT_INDEX_FILE = $previousIndex
        Remove-Item -LiteralPath $temporaryIndex -ErrorAction SilentlyContinue
    }
}

[string[]]$appliedPatches = @(Get-AppliedOverlayPatchNames)
foreach ($patch in $patches) {
    if ($patch.Name -in $appliedPatches) {
        Write-Host "Overlay already applied: $($patch.Name)"
        continue
    }

    & git -C $Checkout apply --check $patch.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Overlay patch does not apply cleanly: $($patch.Name); preserve and inspect upstream changes"
    }
    & git -C $Checkout apply $patch.FullName
    if ($LASTEXITCODE -ne 0) { throw "Failed to apply overlay patch: $($patch.Name)" }
    Write-Host "Applied overlay: $($patch.Name)"
}

$overlayFiles = Join-Path $integration "overlay-files"
if (Test-Path -LiteralPath $overlayFiles) {
    Get-ChildItem -LiteralPath $overlayFiles -Recurse -File | ForEach-Object {
        $relative = [System.IO.Path]::GetRelativePath($overlayFiles, $_.FullName)
        $destination = Join-Path $Checkout $relative
        $parent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    }
}

& (Join-Path $PSScriptRoot "generate-local-config.ps1") -Checkout $Checkout -Config $Config
if (-not $?) { throw "Failed to generate local configuration" }
Write-Host "Xiaozhi overlay ready at pinned revision $expectedRevision."

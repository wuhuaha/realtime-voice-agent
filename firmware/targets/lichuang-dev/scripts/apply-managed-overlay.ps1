param([string]$Checkout = "")

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }
$Checkout = (Resolve-Path -LiteralPath $Checkout).Path

$managedComponentNames = @("78__esp-wifi-connect", "78__esp-ml307")
foreach ($componentName in $managedComponentNames) {
    $component = Join-Path $Checkout "managed_components/$componentName"
    if (-not (Test-Path -LiteralPath $component)) {
        throw "Managed component is not resolved: $component"
    }
}

$patchDirectory = Join-Path $integration "overlay-managed"
$patches = @(Get-ChildItem -LiteralPath $patchDirectory -Filter "*.patch" -File | Sort-Object Name)
if ($patches.Count -eq 0) { throw "No managed component patches found: $patchDirectory" }

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    "voice-agent-managed-overlay-" + [guid]::NewGuid().ToString("N"))
$temporaryCheckout = Join-Path $temporaryDirectory "checkout"
try {
    $temporaryManagedComponents = Join-Path $temporaryCheckout "managed_components"
    [void](New-Item -ItemType Directory -Path $temporaryManagedComponents -Force)
    foreach ($componentName in $managedComponentNames) {
        Copy-Item -LiteralPath (Join-Path $Checkout "managed_components/$componentName") `
            -Destination $temporaryManagedComponents -Recurse
    }

    $fullyApplied = $true
    foreach ($patch in @($patches | Sort-Object Name -Descending)) {
        & git -C $temporaryCheckout apply --recount --reverse $patch.FullName 2>$null
        if ($LASTEXITCODE -ne 0) {
            $fullyApplied = $false
            break
        }
    }

    if ($fullyApplied) {
        Write-Host "Managed overlay stack already applied: $($patches.Count) patches."
        return
    }

    foreach ($patch in $patches) {
        & git -C $Checkout apply --recount --check $patch.FullName
        if ($LASTEXITCODE -ne 0) {
            throw "Managed overlay is partial or drifted; cannot apply cleanly at: $($patch.Name)"
        }
        & git -C $Checkout apply --recount $patch.FullName
        if ($LASTEXITCODE -ne 0) { throw "Failed to apply managed overlay: $($patch.Name)" }
        Write-Host "Applied managed overlay: $($patch.Name)"
    }
} finally {
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

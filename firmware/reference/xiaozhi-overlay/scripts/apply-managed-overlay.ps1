param([string]$Checkout = "")

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }
$Checkout = (Resolve-Path -LiteralPath $Checkout).Path

$component = Join-Path $Checkout "managed_components/78__esp-wifi-connect"
if (-not (Test-Path -LiteralPath $component)) {
    throw "Managed component is not resolved: $component"
}

$patchDirectory = Join-Path $integration "overlay-managed"
$patches = @(Get-ChildItem -LiteralPath $patchDirectory -Filter "*.patch" -File | Sort-Object Name)
if ($patches.Count -eq 0) { throw "No managed component patches found: $patchDirectory" }

foreach ($patch in $patches) {
    & git -C $Checkout apply --reverse --check $patch.FullName 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Managed overlay already applied: $($patch.Name)"
        continue
    }
    & git -C $Checkout apply --check $patch.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Managed overlay does not apply cleanly: $($patch.Name)"
    }
    & git -C $Checkout apply $patch.FullName
    if ($LASTEXITCODE -ne 0) { throw "Failed to apply managed overlay: $($patch.Name)" }
    Write-Host "Applied managed overlay: $($patch.Name)"
}

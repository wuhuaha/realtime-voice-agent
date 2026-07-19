param(
    [string]$Checkout = "",
    [string]$Config = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }
$Checkout = (Resolve-Path -LiteralPath $Checkout).Path
if ($Config) {
    $Config = (Resolve-Path -LiteralPath $Config).Path
} else {
    $Config = (Resolve-Path -LiteralPath (Join-Path $integration ".env.local")).Path
}
& (Join-Path $PSScriptRoot "verify-source-contract.ps1") -Checkout $Checkout
if ($LASTEXITCODE -ne 0) { throw "Pinned Xiaozhi source contract failed" }
if (-not $env:IDF_PATH) { throw "Activate ESP-IDF >= 5.5.2 before running this script" }
$idf = Join-Path $env:IDF_PATH "tools/idf.py"
$versionText = (& python $idf --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $versionText -notmatch 'v?(\d+)\.(\d+)\.(\d+)') { throw "Unable to determine ESP-IDF version" }
$version = [version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
if ($version -lt [version]"5.5.2") { throw "Pinned Xiaozhi manifest requires ESP-IDF >= 5.5.2; active version is $version" }

& (Join-Path $PSScriptRoot "apply-overlay.ps1") -Checkout $Checkout -Config $Config
if ($LASTEXITCODE -ne 0) { throw "Overlay preparation failed" }

$buildDir = Join-Path $Checkout "build-voice-agent"
$sdkconfig = Join-Path $Checkout "sdkconfig.voice-agent"
$configInputPath = "$sdkconfig.config-input.sha256"
$defaults = "sdkconfig.defaults;sdkconfig.defaults.esp32s3;$($integration.Replace('\', '/'))/sdkconfig.defaults"
$configInputFiles = @(
    $Config,
    (Join-Path $Checkout "sdkconfig.defaults"),
    (Join-Path $Checkout "sdkconfig.defaults.esp32s3"),
    (Join-Path $integration "sdkconfig.defaults")
) + @(
    Get-ChildItem -LiteralPath (Join-Path $integration "overlay") -Filter "*.patch" -File
) + @(
    Get-ChildItem -LiteralPath (Join-Path $integration "overlay-managed") -Filter "*.patch" -File
) + @(
    Get-ChildItem -LiteralPath (Join-Path $integration "overlay-files") -Recurse -File
)
$configInputRecords = foreach ($inputFile in $configInputFiles) {
    $resolvedInput = (Resolve-Path -LiteralPath $inputFile).Path
    "$resolvedInput=$((Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedInput).Hash)"
}
$configInputBytes = [Text.Encoding]::UTF8.GetBytes(($configInputRecords -join "`n"))
$configInput = [Convert]::ToHexString(
    [Security.Cryptography.SHA256]::HashData($configInputBytes)).ToLowerInvariant()
$hasReusableConfiguration = (Test-Path -LiteralPath $sdkconfig) -or
                            (Test-Path -LiteralPath $buildDir)
if (-not $Clean -and $hasReusableConfiguration) {
    if (-not (Test-Path -LiteralPath $configInputPath)) {
        throw "Existing build has no config-input guard; rerun with -Clean"
    }
    $previousConfigInput = (Get-Content -Raw -LiteralPath $configInputPath).Trim()
    if ($previousConfigInput -ne $configInput) {
        throw "Configuration inputs changed; rerun with -Clean"
    }
}
if ($Clean -and (Test-Path -LiteralPath $buildDir)) {
    $resolvedCheckout = (Resolve-Path $Checkout).Path
    $resolvedBuild = (Resolve-Path $buildDir).Path
    $checkoutPrefix = $resolvedCheckout.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedBuild.StartsWith($checkoutPrefix)) { throw "Refusing to clean outside checkout" }
    Remove-Item -LiteralPath $buildDir -Recurse -Force
}
if ($Clean) {
    foreach ($generatedConfig in @($sdkconfig, "$sdkconfig.old", $configInputPath)) {
        if (Test-Path -LiteralPath $generatedConfig) { Remove-Item -LiteralPath $generatedConfig -Force }
    }
}

Push-Location $Checkout
try {
    $targetConfigured = (Test-Path -LiteralPath $sdkconfig) -and
        (Select-String -LiteralPath $sdkconfig -Pattern '^CONFIG_IDF_TARGET="esp32s3"$' -Quiet)
    if (-not $targetConfigured) {
        & python $idf -B $buildDir "-DSDKCONFIG=$sdkconfig" "-DSDKCONFIG_DEFAULTS=$defaults" "-DBOARD_NAME=lichuang-dev" "-DBOARD_TYPE=lichuang-dev" set-target esp32s3
        if ($LASTEXITCODE -ne 0) { throw "idf.py set-target failed" }
    }
    else {
        Write-Host "Reusing configured ESP-IDF target esp32s3."
    }
    & (Join-Path $PSScriptRoot "apply-managed-overlay.ps1") -Checkout $Checkout
    if ($LASTEXITCODE -ne 0) { throw "Managed component overlay failed" }
    & (Join-Path $PSScriptRoot "test-udp-media-source-contract.ps1") -Checkout $Checkout
    if ($LASTEXITCODE -ne 0) { throw "UDP final-source contract failed" }
    & python $idf -B $buildDir "-DSDKCONFIG=$sdkconfig" "-DSDKCONFIG_DEFAULTS=$defaults" "-DBOARD_NAME=lichuang-dev" "-DBOARD_TYPE=lichuang-dev" build
    if ($LASTEXITCODE -ne 0) { throw "idf.py build failed" }
    & python $idf -B $buildDir "-DSDKCONFIG=$sdkconfig" size
    if ($LASTEXITCODE -ne 0) { throw "idf.py size failed" }
    [IO.File]::WriteAllText($configInputPath, "$configInput`n", [Text.UTF8Encoding]::new($false))
} finally {
    Pop-Location
}

& (Join-Path $PSScriptRoot "assert-generated-config.ps1") -Checkout $Checkout -Config $Config

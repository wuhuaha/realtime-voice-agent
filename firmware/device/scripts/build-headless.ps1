param([switch]$Clean)

$ErrorActionPreference = "Stop"
$deviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRoot = (Resolve-Path (Join-Path $deviceRoot "../..")).Path
if (-not $env:IDF_PATH) { throw "Activate the ESP-IDF pinned by third_party/sources.lock.yaml" }
$sourceVerifier = Join-Path $repoRoot "firmware/tools/verify-source.py"
& uv run --project $repoRoot python $sourceVerifier --source-id esp-idf --checkout $env:IDF_PATH
if ($LASTEXITCODE -ne 0) { throw "Pinned ESP-IDF source contract failed" }
$idf = Join-Path $env:IDF_PATH "tools/idf.py"

$buildDir = Join-Path $deviceRoot "build-headless"
$sdkconfig = Join-Path $deviceRoot "sdkconfig.headless"
if ($Clean -and (Test-Path -LiteralPath $buildDir)) {
    $resolvedBuild = (Resolve-Path -LiteralPath $buildDir).Path
    if (-not $resolvedBuild.StartsWith($deviceRoot + [IO.Path]::DirectorySeparatorChar)) {
        throw "Refusing to clean outside firmware/device"
    }
    Remove-Item -LiteralPath $buildDir -Recurse -Force
}
if ($Clean) {
    foreach ($generated in @($sdkconfig, "$sdkconfig.old")) {
        if (Test-Path -LiteralPath $generated) { Remove-Item -LiteralPath $generated -Force }
    }
}

Push-Location $deviceRoot
try {
    $configured = (Test-Path -LiteralPath $sdkconfig) -and
        (Select-String -LiteralPath $sdkconfig -Pattern '^CONFIG_IDF_TARGET="esp32s3"$' -Quiet)
    if (-not $configured) {
        & python $idf -B $buildDir "-DSDKCONFIG=$sdkconfig" `
            "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults" set-target esp32s3
        if ($LASTEXITCODE -ne 0) { throw "idf.py set-target failed" }
    }
    & python $idf -B $buildDir "-DSDKCONFIG=$sdkconfig" build
    if ($LASTEXITCODE -ne 0) { throw "idf.py build failed" }
    & python $idf -B $buildDir "-DSDKCONFIG=$sdkconfig" size
    if ($LASTEXITCODE -ne 0) { throw "idf.py size failed" }
} finally {
    Pop-Location
}

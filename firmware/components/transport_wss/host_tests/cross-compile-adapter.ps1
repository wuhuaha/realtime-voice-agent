param(
    [Parameter(Mandatory = $true)][string]$IdfPath,
    [Parameter(Mandatory = $true)][string]$WebsocketInclude,
    [Parameter(Mandatory = $true)][string]$SdkconfigInclude,
    [Parameter(Mandatory = $true)][string]$Compiler
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "../../../..")).Path
foreach ($required in @($IdfPath, $WebsocketInclude, $SdkconfigInclude, $Compiler)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Missing cross-compile prerequisite: $required" }
}

$components = Join-Path $IdfPath "components"
$includes = @(
    $SdkconfigInclude,
    (Join-Path $repo "firmware/components/voice_protocol/include"),
    (Join-Path $repo "firmware/components/transport_wss/include"),
    $WebsocketInclude,
    (Join-Path $components "newlib/platform_include"),
    (Join-Path $components "freertos/config/include"),
    (Join-Path $components "freertos/config/include/freertos"),
    (Join-Path $components "freertos/config/xtensa/include"),
    (Join-Path $components "freertos/FreeRTOS-Kernel/include"),
    (Join-Path $components "freertos/FreeRTOS-Kernel/portable/xtensa/include"),
    (Join-Path $components "freertos/FreeRTOS-Kernel/portable/xtensa/include/freertos"),
    (Join-Path $components "freertos/esp_additions/include"),
    (Join-Path $components "esp_common/include"),
    (Join-Path $components "esp_rom/include"),
    (Join-Path $components "esp_rom/esp32s3/include"),
    (Join-Path $components "esp_rom/esp32s3/include/esp32s3"),
    (Join-Path $components "esp_event/include"),
    (Join-Path $components "esp_system/include"),
    (Join-Path $components "esp_timer/include"),
    (Join-Path $components "esp_hw_support/include"),
    (Join-Path $components "heap/include"),
    (Join-Path $components "soc/include"),
    (Join-Path $components "soc/esp32s3/include"),
    (Join-Path $components "soc/esp32s3/register"),
    (Join-Path $components "hal/platform_port/include"),
    (Join-Path $components "hal/include"),
    (Join-Path $components "hal/esp32s3/include"),
    (Join-Path $components "xtensa/include"),
    (Join-Path $components "xtensa/esp32s3/include"),
    (Join-Path $components "lwip/include"),
    (Join-Path $components "lwip/lwip/src/include"),
    (Join-Path $components "lwip/port/include"),
    (Join-Path $components "lwip/port/freertos/include"),
    (Join-Path $components "lwip/port/esp32xx/include"),
    (Join-Path $components "lwip/port/esp32xx/include/arch"),
    (Join-Path $components "lwip/port/esp32xx/include/sys"),
    (Join-Path $components "tcp_transport/include")
)
$object = Join-Path $env:TEMP ("rva-esp-wss-" + [guid]::NewGuid().ToString("N") + ".o")
$arguments = @(
    '-DESP_PLATFORM', '-DIDF_VER="v5.5.2"', '-D_GNU_SOURCE', '-mlongcalls',
    '-std=gnu++17', '-fno-exceptions', '-fno-rtti', '-Wall', '-Wextra', '-Werror',
    '-c', (Join-Path $repo "firmware/components/transport_wss/esp_websocket_client_port.cc"),
    '-o', $object
)
foreach ($include in $includes) { $arguments += @('-I', $include) }
try {
    & $Compiler @arguments
    if ($LASTEXITCODE -ne 0) { throw "ESP32-S3 adapter cross compilation failed" }
} finally {
    if (Test-Path -LiteralPath $object) { Remove-Item -LiteralPath $object -Force }
}

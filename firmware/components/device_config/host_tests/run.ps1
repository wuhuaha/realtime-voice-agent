$ErrorActionPreference = "Stop"

$componentRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$compiler = Get-Command clang++, g++ -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $compiler) {
    throw "No supported C++ host compiler found (expected clang++ or g++)"
}

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    "rva-device-config-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$executable = Join-Path $temporaryDirectory "device_config_test.exe"
$previousPath = $env:PATH
try {
    & $compiler.Source -std=c++17 -Wall -Wextra -Werror -O2 `
        -I (Join-Path $componentRoot "include") `
        (Join-Path $componentRoot "device_config.cc") `
        (Join-Path $PSScriptRoot "device_config_test.cc") `
        -o $executable
    if ($LASTEXITCODE -ne 0) {
        throw "device_config host compilation failed"
    }

    $env:PATH = "$(Split-Path -Parent $compiler.Source)$([IO.Path]::PathSeparator)$previousPath"
    & $executable
    if ($LASTEXITCODE -ne 0) {
        throw "device_config host test failed with exit code $LASTEXITCODE"
    }
} finally {
    $env:PATH = $previousPath
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

$ErrorActionPreference = "Stop"

$deviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$contracts = Join-Path $deviceRoot "components/voice_contracts"
$core = Join-Path $deviceRoot "components/voice_core"
$test = Join-Path $deviceRoot "host_tests/session_gate_test.cc"
$compiler = Get-Command clang++, g++ -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $compiler) {
    throw "No supported C++ host compiler found (expected clang++ or g++)"
}

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    "voice-agent-headless-contract-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$executable = Join-Path $temporaryDirectory "headless_contract_test.exe"
$previousPath = $env:PATH
try {
    & $compiler.Source -std=c++17 -Wall -Wextra -Werror -O2 `
        -I (Join-Path $contracts "include") -I (Join-Path $core "include") `
        (Join-Path $contracts "transport_profile.cc") `
        (Join-Path $core "session_gate.cc") $test -o $executable
    if ($LASTEXITCODE -ne 0) { throw "Headless contract compilation failed" }

    $hostCompilerDirectory = Split-Path -Parent $compiler.Source
    $env:PATH = "$hostCompilerDirectory$([IO.Path]::PathSeparator)$previousPath"
    & $executable
    if ($LASTEXITCODE -ne 0) { throw "Headless contract test failed" }
} finally {
    $env:PATH = $previousPath
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

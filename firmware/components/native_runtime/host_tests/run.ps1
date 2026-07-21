$ErrorActionPreference = "Stop"

$runtimeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$componentsRoot = (Resolve-Path (Join-Path $runtimeRoot "..")).Path
$cpp = Get-Command g++, clang++ -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $cpp) { throw "A C++ host compiler is required" }

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("rva-runtime-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
try {
    $executable = Join-Path $temporaryDirectory "response_interrupt_gate_test.exe"
    & $cpp.Source -std=c++17 -fno-exceptions -Wall -Wextra -Werror -O2 -pthread `
        -I (Join-Path $runtimeRoot "include") `
        -I (Join-Path $componentsRoot "voice_protocol/include") `
        (Join-Path $PSScriptRoot "response_interrupt_gate_test.cc") -o $executable
    if ($LASTEXITCODE -ne 0) { throw "Runtime host compilation failed" }
    & $executable
    if ($LASTEXITCODE -ne 0) { throw "Runtime host test failed with exit code $LASTEXITCODE" }
} finally {
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

$ErrorActionPreference = "Stop"

$runtimeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$componentsRoot = (Resolve-Path (Join-Path $runtimeRoot "..")).Path
$cpp = Get-Command g++, clang++ -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $cpp) { throw "A C++ host compiler is required" }

& (Join-Path $PSScriptRoot "runtime_lifecycle_source_test.ps1")

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("rva-runtime-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
try {
    $tests = @(
        @{
            Name = "playback_state_test"
            Sources = @(
                (Join-Path $runtimeRoot "playback_state.cc"),
                (Join-Path $PSScriptRoot "playback_state_test.cc")
            )
        },
        @{
            Name = "uplink_pipeline_test"
            Sources = @((Join-Path $PSScriptRoot "uplink_pipeline_test.cc"))
        }
    )
    foreach ($test in $tests) {
        $executable = Join-Path $temporaryDirectory ($test.Name + ".exe")
        & $cpp.Source -std=c++17 -fno-exceptions -Wall -Wextra -Werror -O2 -pthread `
            -I (Join-Path $runtimeRoot "include") `
            -I (Join-Path $componentsRoot "voice_protocol/include") `
            @($test.Sources) -o $executable
        if ($LASTEXITCODE -ne 0) { throw "$($test.Name) compilation failed" }
        & $executable
        if ($LASTEXITCODE -ne 0) { throw "$($test.Name) failed with exit code $LASTEXITCODE" }
    }
} finally {
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

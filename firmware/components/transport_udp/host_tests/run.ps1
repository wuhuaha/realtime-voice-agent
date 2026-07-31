$ErrorActionPreference = "Stop"

$transportRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$componentsRoot = (Resolve-Path (Join-Path $transportRoot "..")).Path
$repositoryRoot = (Resolve-Path (Join-Path $componentsRoot "../..")).Path
$cpp = Get-Command g++, clang++ -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $cpp) { throw "A C++ host compiler is required" }

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("rva-udp-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$previousPath = $env:PATH
try {
    $tests = @(
        @{
            Name = "udp_session_test"
            Sources = @(
                (Join-Path $componentsRoot "voice_contracts/udp_wire.cc"),
                (Join-Path $transportRoot "replay_window.cc"),
                (Join-Path $transportRoot "jitter_buffer.cc"),
                (Join-Path $transportRoot "playout_queue.cc"),
                (Join-Path $transportRoot "udp_session.cc"),
                (Join-Path $PSScriptRoot "udp_session_test.cc")
            )
        },
        @{
            Name = "playout_queue_test"
            Sources = @(
                (Join-Path $transportRoot "playout_queue.cc"),
                (Join-Path $PSScriptRoot "playout_queue_test.cc")
            )
        }
    )

    $env:PATH = "$(Split-Path -Parent $cpp.Source)$([IO.Path]::PathSeparator)$previousPath"
    foreach ($test in $tests) {
        $executable = Join-Path $temporaryDirectory ($test.Name + ".exe")
        & $cpp.Source -std=c++20 -Wall -Wextra -Werror -O2 -pthread `
            -I (Join-Path $transportRoot "include") `
            -I (Join-Path $componentsRoot "voice_contracts/include") `
            @($test.Sources) -o $executable
        if ($LASTEXITCODE -ne 0) { throw "$($test.Name) compilation failed" }

        & $executable
        if ($LASTEXITCODE -ne 0) { throw "$($test.Name) failed with exit code $LASTEXITCODE" }
    }

    if ([string]::IsNullOrWhiteSpace($env:IDF_PATH)) {
        throw "Activate the ESP-IDF pinned by third_party/sources.lock.yaml"
    }

    $sourceVerifier = Join-Path $repositoryRoot "firmware/tools/verify-source.py"
    $fixtureScript = Join-Path $PSScriptRoot "run_gcm_fixtures.py"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $uv) {
        & $uv.Source run --frozen --project $repositoryRoot python $sourceVerifier `
            --source-id esp-idf --checkout $env:IDF_PATH
    } else {
        $python = Get-Command python, python3 -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $python) { throw "uv or Python is required to run the GCM fixtures" }
        & $python.Source $sourceVerifier --source-id esp-idf --checkout $env:IDF_PATH
    }
    if ($LASTEXITCODE -ne 0) { throw "Pinned ESP-IDF source contract failed" }

    if ($null -ne $uv) {
        & $uv.Source run --frozen --project $repositoryRoot python $fixtureScript
    } else {
        & $python.Source $fixtureScript
    }
    if ($LASTEXITCODE -ne 0) { throw "GCM fixture tests failed with exit code $LASTEXITCODE" }
} finally {
    $env:PATH = $previousPath
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

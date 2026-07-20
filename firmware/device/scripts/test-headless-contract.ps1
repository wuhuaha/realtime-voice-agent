$ErrorActionPreference = "Stop"

$deviceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$contracts = Join-Path $deviceRoot "components/voice_contracts"
$core = Join-Path $deviceRoot "components/voice_core"
$repoRoot = (Resolve-Path (Join-Path $deviceRoot "../..")).Path
$sessionTest = Join-Path $deviceRoot "host_tests/session_gate_test.cc"
$wireTest = Join-Path $deviceRoot "host_tests/udp_wire_fixture_test.cc"
$positive = Get-Content -Raw -LiteralPath (
    Join-Path $repoRoot "protocol/xiaozhi_udp_v1/fixtures/positive.json") | ConvertFrom-Json
$negative = Get-Content -Raw -LiteralPath (
    Join-Path $repoRoot "protocol/xiaozhi_udp_v1/fixtures/negative.json") | ConvertFrom-Json
$compiler = Get-Command clang++, g++ -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $compiler) {
    throw "No supported C++ host compiler found (expected clang++ or g++)"
}

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    "voice-agent-headless-contract-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$sessionExecutable = Join-Path $temporaryDirectory "session_contract_test.exe"
$wireExecutable = Join-Path $temporaryDirectory "udp_wire_fixture_test.exe"
$previousPath = $env:PATH
try {
    & $compiler.Source -std=c++17 -Wall -Wextra -Werror -O2 -pthread `
        -I (Join-Path $contracts "include") -I (Join-Path $core "include") `
        (Join-Path $contracts "transport_profile.cc") `
        (Join-Path $core "session_gate.cc") (Join-Path $core "session.cc") `
        $sessionTest -o $sessionExecutable
    if ($LASTEXITCODE -ne 0) { throw "Headless contract compilation failed" }

    & $compiler.Source -std=c++17 -Wall -Wextra -Werror -O2 `
        -I (Join-Path $contracts "include") `
        (Join-Path $contracts "udp_wire.cc") $wireTest -o $wireExecutable
    if ($LASTEXITCODE -ne 0) { throw "UDP wire fixture compilation failed" }

    $hostCompilerDirectory = Split-Path -Parent $compiler.Source
    $env:PATH = "$hostCompilerDirectory$([IO.Path]::PathSeparator)$previousPath"
    & $sessionExecutable
    if ($LASTEXITCODE -ne 0) { throw "Headless contract test failed" }
    foreach ($vector in $positive.vectors) {
        & $wireExecutable positive $vector.datagram_hex $vector.header_hex `
            $vector.ciphertext_and_tag_hex $vector.salt_hex $vector.nonce_hex `
            $vector.direction ([string]$vector.fields.flags) `
            $vector.fields.media_id_hex ([string]$vector.fields.media_epoch) `
            ([string]$vector.fields.sequence) ([string]$vector.fields.timestamp) `
            ([string]$vector.fields.generation) ([string]$vector.fields.payload_length)
        if ($LASTEXITCODE -ne 0) {
            throw "UDP wire fixture rejected positive vector: $($vector.id)"
        }
    }
    foreach ($vector in $negative.vectors) {
        $mode = if ($vector.reject_stage -eq "parser") {
            "parser-reject"
        } else {
            "auth-candidate"
        }
        & $wireExecutable $mode $vector.datagram_hex
        if ($LASTEXITCODE -ne 0) {
            throw "UDP wire fixture admission mismatch: $($vector.id)"
        }
    }
    & $wireExecutable typed-boundaries
    if ($LASTEXITCODE -ne 0) { throw "UDP typed wire boundaries failed" }
} finally {
    $env:PATH = $previousPath
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

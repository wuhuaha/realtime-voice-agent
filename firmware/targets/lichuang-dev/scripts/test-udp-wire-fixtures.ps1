param([string]$Checkout = "")

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
if (-not $Checkout) { $Checkout = Join-Path $repoRoot "external/xiaozhi-esp32" }
$Checkout = (Resolve-Path -LiteralPath $Checkout).Path
$source = Join-Path $integration "tests/udp_wire_fixture_test.cc"
$includeDirectory = Join-Path $Checkout "main/protocols"
$wireHeader = Join-Path $includeDirectory "voice_udp_wire.h"
if (-not (Test-Path -LiteralPath $wireHeader)) {
    throw "Final checkout UDP wire helper is not prepared: $wireHeader"
}
$positive = Get-Content -Raw -LiteralPath (
    Join-Path $repoRoot "protocol/xiaozhi_udp_v1/fixtures/positive.json") | ConvertFrom-Json
$negative = Get-Content -Raw -LiteralPath (
    Join-Path $repoRoot "protocol/xiaozhi_udp_v1/fixtures/negative.json") | ConvertFrom-Json

$compiler = Get-Command clang++, g++ -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $compiler) {
    throw "No supported C++ host compiler found (expected clang++ or g++)"
}

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    "voice-agent-udp-wire-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$executable = Join-Path $temporaryDirectory "udp_wire_fixture_test.exe"
$previousPath = $env:PATH
try {
    & $compiler.Source -std=c++17 -Wall -Wextra -Werror -O2 `
        -I $includeDirectory $source -o $executable
    if ($LASTEXITCODE -ne 0) { throw "C++ UDP fixture parser compilation failed" }

    # ESP-IDF prepends its cross-toolchain DLLs. Prefer the host compiler's
    # runtime directory while executing the host fixture binary.
    $hostCompilerDirectory = Split-Path -Parent $compiler.Source
    $env:PATH = "$hostCompilerDirectory$([IO.Path]::PathSeparator)$previousPath"
    foreach ($vector in $positive.vectors) {
        & $executable positive $vector.datagram_hex $vector.header_hex `
            $vector.ciphertext_and_tag_hex $vector.salt_hex $vector.nonce_hex `
            $vector.direction ([string]$vector.fields.flags) `
            $vector.fields.media_id_hex ([string]$vector.fields.media_epoch) `
            ([string]$vector.fields.sequence) `
            ([string]$vector.fields.timestamp) ([string]$vector.fields.generation) `
            ([string]$vector.fields.payload_length)
        if ($LASTEXITCODE -ne 0) {
            throw "C++ UDP fixture parser rejected positive vector: $($vector.id)"
        }
    }
    foreach ($vector in $negative.vectors) {
        $mode = if ($vector.reject_stage -eq "parser") {
            "parser-reject"
        } else {
            "auth-candidate"
        }
        & $executable $mode $vector.datagram_hex
        if ($LASTEXITCODE -ne 0) {
            throw "C++ UDP fixture admission mismatch: $($vector.id)"
        }
    }
    & $executable keepalive-nonzero-timestamp
    if ($LASTEXITCODE -ne 0) {
        throw "C++ UDP parser rejected non-zero KEEPALIVE timestamp"
    }
} finally {
    $env:PATH = $previousPath
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

Write-Host "C++ UDP canonical wire fixture parser passed."

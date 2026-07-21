$ErrorActionPreference = "Stop"

$transportRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$componentsRoot = (Resolve-Path (Join-Path $transportRoot "..")).Path
$protocolRoot = Join-Path $componentsRoot "voice_protocol"
$cpp = Get-Command g++, clang++ -ErrorAction SilentlyContinue | Select-Object -First 1
$cc = Get-Command gcc, clang -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $cpp -or $null -eq $cc) {
    throw "C and C++ host compilers are required"
}

$idfCandidates = @()
if ($env:IDF_PATH) { $idfCandidates += $env:IDF_PATH }
$idfCandidates += @(Get-ChildItem "D:\Espressif\frameworks" -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | ForEach-Object FullName)
$cJsonSource = $null
foreach ($candidate in $idfCandidates) {
    $source = Join-Path $candidate "components/json/cJSON/cJSON.c"
    if (Test-Path -LiteralPath $source) {
        $cJsonSource = $source
        break
    }
}
if ($null -eq $cJsonSource) { throw "ESP-IDF cJSON source was not found" }
$cJsonInclude = Split-Path -Parent $cJsonSource

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("rva-wss-contract-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$cJsonObject = Join-Path $temporaryDirectory "cJSON.o"
$executable = Join-Path $temporaryDirectory "wss_contract_test.exe"
$previousPath = $env:PATH
try {
    & $cc.Source -std=c11 -O2 -I $cJsonInclude -c $cJsonSource -o $cJsonObject
    if ($LASTEXITCODE -ne 0) { throw "cJSON host compilation failed" }
    & $cpp.Source -std=c++17 -fno-exceptions -Wall -Wextra -Werror -O2 -pthread `
        -I (Join-Path $protocolRoot "include") -I (Join-Path $transportRoot "include") -I $cJsonInclude `
        (Join-Path $protocolRoot "control.cc") (Join-Path $protocolRoot "media_header.cc") `
        (Join-Path $transportRoot "wss_owner.cc") (Join-Path $transportRoot "wss_session.cc") `
        (Join-Path $PSScriptRoot "wss_contract_test.cc") $cJsonObject -o $executable
    if ($LASTEXITCODE -ne 0) { throw "WSS contract host compilation failed" }

    $env:PATH = "$(Split-Path -Parent $cpp.Source)$([IO.Path]::PathSeparator)$previousPath"
    & $executable
    if ($LASTEXITCODE -ne 0) { throw "WSS contract host test failed with exit code $LASTEXITCODE" }
} finally {
    $env:PATH = $previousPath
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

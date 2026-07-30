$ErrorActionPreference = "Stop"

$appRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$cpp = Get-Command g++, clang++ -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $cpp) { throw "A C++ host compiler is required" }

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    "rva-voice-terminal-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
try {
    $variants = @(
        @{
            Name = "unset_fails_safe_to_wss"
            Defines = @()
        },
        @{
            Name = "configured_wss"
            Defines = @("-DCONFIG_RVA_DEFAULT_MEDIA_PROFILE_WSS=1")
        },
        @{
            Name = "configured_udp"
            Defines = @(
                "-DCONFIG_RVA_DEFAULT_MEDIA_PROFILE_UDP=1",
                "-DRVA_EXPECT_UDP=1"
            )
        },
        @{
            Name = "conflicting_config_fails_safe_to_wss"
            Defines = @(
                "-DCONFIG_RVA_DEFAULT_MEDIA_PROFILE_WSS=1",
                "-DCONFIG_RVA_DEFAULT_MEDIA_PROFILE_UDP=1"
            )
        }
    )

    foreach ($variant in $variants) {
        $executable = Join-Path $temporaryDirectory ($variant.Name + ".exe")
        & $cpp.Source -std=c++17 -Wall -Wextra -Werror -O2 `
            -I (Join-Path $appRoot "main") `
            @($variant.Defines) `
            (Join-Path $PSScriptRoot "default_media_profile_test.cc") -o $executable
        if ($LASTEXITCODE -ne 0) { throw "$($variant.Name) compilation failed" }
        & $executable
        if ($LASTEXITCODE -ne 0) { throw "$($variant.Name) failed with exit code $LASTEXITCODE" }
    }
} finally {
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

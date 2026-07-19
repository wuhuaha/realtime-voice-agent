$ErrorActionPreference = "Stop"

$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
$validator = Join-Path $PSScriptRoot "assert-generated-config.ps1"

$tokens = $null
$parseErrors = $null
[void][Management.Automation.Language.Parser]::ParseFile(
    $validator,
    [ref]$tokens,
    [ref]$parseErrors)
if ($parseErrors.Count -ne 0) {
    throw "Validator AST parse failed: $($parseErrors[0].Message)"
}

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    "voice-agent-config-validator-" + [guid]::NewGuid().ToString("N"))
$checkout = Join-Path $temporaryDirectory "checkout"
$header = Join-Path $checkout "main/voice_agent_local_config.h"
$validConfig = Join-Path $temporaryDirectory "valid.env"
$invalidConfig = Join-Path $temporaryDirectory "invalid.env"
$utf8NoBom = [Text.UTF8Encoding]::new($false)
$token = "validator-token-" + [guid]::NewGuid().ToString("N")
$password = "validator-password-" + [guid]::NewGuid().ToString("N")
$hostExecutable = (Get-Process -Id $PID).Path

try {
    [void](New-Item -ItemType Directory -Path (Split-Path -Parent $header))
    [IO.File]::WriteAllText($header, @"
#define VOICE_AGENT_LOCAL_LAB 1
#define VOICE_AGENT_WS_URL "wss://fixture.invalid/ws"
#define VOICE_AGENT_WS_TOKEN "fixture"
#define VOICE_AGENT_WIFI_SSID "fixture"
#define VOICE_AGENT_WIFI_PASSWORD "fixture"
"@, $utf8NoBom)
    [IO.File]::WriteAllText($validConfig, @"
XIAOZHI_LAB_TOKEN=$token
XIAOZHI_WIFI_PASSWORD=$password
"@, $utf8NoBom)
    [IO.File]::WriteAllText($invalidConfig, @"
XIAOZHI_WIFI_PASSWORD=$password
"@, $utf8NoBom)

    & $hostExecutable -NoLogo -NoProfile -NonInteractive -File $validator `
        -Checkout $checkout -Config $invalidConfig *> $null
    if ($LASTEXITCODE -eq 0) {
        throw "Standalone validator accepted a missing secret"
    }

    & $hostExecutable -NoLogo -NoProfile -NonInteractive -File $validator `
        -Checkout $checkout -Config $validConfig *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Standalone validator left exit code $LASTEXITCODE"
    }

    $missingPattern = "validator-no-match-" + [guid]::NewGuid().ToString("N")
    & git -C $repoRoot grep -F --quiet -- $missingPattern
    if ($LASTEXITCODE -ne 1) {
        throw "Unable to establish stale git grep exit code"
    }

    & $validator -Checkout $checkout -Config $validConfig *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Call-operator validator left exit code $LASTEXITCODE"
    }

    Write-Host "Generated configuration validator process and caller contract passed."
} finally {
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

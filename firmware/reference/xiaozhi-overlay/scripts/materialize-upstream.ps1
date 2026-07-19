param(
    [switch]$VerifyOnly,
    [switch]$VerifyInputsOnly
)

$ErrorActionPreference = "Stop"
$integration = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $integration "../../..")).Path
$script = Join-Path $PSScriptRoot "materialize-upstream.py"
$arguments = @("run", "--project", $repoRoot, "python", $script)
if ($VerifyOnly -and $VerifyInputsOnly) { throw "Choose only one verification mode" }
if ($VerifyOnly) { $arguments += "--verify-only" }
if ($VerifyInputsOnly) { $arguments += "--verify-inputs-only" }

& uv @arguments
if ($LASTEXITCODE -ne 0) { throw "Xiaozhi upstream materialization failed" }

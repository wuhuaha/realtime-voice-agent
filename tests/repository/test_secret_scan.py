from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("check_secrets", ROOT / "scripts" / "check_secrets.py")
assert SPEC is not None and SPEC.loader is not None
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def test_detects_provider_key(tmp_path: Path) -> None:
    candidate = tmp_path / "settings.py"
    candidate.write_text('API_KEY = "sk-' + 'examplelongsecretvalue"\n', encoding="utf-8")
    assert CHECK.scan(tmp_path, ("settings.py",)) == ["settings.py:1: possible provider_api_key"]


def test_ignores_env_templates(tmp_path: Path) -> None:
    candidate = tmp_path / ".env.example"
    candidate.write_text("API_KEY=replace-with-provider-key\n", encoding="utf-8")
    assert CHECK.scan(tmp_path, (".env.example",)) == []


def test_scans_env_templates_for_real_values(tmp_path: Path) -> None:
    candidate = tmp_path / ".env.example"
    candidate.write_text("PASSWORD=actual-" + "production-password\n", encoding="utf-8")
    assert CHECK.scan(tmp_path, (".env.example",)) == [
        ".env.example:1: possible assigned_secret"
    ]


def test_ignores_dynamic_validator_and_constant_assertion(tmp_path: Path) -> None:
    candidate = tmp_path / "contract.ps1"
    candidate.write_text(
        '$token = "validator-token-prefix-long-enough-" + [guid]::NewGuid()\n'
        'const cJSON* fencing_token = cJSON_GetObjectItemCaseSensitive(root, "fencing_token");\n'
        "$source.Contains('voice_agent_token = VOICE_AGENT_WS_TOKEN')\n",
        encoding="utf-8",
    )
    assert CHECK.scan(tmp_path, ("contract.ps1",)) == []


def test_scans_product_configuration_and_document_sources(tmp_path: Path) -> None:
    secret_value = "actual-" + "configuration-secret-value"
    candidates = {
        "guide.html": f'<div data-token="{secret_value}"></div>\n',
        "sdkconfig.defaults": 'CONFIG_WIFI_PASSWORD="actual-default-password"\n',
        "Kconfig": f'config RVA_WIFI_PASSWORD\n    string\n    default "{secret_value}"\n',
        "CMakeLists.txt": f'set(PROVIDER_API_KEY "{secret_value}")\n',
    }
    for name, content in candidates.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    findings = CHECK.scan(tmp_path, tuple(candidates))

    assert findings == [
        "guide.html:1: possible assigned_secret",
        "sdkconfig.defaults:1: possible assigned_secret",
        "Kconfig:3: possible assigned_secret",
        "CMakeLists.txt:1: possible assigned_secret",
    ]

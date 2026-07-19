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

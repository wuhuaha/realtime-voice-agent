from __future__ import annotations

from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (
    SERVER_ROOT / "apps" / "session_director" / "src",
    SERVER_ROOT / "apps" / "realtime_worker" / "src",
    SERVER_ROOT / "packages" / "voice_contracts" / "src",
)


def test_server_source_excludes_frozen_direct_protocols() -> None:
    forbidden = ("aiortc", "webrtc", "aimp", "datachannel", "direct_session_v1", '"/v1/direct"')
    violations: list[str] = []
    for root in SOURCE_ROOTS:
        for path in root.rglob("*.py"):
            content = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                if token in content:
                    violations.append(f"{path.relative_to(SERVER_ROOT)}: {token}")
    assert not violations, "legacy runtime references found:\n" + "\n".join(violations)


def test_realtime_worker_does_not_depend_on_legacy_packages() -> None:
    project = (SERVER_ROOT / "apps" / "realtime_worker" / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert "aiortc" not in project
    assert "jsonschema" not in project
    assert "turn-detector" not in project

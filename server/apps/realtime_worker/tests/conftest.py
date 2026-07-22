from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_local_runtime_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's ignored live-provider .env must not change offline tests."""

    monkeypatch.setenv("VOICE_RUNNER", "deterministic")
    monkeypatch.setenv("VOICE_LEGACY_XIAOZHI_ENABLED", "false")
    monkeypatch.setenv("VOICE_XIAOZHI_UDP_ENABLED", "false")
    monkeypatch.setenv("VOICE_XIAOZHI_TRANSPORT_POLICY", "force_wss")
    monkeypatch.setenv("VOICE_UDP_ADVERTISE_HOST", "")
    monkeypatch.setenv("VOICE_UDP_ADVERTISE_PORT", "0")
    monkeypatch.setenv("VOICE_INTERNAL_TOKEN", "internal-test-token")
    monkeypatch.setenv("VOICE_GRANT_SIGNING_KEY", "test-signing-key-with-32-bytes")
    monkeypatch.setenv("VOICE_LAB_TOKEN", "lab-test-token")

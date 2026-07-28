from __future__ import annotations

from pydantic import SecretStr
from realtime_worker.config import Settings
from realtime_worker.providers.deepseek_llm import create_deepseek_llm


def test_deepseek_llm_uses_configured_stream_read_deadline(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeLlm:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    from livekit.plugins import openai

    monkeypatch.setattr(openai, "LLM", FakeLlm)
    settings = Settings(
        runner="livekit",
        deepseek_api_key=SecretStr("test-key"),
        deepseek_read_timeout_seconds=17.0,
    )

    result = create_deepseek_llm(settings)

    assert isinstance(result, FakeLlm)
    timeout = captured["timeout"]
    assert timeout.read == 17.0
    assert timeout.connect == 15.0
    assert timeout.write == 10.0
    assert timeout.pool == 5.0

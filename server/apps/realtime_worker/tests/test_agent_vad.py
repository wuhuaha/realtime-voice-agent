from __future__ import annotations

from types import SimpleNamespace

import pytest
from livekit.agents import vad as vad_api
from pydantic import ValidationError
from realtime_worker import agent as agent_module
from realtime_worker.agent import LiveKitAgentRunner
from realtime_worker.config import Settings
from realtime_worker.vad import ResettingVAD

pytestmark = pytest.mark.unit


class FakeSession:
    def __init__(self) -> None:
        self.input = SimpleNamespace(audio=None)
        self.output = SimpleNamespace(audio=None)

    def on(self, _name: str, _handler: object) -> None:
        return None

    async def start(self, _agent: object) -> None:
        return None


def fake_vad() -> object:
    return SimpleNamespace(
        capabilities=vad_api.VADCapabilities(update_interval=0.032),
        model="fake",
        provider="fake",
    )


@pytest.mark.parametrize(
    ("settings_options", "expected_thresholds"),
    [
        ({}, (0.1, 0.1)),
        (
            {"vad_activation_threshold": 0.4, "vad_deactivation_threshold": 0.25},
            (0.4, 0.25),
        ),
    ],
)
@pytest.mark.asyncio
async def test_livekit_runner_passes_configured_threshold_to_silero(
    monkeypatch: pytest.MonkeyPatch,
    settings_options: dict[str, float],
    expected_thresholds: tuple[float, float],
) -> None:
    agent_module._load_vad.cache_clear()  # noqa: SLF001
    captured_vad_options: dict[str, object] = {}

    async def create_tts(_settings: Settings, *, tracer: object) -> object:
        return object()

    def load_vad(**options: object) -> object:
        captured_vad_options.update(options)
        return fake_vad()

    monkeypatch.setattr(agent_module, "configure_trace_logging", lambda: None)
    monkeypatch.setattr(agent_module, "create_tts", create_tts)
    monkeypatch.setattr(agent_module, "FunASRSTT", lambda _config, *, tracer: object())
    monkeypatch.setattr(agent_module, "create_deepseek_llm", lambda _settings: object())
    monkeypatch.setattr(agent_module.silero.VAD, "load", load_vad)
    captured_session_options: dict[str, object] = {}
    monkeypatch.setattr(
        agent_module,
        "AgentSession",
        lambda **options: captured_session_options.update(options) or FakeSession(),
    )
    monkeypatch.setattr(agent_module, "_DefaultAgent", lambda _consume_overlap: object())

    async def emit_segment(_frames: object) -> None:
        return None

    runner = LiveKitAgentRunner(
        Settings(
            _env_file=None,
            runner="livekit",
            deepseek_api_key="test-key",
            **settings_options,
        ),
        emit_segment,
        lambda _epoch: None,
    )
    await runner.start()

    assert captured_vad_options == {
        "force_cpu": True,
        "sample_rate": agent_module.PCM_SAMPLE_RATE,
        "activation_threshold": expected_thresholds[0],
        "deactivation_threshold": expected_thresholds[1],
    }
    assert isinstance(captured_session_options["vad"], ResettingVAD)


@pytest.mark.asyncio
async def test_livekit_runner_exposes_interruption_controls_to_agent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_session_options: dict[str, object] = {}

    async def create_tts(_settings: Settings, *, tracer: object) -> object:
        return object()

    monkeypatch.setattr(agent_module, "configure_trace_logging", lambda: None)
    monkeypatch.setattr(agent_module, "create_tts", create_tts)
    monkeypatch.setattr(agent_module, "FunASRSTT", lambda _config, *, tracer: object())
    monkeypatch.setattr(agent_module, "create_deepseek_llm", lambda _settings: object())
    monkeypatch.setattr(agent_module.silero.VAD, "load", lambda **_options: fake_vad())
    monkeypatch.setattr(
        agent_module,
        "AgentSession",
        lambda **options: captured_session_options.update(options) or FakeSession(),
    )
    monkeypatch.setattr(agent_module, "_DefaultAgent", lambda _consume_overlap: object())

    async def emit_segment(_frames: object) -> None:
        return None

    runner = LiveKitAgentRunner(
        Settings(
            _env_file=None,
            runner="livekit",
            deepseek_api_key="test-key",
            agent_interruption_enabled=False,
            agent_interruption_min_duration_seconds=1.4,
        ),
        emit_segment,
        lambda _epoch: None,
    )
    await runner.start()

    turn_handling = captured_session_options["turn_handling"]
    assert isinstance(turn_handling, dict)
    assert turn_handling["interruption"]["enabled"] is False
    assert turn_handling["interruption"]["min_duration"] == 1.4
    assert turn_handling["interruption"]["discard_audio_if_uninterruptible"] is False
    assert turn_handling["turn_detection"] == "vad"
    assert captured_session_options["aec_warmup_duration"] is None


def test_vad_thresholds_use_chinese_near_field_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICE_VAD_ACTIVATION_THRESHOLD", raising=False)
    monkeypatch.delenv("VOICE_VAD_DEACTIVATION_THRESHOLD", raising=False)

    settings = Settings(_env_file=None)

    assert settings.vad_activation_threshold == 0.1
    assert settings.vad_deactivation_threshold == 0.1
    assert settings.vad_idle_reset_seconds == 30.0


def test_vad_activation_threshold_accepts_explicit_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_VAD_ACTIVATION_THRESHOLD", "0.45")
    monkeypatch.setenv("VOICE_VAD_DEACTIVATION_THRESHOLD", "0.25")

    settings = Settings(_env_file=None)

    assert settings.vad_activation_threshold == 0.45
    assert settings.vad_deactivation_threshold == 0.25


@pytest.mark.parametrize("threshold", [0.1, 0.9])
def test_vad_activation_threshold_accepts_inclusive_boundaries(threshold: float) -> None:
    settings = Settings(
        _env_file=None,
        vad_activation_threshold=threshold,
        vad_deactivation_threshold=threshold,
    )

    assert settings.vad_activation_threshold == threshold


@pytest.mark.parametrize("threshold", [0.099, 0.901])
def test_vad_activation_threshold_rejects_values_outside_bounds(threshold: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, vad_activation_threshold=threshold)


def test_vad_deactivation_threshold_cannot_exceed_activation() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            vad_activation_threshold=0.3,
            vad_deactivation_threshold=0.31,
        )

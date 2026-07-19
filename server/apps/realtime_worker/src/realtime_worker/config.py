from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .audio import PCM_SAMPLE_RATE, PCM_SAMPLES
from .errors import ConfigurationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VOICE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    worker_id: str = "worker-local-1"
    environment: Literal["development", "production"] = Field(
        default="development",
        validation_alias=AliasChoices("VOICE_ENV", "VOICE_ENVIRONMENT"),
    )
    worker_public_ws_url: str = "ws://127.0.0.1:8081/v1/xiaozhi"
    bind_host: str = Field("127.0.0.1", validation_alias=AliasChoices("VOICE_WORKER_BIND_HOST", "VOICE_BIND_HOST"))
    bind_port: int = Field(
        8081,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("VOICE_WORKER_BIND_PORT", "VOICE_BIND_PORT"),
    )
    max_sessions: int = Field(
        5,
        ge=1,
        le=1024,
        validation_alias=AliasChoices("VOICE_WORKER_MAX_SESSIONS", "VOICE_MAX_SESSIONS"),
    )
    lab_token: SecretStr = SecretStr("replace-with-device-token")
    allow_lab_auth: bool = True
    grant_signing_key: SecretStr = SecretStr("replace-with-random-grant-key")
    internal_token: SecretStr = SecretStr("replace-with-random-internal-token")
    director_url: str = ""
    heartbeat_enabled: bool = True
    heartbeat_interval_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    provider_probe_interval_seconds: float = Field(default=10.0, ge=2.0, le=300.0)
    provider_probe_timeout_seconds: float = Field(default=3.0, gt=0, le=15.0)

    media_queue_frames: int = Field(default=12, ge=4, le=12)
    output_segment_max_seconds: int = Field(default=30, ge=5, le=120)
    max_control_bytes: int = Field(default=16 * 1024, ge=1024, le=64 * 1024)
    session_start_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    websocket_ping_interval_seconds: float = Field(default=10.0, gt=0, le=60)
    websocket_ping_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    xiaozhi_handshake_timeout_seconds: float = Field(default=8.0, gt=0, le=10)
    xiaozhi_media_queue_frames: int = Field(default=12, ge=4, le=12)
    xiaozhi_max_opus_bytes: int = Field(default=4096, ge=1275, le=16 * 1024)
    xiaozhi_queue_timeout_seconds: float = Field(default=0.24, gt=0, le=1)
    xiaozhi_audio_diagnostics: bool = False
    xiaozhi_audio_diagnostics_interval_packets: int = Field(default=50, ge=1, le=10_000)
    xiaozhi_transport_policy: Literal["auto", "prefer_device", "force_wss", "force_udp_for_test"] = "force_wss"
    xiaozhi_udp_enabled: bool = False
    xiaozhi_udp_bind_host: str = "0.0.0.0"
    xiaozhi_udp_bind_port: int = Field(default=8092, ge=0, le=65535)
    xiaozhi_udp_advertise_host: str = ""
    xiaozhi_udp_advertise_port: int = Field(default=0, ge=0, le=65535)
    xiaozhi_udp_probe_timeout_seconds: float = Field(default=3.0, gt=0, le=10)
    xiaozhi_udp_session_lifetime_seconds: int = Field(default=600, ge=30, le=3600)
    xiaozhi_udp_queue_datagrams: int = Field(default=32, ge=4, le=256)
    xiaozhi_udp_reorder_wait_ms: int = Field(default=30, ge=5, le=100)

    runner: Literal["deterministic", "livekit"] = "deterministic"
    agent_profile: str = "default"
    vad_activation_threshold: float = Field(default=0.6, ge=0.1, le=0.9)
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("VOICE_LLM_API_KEY", "VOICE_DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("VOICE_LLM_BASE_URL", "VOICE_DEEPSEEK_BASE_URL"),
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias=AliasChoices("VOICE_LLM_MODEL", "VOICE_DEEPSEEK_MODEL"),
    )
    funasr_ws_url: str = Field(
        default="ws://127.0.0.1:1111/v1/asr/stream",
        validation_alias=AliasChoices("VOICE_FUNASR_URL", "VOICE_FUNASR_WS_URL"),
    )
    funasr_protocol: Literal["standalone", "local", "funasr"] = "standalone"
    funasr_mode: str = "2pass"
    funasr_chunk_size: str = "8,8,4"
    funasr_audio_fs: int = 16_000
    funasr_queue_max_chunks: int = Field(default=16, ge=1, le=256)
    funasr_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    asr_recording_enabled: bool = False
    remote_cosyvoice_url: str = Field(
        default="http://127.0.0.1:2222",
        validation_alias=AliasChoices("VOICE_COSYVOICE_URL", "VOICE_REMOTE_COSYVOICE_URL"),
    )
    remote_cosyvoice_model: str = Field(
        default="cosyvoice3",
        validation_alias=AliasChoices("VOICE_COSYVOICE_MODEL", "VOICE_REMOTE_COSYVOICE_MODEL"),
    )
    remote_cosyvoice_voice: str = Field(
        default="mumu",
        validation_alias=AliasChoices("VOICE_COSYVOICE_VOICE", "VOICE_REMOTE_COSYVOICE_VOICE"),
    )
    remote_cosyvoice_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    remote_cosyvoice_max_concurrency: int = Field(default=1, ge=1, le=4)
    tts_provider: Literal["remote_cosyvoice", "mimo", "cosyvoice"] = "remote_cosyvoice"
    cosyvoice_url: str = "http://127.0.0.1:50000"
    cosyvoice_mode: str = "zero-shot"
    cosyvoice_speaker: str = ""
    cosyvoice_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    cosyvoice_max_concurrency: int = Field(default=1, ge=1, le=2)
    mimo_api_key: SecretStr | None = None
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_tts_model: str = "mimo-v2.5-tts"
    mimo_tts_voice: str = "冰糖"
    mimo_tts_style: str = ""
    mimo_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    mimo_max_concurrency: int = Field(default=1, ge=1, le=4)

    @property
    def funasr_chunk_sizes(self) -> tuple[int, int, int]:
        values = tuple(int(value.strip()) for value in self.funasr_chunk_size.split(","))
        if len(values) != 3 or any(value <= 0 for value in values):
            raise ValueError("VOICE_FUNASR_CHUNK_SIZE must contain three positive integers")
        return values  # type: ignore[return-value]

    @property
    def output_segment_max_frames(self) -> int:
        return self.output_segment_max_seconds * PCM_SAMPLE_RATE // PCM_SAMPLES

    def validate_runtime(self) -> None:
        for name, secret in (
            ("VOICE_INTERNAL_TOKEN", self.internal_token),
            ("VOICE_GRANT_SIGNING_KEY", self.grant_signing_key),
        ):
            if secret.get_secret_value().startswith("replace-with-"):
                raise ValueError(f"{name} must not use the repository placeholder")
        if len(self.grant_signing_key.get_secret_value().encode("utf-8")) < 16:
            raise ValueError("VOICE_GRANT_SIGNING_KEY must contain at least 16 bytes")
        if self.allow_lab_auth and self.lab_token.get_secret_value().startswith("replace-with-"):
            raise ValueError("VOICE_LAB_TOKEN must not use the repository placeholder")
        if self.environment == "production" and self.allow_lab_auth:
            raise ValueError("production requires VOICE_ALLOW_LAB_AUTH=false")
        if self.director_url and not self.heartbeat_enabled:
            raise ValueError("VOICE_HEARTBEAT_ENABLED must be true when VOICE_DIRECTOR_URL is configured")
        if self.environment == "production" and not self.director_url:
            raise ValueError("production requires VOICE_DIRECTOR_URL")
        public_ws_url = urlsplit(self.worker_public_ws_url)
        if public_ws_url.scheme not in {"ws", "wss"} or not public_ws_url.hostname:
            raise ValueError("VOICE_WORKER_PUBLIC_WS_URL must be an absolute ws:// or wss:// URL")
        if public_ws_url.path != "/v1/xiaozhi" or public_ws_url.query or public_ws_url.fragment:
            raise ValueError("VOICE_WORKER_PUBLIC_WS_URL must use the canonical /v1/xiaozhi path")
        if self.environment == "production" and public_ws_url.scheme != "wss":
            raise ValueError("production requires VOICE_WORKER_PUBLIC_WS_URL to use wss://")
        if self.xiaozhi_udp_enabled and not self.xiaozhi_udp_advertise_host:
            raise ValueError("VOICE_XIAOZHI_UDP_ADVERTISE_HOST is required when UDP is enabled")
        if self.xiaozhi_transport_policy == "force_udp_for_test" and not self.xiaozhi_udp_enabled:
            raise ValueError("force_udp_for_test requires VOICE_XIAOZHI_UDP_ENABLED=true")
        if self.runner != "livekit":
            return
        self.require_worker()

    def require_livekit(self) -> None:
        if self.deepseek_api_key is None:
            raise ConfigurationError("VOICE_LLM_API_KEY is required for the livekit runner")
        if self.deepseek_api_key.get_secret_value().startswith("replace-with-"):
            raise ConfigurationError("VOICE_LLM_API_KEY must not use the repository placeholder")

    def require_worker(self) -> None:
        self.require_livekit()
        if not self.funasr_ws_url:
            raise ConfigurationError("VOICE_FUNASR_URL is required")
        if self.tts_provider == "remote_cosyvoice":
            self.require_remote_cosyvoice_tts()
        elif self.tts_provider == "mimo":
            self.require_mimo_tts()
        else:
            self.require_cosyvoice_gateway()

    def require_remote_cosyvoice_tts(self) -> None:
        if not self.remote_cosyvoice_url:
            raise ConfigurationError("VOICE_REMOTE_COSYVOICE_URL/VOICE_COSYVOICE_URL is required")

    def require_mimo_tts(self) -> None:
        if self.mimo_api_key is None:
            raise ConfigurationError("VOICE_MIMO_API_KEY is required")

    def require_cosyvoice_gateway(self) -> None:
        if not self.cosyvoice_url:
            raise ConfigurationError("VOICE_COSYVOICE_URL is required")

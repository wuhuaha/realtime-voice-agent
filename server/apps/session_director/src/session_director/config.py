from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DirectorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VOICE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    director_bind_host: str = "127.0.0.1"
    director_bind_port: int = Field(default=8080, ge=1, le=65535)
    coordination_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://127.0.0.1:6379/0"
    coordination_prefix: str = "voice-agent"
    internal_token: SecretStr = SecretStr("replace-with-random-internal-token")
    grant_signing_key: SecretStr = SecretStr("replace-with-random-grant-key")
    device_bootstrap_token: SecretStr = SecretStr("replace-with-device-token")
    environment: Literal["development", "production"] = Field(
        default="development",
        validation_alias=AliasChoices("VOICE_ENV", "VOICE_ENVIRONMENT"),
    )
    allow_shared_bootstrap_auth: bool = True
    device_credentials: dict[str, dict[str, SecretStr]] = Field(default_factory=dict)
    worker_heartbeat_ttl_seconds: float = Field(default=15.0, ge=3.0, le=120.0)
    route_lease_ttl_seconds: float = Field(default=30.0, ge=5.0, le=300.0)

    @property
    def bind_host(self) -> str:
        return self.director_bind_host

    @property
    def bind_port(self) -> int:
        return self.director_bind_port

    def validate_runtime(self) -> None:
        for name, secret in (
            ("VOICE_INTERNAL_TOKEN", self.internal_token),
            ("VOICE_GRANT_SIGNING_KEY", self.grant_signing_key),
        ):
            if secret.get_secret_value().startswith("replace-with-"):
                raise ValueError(f"{name} must not use the repository placeholder")
        if len(self.grant_signing_key.get_secret_value().encode("utf-8")) < 16:
            raise ValueError("VOICE_GRANT_SIGNING_KEY must contain at least 16 bytes")
        if self.coordination_backend == "redis" and not self.redis_url:
            raise ValueError("VOICE_REDIS_URL is required for redis coordination")
        if self.allow_shared_bootstrap_auth:
            if self.device_bootstrap_token.get_secret_value().startswith("replace-with-"):
                raise ValueError("VOICE_DEVICE_BOOTSTRAP_TOKEN must not use the repository placeholder")
        if self.environment == "production":
            if self.coordination_backend != "redis":
                raise ValueError("production requires VOICE_COORDINATION_BACKEND=redis")
            if self.allow_shared_bootstrap_auth:
                raise ValueError("production requires VOICE_ALLOW_SHARED_BOOTSTRAP_AUTH=false")
            if not self.device_credentials:
                raise ValueError("production requires VOICE_DEVICE_CREDENTIALS")

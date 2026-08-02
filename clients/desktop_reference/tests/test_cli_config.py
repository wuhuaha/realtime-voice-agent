from __future__ import annotations

import argparse
import math

import pytest

from rva_desktop.cli import build_parser, client_config
from rva_desktop.config import ClientConfig, EndpointCapabilities, MediaProfile


def _config(**updates: object) -> ClientConfig:
    values = {
        "director_url": "https://director.test",
        "bootstrap_token": "bootstrap-secret",
        "device_id": "desktop-1",
        "supported_profiles": (MediaProfile.WSS_OPUS_V1,),
        "preferred_profile": MediaProfile.WSS_OPUS_V1,
    }
    values.update(updates)
    return ClientConfig(**values)  # type: ignore[arg-type]


def test_cli_defaults_to_explicit_wss_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RVA_DIRECTOR_URL", raising=False)
    monkeypatch.setenv("RVA_BOOTSTRAP_TOKEN", "top-secret")
    args = build_parser().parse_args(
        [
            "headless",
            "--director-url",
            "https://director.test",
            "--device-id",
            "desktop-1",
        ]
    )

    config = client_config(args)

    assert config.preferred_profile is MediaProfile.WSS_OPUS_V1
    assert config.supported_profiles == (MediaProfile.WSS_OPUS_V1,)
    assert "top-secret" not in repr(config)


def test_cli_can_explicitly_select_udp_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RVA_BOOTSTRAP_TOKEN", "secret")
    args = build_parser().parse_args(
        [
            "interactive",
            "--director-url",
            "https://director.test",
            "--profile",
            "udp-opus-gcm/1",
        ]
    )

    config = client_config(args)

    assert config.supported_profiles == (MediaProfile.UDP_OPUS_GCM_V1,)


def test_cli_rejects_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RVA_BOOTSTRAP_TOKEN", raising=False)
    args = argparse.Namespace(
        director_url=None,
        token_file=None,
        device_id="desktop-1",
        tenant_id="default",
        profile="wss-opus/1",
    )

    with pytest.raises(ValueError, match="director-url"):
        client_config(args)


def test_cli_reads_secret_from_bounded_token_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RVA_BOOTSTRAP_TOKEN", raising=False)
    token_file = tmp_path / "bootstrap.token"
    token_file.write_text("  file-secret\n", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "headless",
            "--director-url",
            "https://director.test",
            "--token-file",
            str(token_file),
        ]
    )

    config = client_config(args)

    assert "file-secret" not in repr(config)


def test_cli_does_not_offer_plaintext_token_argument() -> None:
    help_text = build_parser().format_help()

    assert "--token TOKEN" not in help_text
    with pytest.raises(SystemExit):
        build_parser().parse_args(["headless", "--token", "must-not-enter-argv"])


def test_cli_does_not_offer_auto_profile() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["headless", "--profile", "auto"])


def test_cli_requires_explicit_opt_in_for_plain_http_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RVA_BOOTSTRAP_TOKEN", "secret")
    args = build_parser().parse_args(
        ["headless", "--director-url", "http://127.0.0.1:8000"]
    )

    with pytest.raises(ValueError, match="plain HTTP Director"):
        client_config(args)

    opted_in = build_parser().parse_args(
        [
            "headless",
            "--director-url",
            "http://127.0.0.1:8000",
            "--allow-insecure-loopback",
        ]
    )
    assert client_config(opted_in).allow_insecure_loopback


def test_cli_insecure_opt_in_never_allows_non_loopback_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RVA_BOOTSTRAP_TOKEN", "secret")
    args = build_parser().parse_args(
        [
            "headless",
            "--director-url",
            "http://director.test:8000",
            "--allow-insecure-loopback",
        ]
    )

    with pytest.raises(ValueError, match="plain HTTP Director"):
        client_config(args)


def test_cli_reads_insecure_loopback_opt_in_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RVA_BOOTSTRAP_TOKEN", "secret")
    monkeypatch.setenv("RVA_ALLOW_INSECURE_LOOPBACK", "true")
    args = build_parser().parse_args(
        ["headless", "--director-url", "http://localhost:8000"]
    )

    assert client_config(args).allow_insecure_loopback


def test_cli_rejects_oversized_token_file_without_echoing_secret(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RVA_BOOTSTRAP_TOKEN", raising=False)
    secret = "s" * 4_097
    token_file = tmp_path / "oversized.token"
    token_file.write_text(secret, encoding="utf-8")
    args = build_parser().parse_args(
        [
            "headless",
            "--director-url",
            "https://director.test",
            "--token-file",
            str(token_file),
        ]
    )

    with pytest.raises(ValueError) as captured:
        client_config(args)

    assert secret not in str(captured.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in (
            "connect_timeout_seconds",
            "control_timeout_seconds",
            "media_max_age_seconds",
            "udp_probe_retry_seconds",
        )
        for value in (math.nan, math.inf, -math.inf)
    ],
)
def test_client_config_rejects_non_finite_durations(field: str, value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        _config(**{field: value})


@pytest.mark.parametrize(
    "updates",
    [
        {"supported_profiles": ("wss-opus/1",)},
        {"preferred_profile": "wss-opus/1"},
        {"capabilities": {"display": True}},
    ],
)
def test_client_config_rejects_non_typed_profiles_and_capabilities(updates: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="type"):
        _config(**updates)


def test_endpoint_capabilities_require_actual_booleans() -> None:
    with pytest.raises(ValueError, match="bool"):
        EndpointCapabilities(display=1)  # type: ignore[arg-type]

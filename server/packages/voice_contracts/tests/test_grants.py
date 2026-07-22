from __future__ import annotations

import pytest
from voice_contracts import BindingAdvertisement, ConnectGrantClaims, GrantCodec, GrantError, encode_route_key
from voice_testkit import MutableClock


def claims(clock: MutableClock, **changes: object) -> ConnectGrantClaims:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "device_id": "device-1",
        "worker_id": "worker-1",
        "session_epoch": "epoch-1",
        "fencing_token": 7,
        "profiles": ("wss-opus-v2", "udp-opus-gcm-v1"),
        "iat": clock(),
        "exp": clock() + 30,
        "jti": "jti-1",
    }
    values.update(changes)
    return ConnectGrantClaims(**values)


def test_grant_is_bound_to_worker_device_and_expiry() -> None:
    clock = MutableClock()
    codec = GrantCodec("test-signing-key-with-32-bytes", clock=clock)
    token = codec.issue(claims(clock))

    verified = codec.verify(token, worker_id="worker-1", device_id="device-1")
    assert verified.fencing_token == 7

    with pytest.raises(GrantError, match="another worker"):
        codec.verify(token, worker_id="worker-2")
    with pytest.raises(GrantError, match="another device"):
        codec.verify(token, device_id="device-2")

    clock.advance(30)
    with pytest.raises(GrantError, match="expired"):
        codec.verify(token)


def test_grant_defaults_to_native_rva_control() -> None:
    value = claims(MutableClock())
    assert value.control_protocol == "rva-control-v1"
    assert value.profiles == ("wss-opus-v2", "udp-opus-gcm-v1")


def test_grant_tampering_fails_before_claims_are_accepted() -> None:
    clock = MutableClock()
    codec = GrantCodec("test-signing-key-with-32-bytes", clock=clock)
    token = codec.issue(claims(clock))
    header, payload, signature = token.split(".")
    replacement = "A" if payload[-1] != "A" else "B"

    with pytest.raises(GrantError, match="signature"):
        codec.verify(f"{header}.{payload[:-1]}{replacement}.{signature}")


def test_route_key_encoding_is_unambiguous_for_colon_identifiers() -> None:
    assert encode_route_key("a:b", "c") != encode_route_key("a", "b:c")
    assert encode_route_key("tenant", "aa:bb:cc:dd:ee:ff") == "tenant:aa%3Abb%3Acc%3Add%3Aee%3Aff"


@pytest.mark.parametrize(
    ("control_protocol", "path", "profiles"),
    [
        ("xiaozhi-control-v1", "/v1/xiaozhi", ("wss-opus-v1", "udp-opus-gcm-v1")),
        ("rva-control-v1", "/v1/voice", ("wss-opus-v2", "udp-opus-gcm-v1")),
    ],
)
def test_binding_accepts_routable_control_profile_combinations(
    control_protocol: str,
    path: str,
    profiles: tuple[str, ...],
) -> None:
    binding = BindingAdvertisement(
        control_protocol=control_protocol,
        public_wss_url=f"wss://worker.test{path}",
        profiles=profiles,
    )
    assert binding.profiles == profiles


@pytest.mark.parametrize(
    ("control_protocol", "path", "profiles"),
    [
        ("xiaozhi-control-v1", "/v1/xiaozhi", ("wss-opus-v2",)),
        ("rva-control-v1", "/v1/voice", ("wss-opus-v1",)),
    ],
)
def test_binding_rejects_unroutable_control_profile_combinations(
    control_protocol: str,
    path: str,
    profiles: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="cannot route transport profiles"):
        BindingAdvertisement(
            control_protocol=control_protocol,
            public_wss_url=f"wss://worker.test{path}",
            profiles=profiles,
        )


def test_connect_grant_rejects_unroutable_control_profile_combination() -> None:
    clock = MutableClock()
    with pytest.raises(ValueError, match="rva-control-v1 cannot route.*wss-opus-v1"):
        claims(clock, control_protocol="rva-control-v1", profiles=("wss-opus-v1",))

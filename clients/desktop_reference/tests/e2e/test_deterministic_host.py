from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from voice_testkit.subprocess_cluster import running_process_cluster

from rva_desktop.app import DesktopApp
from rva_desktop.audio.fixture import FixturePcmSource, RecordingAudioSink
from rva_desktop.audio.opus import PyAvOpusCodec
from rva_desktop.audio.ports import WIRE_BYTES_PER_FRAME, WIRE_SAMPLES_PER_FRAME, PcmFrame
from rva_desktop.config import ClientConfig, MediaProfile
from rva_desktop.session.client import DesktopSession
from rva_desktop.transport.wss import WssTransport

PRODUCT_ROOT = Path(__file__).resolve().parents[4]
BOOTSTRAP_TOKEN = "validator-desktop-e2e-bootstrap-token"
INTERNAL_TOKEN = "validator-desktop-e2e-internal-token"
GRANT_SIGNING_KEY = "validator-desktop-e2e-grant-signing-key"
LAB_TOKEN = "validator-desktop-e2e-lab-token"


def _server_python() -> Path:
    name = "python.exe" if sys.platform == "win32" else "python"
    path = PRODUCT_ROOT / "server" / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / name
    if not path.is_file():
        raise AssertionError(f"server environment is missing: {path}")
    return path


class _RecordingWssTransport(WssTransport):
    def __init__(self) -> None:
        super().__init__()
        self.sent_control: list[dict[str, Any]] = []

    async def send_control(self, wire: str) -> None:
        self.sent_control.append(json.loads(wire))
        await super().send_control(wire)


@dataclass(frozen=True, slots=True)
class _DesktopAppExerciseEvidence:
    outcome: str
    detail: str


async def _exercise_profile(director_url: str, profile: MediaProfile) -> None:
    transport = _RecordingWssTransport()
    session = DesktopSession(
        ClientConfig(
            director_url=director_url,
            bootstrap_token=BOOTSTRAP_TOKEN,
            device_id=f"desktop-e2e-{profile.value.replace('/', '-')}",
            tenant_id="desktop-e2e",
            supported_profiles=(profile,),
            preferred_profile=profile,
            connect_timeout_seconds=10,
            control_timeout_seconds=10,
            allow_insecure_loopback=True,
        ),
        wss_factory=lambda: transport,
    )
    codec = PyAvOpusCodec()
    source = FixturePcmSource(b"\x00" * WIRE_BYTES_PER_FRAME * 3, paced=True)
    sink = RecordingAudioSink(max_frames=4)
    events = []
    media = []
    response_end: dict[str, Any] | None = None
    playback_started = False
    try:
        # Exclude one-time codec initialization from the server's 120 ms live-media
        # budget, then preserve the canonical 60 ms capture cadence.
        codec.encode_60ms(b"\x00" * WIRE_BYTES_PER_FRAME)
        await source.start()
        await sink.start()
        opened = await session.connect()
        assert opened.kind == "session.opened"
        assert session.selected_profile is profile

        for _ in range(3):
            frame = await source.read_frame()
            assert frame is not None
            await session.send_opus(codec.encode_60ms(frame.data))

        while response_end is None or len(media) < 4:
            event = await asyncio.wait_for(session.next_event(), timeout=10)
            events.append(event)
            if event.kind == "response.end":
                response_end = event.message
            if event.kind != "media.audio":
                continue
            assert event.media is not None
            assert event.target is not None
            media.append(event.media)
            decoded = codec.decode_60ms(event.media.payload)
            await sink.write_frame(
                PcmFrame(
                    data=decoded,
                    sequence=event.media.sequence,
                    timestamp_samples=event.media.timestamp,
                    captured_at=time.monotonic(),
                )
            )
            ack = await sink.wait_rendered(event.media.sequence)
            if not playback_started:
                await session.playback_started(event.target, event.media.sequence)
                playback_started = True
            assert ack.rendered_samples == WIRE_SAMPLES_PER_FRAME

        await sink.drain()
        target = events[-1].target or next(event.target for event in events if event.target is not None)
        assert target is not None
        await session.playback_ended(
            target,
            outcome="completed",
            played_samples=len(sink.frames) * WIRE_SAMPLES_PER_FRAME,
            last_media_sequence=media[-1].sequence,
        )

        messages = [event.message for event in events if event.kind != "media.audio"]
        by_kind = {message["type"]: message for message in messages}
        assert by_kind["transcript.delta"]["text"] == "deterministic"
        assert by_kind["transcript.delta"]["sequence"] == 0
        assert by_kind["transcript.final"]["text"] == "deterministic turn"
        assert by_kind["transcript.final"]["sequence"] == 1
        assert by_kind["response.text"]["text"] == "deterministic turn"
        assert by_kind["response.text"]["sequence"] == 0
        assert response_end is not None
        assert response_end["outcome"] == "completed"

        expected_first_sequence = 1 if profile is MediaProfile.UDP_OPUS_GCM_V1 else 0
        assert [frame.sequence for frame in media] == list(range(expected_first_sequence, expected_first_sequence + 4))
        assert [frame.timestamp for frame in media] == [0, 960, 1_920, 2_880]
        assert {frame.generation for frame in media} == {by_kind["response.begin"]["generation"]}
        assert {frame.media_id for frame in media} == {session.state.opened.media_id}
        assert {frame.media_epoch for frame in media} == {session.state.opened.media_epoch}
        assert response_end["final_media_sequence"] == media[-1].sequence
        assert len(sink.frames) == 4
        assert len(sink.pcm) // 2 == 3_840

        playback_facts = [
            message for message in transport.sent_control if message["type"] in {"playback.started", "playback.ended"}
        ]
        assert [message["type"] for message in playback_facts] == ["playback.started", "playback.ended"]
        assert playback_facts[0]["first_media_sequence"] == media[0].sequence
        assert playback_facts[1]["outcome"] == "completed"
        assert playback_facts[1]["played_samples"] == 3_840
        assert playback_facts[1]["last_media_sequence"] == media[-1].sequence
    finally:
        await session.close(reason="normal", detail="host_e2e_complete")
        await source.close()
        await sink.close()
        codec.close()


async def _exercise_desktop_app_profile(director_url: str, profile: MediaProfile) -> None:
    evidence = await _run_desktop_app_profile(director_url, profile)
    assert evidence.outcome == "completed", evidence.detail


async def _exercise_netem_desktop_app_profile(
    director_url: str,
    profile: MediaProfile,
) -> _DesktopAppExerciseEvidence:
    """Run the real composition root while preserving its bounded recovery result."""
    return await _run_desktop_app_profile(director_url, profile)


async def _run_desktop_app_profile(
    director_url: str,
    profile: MediaProfile,
) -> _DesktopAppExerciseEvidence:
    transport = _RecordingWssTransport()
    session = DesktopSession(
        ClientConfig(
            director_url=director_url,
            bootstrap_token=BOOTSTRAP_TOKEN,
            device_id=f"desktop-app-e2e-{profile.value.replace('/', '-')}",
            tenant_id="desktop-e2e",
            supported_profiles=(profile,),
            preferred_profile=profile,
            connect_timeout_seconds=10,
            control_timeout_seconds=10,
            allow_insecure_loopback=True,
        ),
        wss_factory=lambda: transport,
    )
    source = FixturePcmSource(b"\x00" * WIRE_BYTES_PER_FRAME * 3, paced=True)
    sink = RecordingAudioSink(max_frames=4)
    codec = PyAvOpusCodec()
    events = []

    # DesktopApp owns the codec lifecycle, so warm the stateful encoder before
    # handing ownership over to the real composition root.
    codec.encode_60ms(b"\x00" * WIRE_BYTES_PER_FRAME)
    app = DesktopApp(
        session,
        source=source,
        sink=sink,
        codec=codec,
        on_event=events.append,
    )
    result = await asyncio.wait_for(app.run(stop_after_playbacks=1), timeout=20)

    frame_sequences = [frame.sequence for frame in sink.frames]
    frame_timestamps = [frame.timestamp_samples for frame in sink.frames]
    playback_facts = [
        message for message in transport.sent_control if message["type"] in {"playback.started", "playback.ended"}
    ]
    diagnostic = (
        f"result={result!r}, frame_sequences={frame_sequences!r}, "
        f"frame_timestamps={frame_timestamps!r}, playback_facts={playback_facts!r}, "
        f"events={[event.kind for event in events]!r}"
    )

    assert result.uplink_frames >= 1, diagnostic
    assert result.completed_playbacks == 1, diagnostic

    messages = [event.message for event in events if event.kind != "media.audio"]
    by_kind = {message["type"]: message for message in messages}
    assert by_kind["transcript.final"]["text"] == "deterministic turn"
    assert by_kind["response.text"]["text"] == "deterministic turn"
    assert by_kind["response.end"]["outcome"] == "completed"
    assert transport.sent_control[-1]["type"] == "session.close"

    # A successful return includes deterministic ownership cleanup, not merely
    # task cancellation while audio or transport resources remain reusable.
    assert session.selected_profile is None
    with pytest.raises(RuntimeError, match="audio source is closed"):
        await source.start()
    with pytest.raises(RuntimeError, match="audio sink is closed"):
        await sink.start()
    with pytest.raises(RuntimeError, match="Opus codec is closed"):
        codec.encode_60ms(b"\x00" * WIRE_BYTES_PER_FRAME)

    expected_first_sequence = 1 if profile is MediaProfile.UDP_OPUS_GCM_V1 else 0
    completed = (
        result.playback_frames == 4
        and len(sink.frames) == 4
        and len(sink.pcm) == WIRE_BYTES_PER_FRAME * 4
        and frame_sequences == list(range(expected_first_sequence, expected_first_sequence + 4))
        and frame_timestamps == [0, 960, 1_920, 2_880]
        and by_kind["response.end"]["final_media_sequence"] == expected_first_sequence + 3
        and [message["type"] for message in playback_facts] == ["playback.started", "playback.ended"]
        and playback_facts[0]["first_media_sequence"] == expected_first_sequence
        and playback_facts[1]["outcome"] == "completed"
        and playback_facts[1]["played_samples"] == 4 * WIRE_SAMPLES_PER_FRAME
        and playback_facts[1]["last_media_sequence"] == expected_first_sequence + 3
    )
    if completed:
        return _DesktopAppExerciseEvidence("completed", diagnostic)

    opened_indexes = [index for index, event in enumerate(events) if event.kind == "session.opened"]
    stopped = [
        message
        for message in playback_facts
        if message["type"] == "playback.ended" and message["outcome"] == "stopped"
    ]
    identity_fields = ("session_epoch", "fencing_token", "media_epoch")
    fresh_identity = len(opened_indexes) == 2 and any(
        events[opened_indexes[0]].message.get(field) != events[opened_indexes[1]].message.get(field)
        for field in identity_fields
    )
    no_old_media_after_reopen = len(opened_indexes) == 2 and not any(
        event.kind == "media.audio" for event in events[opened_indexes[1] + 1 :]
    )
    bounded_recovery = (
        profile is MediaProfile.UDP_OPUS_GCM_V1
        and len(stopped) == 1
        and fresh_identity
        and no_old_media_after_reopen
        and playback_facts[-1] == stopped[0]
        and 0 <= result.playback_frames < 4
    )
    if bounded_recovery:
        return _DesktopAppExerciseEvidence("bounded_recovery_verified", diagnostic)
    raise AssertionError(diagnostic)


@pytest.mark.e2e_host
@pytest.mark.parametrize(
    ("profile", "udp_enabled"),
    [
        pytest.param(MediaProfile.WSS_OPUS_V1, False, id="wss-opus/1"),
        pytest.param(MediaProfile.UDP_OPUS_GCM_V1, True, id="udp-opus-gcm/1"),
    ],
)
def test_deterministic_host_round_trip(
    tmp_path: Path,
    profile: MediaProfile,
    udp_enabled: bool,
) -> None:
    with running_process_cluster(
        tmp_path,
        worker_count=1,
        udp_enabled=udp_enabled,
        python_executable=_server_python(),
        internal_token=INTERNAL_TOKEN,
        bootstrap_token=BOOTSTRAP_TOKEN,
        grant_signing_key=GRANT_SIGNING_KEY,
        lab_token=LAB_TOKEN,
    ) as cluster:
        asyncio.run(_exercise_profile(cluster.director_url, profile))


@pytest.mark.e2e_host
@pytest.mark.parametrize(
    ("profile", "udp_enabled"),
    [
        pytest.param(MediaProfile.WSS_OPUS_V1, False, id="wss-opus/1"),
        pytest.param(MediaProfile.UDP_OPUS_GCM_V1, True, id="udp-opus-gcm/1"),
    ],
)
def test_desktop_app_deterministic_host_round_trip(
    tmp_path: Path,
    profile: MediaProfile,
    udp_enabled: bool,
) -> None:
    with running_process_cluster(
        tmp_path,
        worker_count=1,
        udp_enabled=udp_enabled,
        python_executable=_server_python(),
        internal_token=INTERNAL_TOKEN,
        bootstrap_token=BOOTSTRAP_TOKEN,
        grant_signing_key=GRANT_SIGNING_KEY,
        lab_token=LAB_TOKEN,
    ) as cluster:
        asyncio.run(_exercise_desktop_app_profile(cluster.director_url, profile))

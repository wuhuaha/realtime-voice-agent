from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

from rva_desktop.app import DesktopApp
from rva_desktop.audio import (
    WIRE_BYTES_PER_FRAME,
    FixturePcmSource,
    PcmFrame,
    RenderAck,
)
from rva_desktop.config import MediaProfile
from rva_desktop.errors import FreshReopenRequired, TransportError
from rva_desktop.events import SessionEvent
from rva_desktop.protocol import FLAG_AUDIO, MediaFrame, PlaybackTarget


class FakeCodec:
    def __init__(self) -> None:
        self.closed = False
        self.concealed = 0

    def encode_60ms(self, pcm16le: bytes) -> bytes:
        assert len(pcm16le) == WIRE_BYTES_PER_FRAME
        return b"encoded"

    def decode_60ms(self, payload: bytes) -> bytes:
        assert payload == b"downlink"
        return b"\x01\x00" * 960

    def conceal_60ms(self) -> bytes:
        self.concealed += 1
        return b"\x00\x00" * 960

    def close(self) -> None:
        self.closed = True


class FailingCloseSource(FixturePcmSource):
    async def close(self) -> None:
        await super().close()
        raise RuntimeError("source close failed")


class FakeSink:
    def __init__(self) -> None:
        self.frames: list[PcmFrame] = []
        self.started = False
        self.closed = False
        self.aborted = False
        self.drains = 0
        self.rendered = asyncio.Event()
        self.auto_render = True

    @property
    def format(self):  # type annotation is deliberately inferred from the audio port
        from rva_desktop.audio import WIRE_FORMAT

        return WIRE_FORMAT

    async def start(self) -> None:
        self.started = True

    async def write_frame(self, frame: PcmFrame) -> None:
        self.frames.append(frame)

    async def wait_rendered(self, sequence: int) -> RenderAck:
        if not self.auto_render:
            await self.rendered.wait()
        frame = next(item for item in self.frames if item.sequence == sequence)
        return RenderAck(frame.sequence, frame.timestamp_samples)

    async def drain(self) -> None:
        self.drains += 1

    async def close(self) -> None:
        self.closed = True

    async def abort(self) -> None:
        self.aborted = True
        self.closed = True


class FakeSession:
    def __init__(
        self,
        events: list[SessionEvent],
        *,
        profile: MediaProfile = MediaProfile.WSS_OPUS_V1,
        keepalive_signal: asyncio.Event | None = None,
    ) -> None:
        self.events = asyncio.Queue[SessionEvent]()
        for event in events:
            self.events.put_nowait(event)
        self.sent_opus: list[bytes] = []
        self.facts: list[tuple[object, ...]] = []
        self.closed = False
        self.close_calls: list[tuple[str, str | None]] = []
        self._profile = profile
        self.keepalives = 0
        self.reopens = 0
        self.keepalive_signal = keepalive_signal
        self.state = SimpleNamespace(opened=SimpleNamespace(heartbeat_interval_ms=100))

    @property
    def selected_profile(self) -> MediaProfile:
        return self._profile

    async def connect(self) -> SessionEvent:
        return SessionEvent("session.opened", {"heartbeat_interval_ms": 15_000})

    async def send_opus(self, payload: bytes, *, samples: int) -> None:
        assert samples == 960
        self.sent_opus.append(payload)

    async def next_event(self) -> SessionEvent:
        return await self.events.get()

    async def send_keepalive(self) -> None:
        self.keepalives += 1
        if self.keepalive_signal is not None:
            self.keepalive_signal.set()

    async def reopen(self) -> SessionEvent:
        self.reopens += 1
        return SessionEvent("session.opened", {"heartbeat_interval_ms": 15_000})

    async def playback_started(self, target: PlaybackTarget, sequence: int) -> None:
        self.facts.append(("started", target, sequence))

    async def playback_ended(
        self,
        target: PlaybackTarget,
        *,
        outcome: str,
        played_samples: int,
        last_media_sequence: int | None,
    ) -> None:
        self.facts.append(("ended", target, outcome, played_samples, last_media_sequence))

    async def close(self, *, reason: str = "normal", detail: str | None = None) -> None:
        self.close_calls.append((reason, detail))
        self.closed = True


class FailingAfterEventsSession(FakeSession):
    async def next_event(self) -> SessionEvent:
        if self.events.empty():
            raise TransportError("socket_lost", retryable=True)
        return await super().next_event()


def _response_events(target: PlaybackTarget) -> list[SessionEvent]:
    media = MediaFrame(
        FLAG_AUDIO,
        bytes.fromhex("0123456789abcdef"),
        1,
        7,
        960,
        target.generation,
        b"downlink",
    )
    return [
        SessionEvent("response.begin", target=target),
        SessionEvent("media.audio", target=target, media=media),
        SessionEvent(
            "response.end",
            {
                "response_id": target.response_id,
                "generation": target.generation,
                "outcome": "completed",
                "final_media_sequence": 7,
            },
            target=target,
        ),
    ]


def test_headless_composition_waits_for_physical_playback_fact() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("response-1", 1)
        session = FakeSession(_response_events(target))
        source = FixturePcmSource(b"\x00" * WIRE_BYTES_PER_FRAME)
        sink = FakeSink()
        codec = FakeCodec()
        observed: list[str] = []
        app = DesktopApp(
            session,  # type: ignore[arg-type]
            source=source,
            sink=sink,
            codec=codec,
            on_event=lambda event: observed.append(event.kind),
        )

        result = await app.run(stop_after_playbacks=1)

        assert result.uplink_frames == 1
        assert result.playback_frames == 1
        assert result.completed_playbacks == 1
        assert result.source_exhausted
        assert session.sent_opus == [b"encoded"]
        assert session.facts == [
            ("started", target, 7),
            ("ended", target, "completed", 960, 7),
        ]
        assert observed == ["session.opened", "response.begin", "media.audio", "response.end"]
        assert len(sink.frames) == 1
        assert sink.drains >= 1
        assert session.closed and sink.closed and codec.closed

    asyncio.run(scenario())


def test_playback_facts_wait_for_render_ack() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("response-ack", 1)
        session = FakeSession(_response_events(target))
        sink = FakeSink()
        sink.auto_render = False
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=sink,
            codec=FakeCodec(),
        )

        running = asyncio.create_task(app.run(stop_after_playbacks=1))
        while not sink.frames:
            await asyncio.sleep(0)
        assert session.facts == []

        sink.rendered.set()
        await asyncio.wait_for(running, timeout=1)
        assert [fact[0] for fact in session.facts] == ["started", "ended"]

    asyncio.run(scenario())


def test_playback_stop_rebuilds_sink_and_fences_old_generation() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("response-1", 1)
        stale = MediaFrame(
            FLAG_AUDIO,
            bytes.fromhex("0123456789abcdef"),
            1,
            8,
            1_920,
            1,
            b"downlink",
        )
        first = MediaFrame(
            FLAG_AUDIO,
            bytes.fromhex("0123456789abcdef"),
            1,
            7,
            960,
            1,
            b"downlink",
        )
        events = [
            SessionEvent("response.begin", target=target),
            SessionEvent("media.audio", target=target, media=first),
            SessionEvent(
                "playback.stop",
                {"fence_generation": 2, "cause": "recognized_interrupt"},
                target=target,
            ),
            SessionEvent("media.audio", target=target, media=stale),
            SessionEvent("session.close", {"reason": "normal"}),
        ]
        session = FakeSession(events)
        source = FixturePcmSource(b"")
        initial = FakeSink()
        replacements: deque[FakeSink] = deque([FakeSink()])
        app = DesktopApp(
            session,  # type: ignore[arg-type]
            source=source,
            sink=initial,
            sink_factory=replacements.popleft,
            codec=FakeCodec(),
        )

        result = await app.run(stop_after_playbacks=2)

        assert result.playback_frames == 1
        assert result.completed_playbacks == 1
        assert initial.aborted
        assert session.facts == [
            ("started", target, 7),
            ("ended", target, "stopped", 960, 7),
        ]

    asyncio.run(scenario())


def test_external_stop_cancels_blocked_audio_tasks_and_closes_resources() -> None:
    async def scenario() -> None:
        session = FakeSession([])
        source = FixturePcmSource(b"\x00" * WIRE_BYTES_PER_FRAME, paced=True)
        sink = FakeSink()
        codec = FakeCodec()
        stop = asyncio.Event()
        stop.set()
        app = DesktopApp(session, source=source, sink=sink, codec=codec)  # type: ignore[arg-type]

        await asyncio.wait_for(app.run(stop_event=stop), timeout=1)

        assert session.closed and sink.closed and codec.closed

    asyncio.run(scenario())


def test_udp_silence_sends_keepalive_without_stopping_receive_loop() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        session = FakeSession(
            [],
            profile=MediaProfile.UDP_OPUS_GCM_V1,
            keepalive_signal=stop,
        )
        source = FixturePcmSource(b"")
        sink = FakeSink()
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=source,
            sink=sink,
            codec=FakeCodec(),
        )

        await asyncio.wait_for(
            app.run(stop_event=stop, stop_after_playbacks=1),
            timeout=1,
        )

        assert session.keepalives == 1

    asyncio.run(scenario())


def test_udp_keepalive_refresh_fresh_reopens_idle_session() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()

        class RefreshingSession(FakeSession):
            async def send_keepalive(self) -> None:
                if self.reopens == 0:
                    raise FreshReopenRequired()
                await super().send_keepalive()

            async def reopen(self) -> SessionEvent:
                opened = await super().reopen()
                stop.set()
                return opened

        session = RefreshingSession([], profile=MediaProfile.UDP_OPUS_GCM_V1)
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=FakeSink(),
            codec=FakeCodec(),
        )

        await asyncio.wait_for(app.run(stop_event=stop, stop_after_playbacks=1), timeout=1)

        assert session.reopens == 1

    asyncio.run(scenario())


def test_cleanup_failure_does_not_skip_other_resource_owners() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        stop.set()
        session = FakeSession([])
        sink = FakeSink()
        codec = FakeCodec()
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FailingCloseSource(b""),
            sink=sink,
            codec=codec,
        )

        try:
            await app.run(stop_event=stop, stop_after_playbacks=1)
        except ExceptionGroup as exc:
            assert any("source close failed" in str(item) for item in exc.exceptions)
        else:
            raise AssertionError("cleanup error was not reported")

        assert sink.closed and session.closed and codec.closed

    asyncio.run(scenario())


def test_late_stop_for_old_target_does_not_reset_current_sink() -> None:
    async def scenario() -> None:
        first = PlaybackTarget("response-1", 1)
        second = PlaybackTarget("response-2", 2)

        def media(target: PlaybackTarget, sequence: int) -> SessionEvent:
            frame = MediaFrame(
                FLAG_AUDIO,
                bytes.fromhex("0123456789abcdef"),
                1,
                sequence,
                sequence * 960,
                target.generation,
                b"downlink",
            )
            return SessionEvent("media.audio", target=target, media=frame)

        def ended(target: PlaybackTarget, sequence: int) -> SessionEvent:
            return SessionEvent(
                "response.end",
                {
                    "outcome": "completed",
                    "final_media_sequence": sequence,
                },
                target=target,
            )

        events = [
            SessionEvent("response.begin", target=first),
            media(first, 0),
            ended(first, 0),
            SessionEvent("response.begin", target=second),
            media(second, 1),
            SessionEvent(
                "playback.stop",
                {"fence_generation": 2, "cause": "recognized_interrupt"},
                target=first,
            ),
            media(second, 2),
            ended(second, 2),
        ]
        resets = 0

        def replacement() -> FakeSink:
            nonlocal resets
            resets += 1
            return FakeSink()

        session = FakeSession(events)
        sink = FakeSink()
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=sink,
            sink_factory=replacement,
            codec=FakeCodec(),
        )

        result = await app.run(stop_after_playbacks=2)

        assert result.playback_frames == 3
        assert resets == 0
        assert not sink.aborted
        assert session.facts[-1] == ("ended", second, "completed", 1_920, 2)

    asyncio.run(scenario())


def test_udp_audio_timestamp_gap_uses_bounded_plc_before_current_packet() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("response-gap", 1)

        def media(sequence: int, timestamp: int) -> SessionEvent:
            return SessionEvent(
                "media.audio",
                target=target,
                media=MediaFrame(
                    FLAG_AUDIO,
                    bytes.fromhex("0123456789abcdef"),
                    1,
                    sequence,
                    timestamp,
                    target.generation,
                    b"downlink",
                ),
            )

        events = [
            SessionEvent("response.begin", target=target),
            media(1, 0),
            media(4, 2_880),
            SessionEvent(
                "response.end",
                {"outcome": "completed", "final_media_sequence": 4},
                target=target,
            ),
        ]
        session = FakeSession(events, profile=MediaProfile.UDP_OPUS_GCM_V1)
        sink = FakeSink()
        codec = FakeCodec()
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=sink,
            codec=codec,
        )

        result = await app.run(stop_after_playbacks=1)

        assert codec.concealed == 2
        assert [frame.sequence for frame in sink.frames] == [1, 2, 3, 4]
        assert result.playback_frames == 4
        assert session.facts[-1] == ("ended", target, "completed", 3_840, 4)

    asyncio.run(scenario())


def test_udp_sequence_gap_from_keepalive_does_not_create_false_plc() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("response-keepalive-gap", 1)
        frames = [
            MediaFrame(
                FLAG_AUDIO,
                bytes.fromhex("0123456789abcdef"),
                1,
                sequence,
                timestamp,
                1,
                b"downlink",
            )
            for sequence, timestamp in ((1, 0), (3, 960))
        ]
        events = [SessionEvent("response.begin", target=target)]
        events.extend(SessionEvent("media.audio", target=target, media=frame) for frame in frames)
        events.append(
            SessionEvent(
                "response.end",
                {"outcome": "completed", "final_media_sequence": 3},
                target=target,
            )
        )
        session = FakeSession(events, profile=MediaProfile.UDP_OPUS_GCM_V1)
        codec = FakeCodec()
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=FakeSink(),
            codec=codec,
        )

        result = await app.run(stop_after_playbacks=1)

        assert codec.concealed == 0
        assert result.playback_frames == 2
        assert session.reopens == 0

    asyncio.run(scenario())


def test_udp_large_audio_gap_stops_target_and_fresh_reopens() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("response-large-gap", 1)
        frames = [
            MediaFrame(
                FLAG_AUDIO,
                bytes.fromhex("0123456789abcdef"),
                1,
                sequence,
                sequence * 960,
                1,
                b"downlink",
            )
            for sequence in (1, 7)
        ]
        events = [SessionEvent("response.begin", target=target)]
        events.extend(SessionEvent("media.audio", target=target, media=frame) for frame in frames)
        events.append(
            SessionEvent(
                "response.end",
                {"outcome": "completed", "final_media_sequence": 7},
                target=target,
            )
        )
        session = FakeSession(events, profile=MediaProfile.UDP_OPUS_GCM_V1)
        codec = FakeCodec()
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=FakeSink(),
            codec=codec,
        )

        result = await app.run(stop_after_playbacks=1)

        assert codec.concealed == 0
        assert result.playback_frames == 1
        assert result.completed_playbacks == 1
        assert session.reopens == 1
        assert session.facts[-1] == ("ended", target, "stopped", 960, 1)

    asyncio.run(scenario())


def test_missing_final_udp_packet_expires_then_stops_and_reopens() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("response-tail-loss", 1)
        first = MediaFrame(
            FLAG_AUDIO,
            bytes.fromhex("0123456789abcdef"),
            1,
            1,
            0,
            1,
            b"downlink",
        )
        events = [
            SessionEvent("response.begin", target=target),
            SessionEvent("media.audio", target=target, media=first),
            SessionEvent(
                "response.end",
                {"outcome": "completed", "final_media_sequence": 2},
                target=target,
            ),
        ]
        session = FakeSession(events, profile=MediaProfile.UDP_OPUS_GCM_V1)
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=FakeSink(),
            codec=FakeCodec(),
        )

        await asyncio.wait_for(app.run(stop_after_playbacks=1), timeout=1)

        assert session.reopens == 1
        assert session.facts[-1] == ("ended", target, "stopped", 960, 1)

    asyncio.run(scenario())


def test_missing_response_end_expires_then_stops_and_reopens() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("response-no-terminal", 1)
        first = MediaFrame(
            FLAG_AUDIO,
            bytes.fromhex("0123456789abcdef"),
            1,
            1,
            0,
            1,
            b"downlink",
        )
        session = FakeSession(
            [
                SessionEvent("response.begin", target=target),
                SessionEvent("media.audio", target=target, media=first),
            ],
            profile=MediaProfile.UDP_OPUS_GCM_V1,
        )
        session.config = SimpleNamespace(
            media_max_age_seconds=0.01,
            control_timeout_seconds=0.05,
        )
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=FakeSink(),
            codec=FakeCodec(),
        )

        await asyncio.wait_for(app.run(stop_after_playbacks=1), timeout=2)

        assert session.reopens == 1
        assert session.facts[-1] == ("ended", target, "stopped", 960, 1)

    asyncio.run(scenario())


def test_recovery_completion_waits_until_fresh_reopen_finishes() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("response-refresh-order", 1)
        first = MediaFrame(
            FLAG_AUDIO,
            bytes.fromhex("0123456789abcdef"),
            1,
            1,
            0,
            1,
            b"downlink",
        )
        second = MediaFrame(
            FLAG_AUDIO,
            bytes.fromhex("0123456789abcdef"),
            1,
            8,
            6_720,
            1,
            b"downlink",
        )
        reopen_started = asyncio.Event()
        reopen_release = asyncio.Event()

        class SlowReopenSession(FakeSession):
            async def reopen(self) -> SessionEvent:
                self.reopens += 1
                reopen_started.set()
                await reopen_release.wait()
                return SessionEvent("session.opened", {"heartbeat_interval_ms": 15_000})

        session = SlowReopenSession(
            [
                SessionEvent("response.begin", target=target),
                SessionEvent("media.audio", target=target, media=first),
                SessionEvent("media.audio", target=target, media=second),
            ],
            profile=MediaProfile.UDP_OPUS_GCM_V1,
        )
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=FakeSink(),
            codec=FakeCodec(),
        )

        running = asyncio.create_task(app.run(stop_after_playbacks=1))
        await asyncio.wait_for(reopen_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not running.done()

        reopen_release.set()
        await asyncio.wait_for(running, timeout=1)
        assert session.reopens == 1

    asyncio.run(scenario())


def test_media_for_same_generation_but_different_response_is_not_rendered() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("expected", 1)
        wrong_target = PlaybackTarget("wrong", 1)
        wrong_media = MediaFrame(
            FLAG_AUDIO,
            bytes.fromhex("0123456789abcdef"),
            1,
            1,
            0,
            1,
            b"downlink",
        )
        events = [
            SessionEvent("response.begin", target=target),
            SessionEvent("media.audio", target=wrong_target, media=wrong_media),
            SessionEvent(
                "response.end",
                {"outcome": "cancelled"},
                target=target,
            ),
        ]
        session = FakeSession(events)
        sink = FakeSink()
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=sink,
            codec=FakeCodec(),
        )

        result = await app.run(stop_after_playbacks=1)

        assert result.playback_frames == 0
        assert sink.frames == []
        assert session.facts == [("ended", target, "stopped", 0, None)]

    asyncio.run(scenario())


def test_core_transport_failure_wins_over_simultaneous_completion() -> None:
    async def scenario() -> None:
        target = PlaybackTarget("response-race", 1)
        session = FailingAfterEventsSession(_response_events(target))
        app = DesktopApp(  # type: ignore[arg-type]
            session,
            source=FixturePcmSource(b""),
            sink=FakeSink(),
            codec=FakeCodec(),
        )

        try:
            await app.run(stop_after_playbacks=1)
        except TransportError as exc:
            assert exc.code == "socket_lost"
        else:
            raise AssertionError("core transport failure was swallowed by completion")

    asyncio.run(scenario())

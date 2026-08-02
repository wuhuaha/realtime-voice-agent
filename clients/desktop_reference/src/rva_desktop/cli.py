from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from .app import DesktopApp
from .audio import (
    WIRE_BYTES_PER_FRAME,
    FixturePcmSource,
    NullAudioSink,
    OpusUnavailableError,
    PyAvOpusCodec,
    RecordingAudioSink,
    SoundDeviceAudioSink,
    SoundDeviceAudioSource,
    SoundDeviceInputConfig,
    SoundDeviceOutputConfig,
    SoundDeviceUnavailableError,
)
from .config import ClientConfig, MediaProfile
from .errors import RvaClientError
from .events import SessionEvent
from .session import DesktopSession
from .trace import LoggingTrace

_PROFILE_CHOICES = (MediaProfile.WSS_OPUS_V1.value, MediaProfile.UDP_OPUS_GCM_V1.value)
_MAX_TOKEN_FILE_BYTES = 4_096
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off", ""})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rva-desktop", allow_abbrev=False)
    parser.add_argument("mode", choices=("headless", "interactive"))
    parser.add_argument("--director-url", default=os.getenv("RVA_DIRECTOR_URL"))
    parser.add_argument(
        "--allow-insecure-loopback",
        action="store_true",
        default=_environment_flag("RVA_ALLOW_INSECURE_LOOPBACK"),
        help="allow plain HTTP/WS only when the Director host is loopback",
    )
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--device-id", default=os.getenv("RVA_DEVICE_ID", "desktop-reference"))
    parser.add_argument("--tenant-id", default=os.getenv("RVA_TENANT_ID", "default"))
    parser.add_argument(
        "--profile",
        choices=_PROFILE_CHOICES,
        default=os.getenv("RVA_MEDIA_PROFILE", MediaProfile.WSS_OPUS_V1.value),
    )
    parser.add_argument("--input-pcm", type=Path)
    parser.add_argument("--output-pcm", type=Path)
    parser.add_argument("--silence-frames", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--input-device")
    parser.add_argument("--output-device")
    parser.add_argument("--verbose", action="store_true")
    return parser


def client_config(args: argparse.Namespace) -> ClientConfig:
    if not args.director_url:
        raise ValueError("--director-url or RVA_DIRECTOR_URL is required")
    token_file = getattr(args, "token_file", None)
    token = _read_bootstrap_token(token_file) if token_file is not None else os.getenv("RVA_BOOTSTRAP_TOKEN")
    if not token:
        raise ValueError("RVA_BOOTSTRAP_TOKEN or --token-file is required")
    preferred = MediaProfile(args.profile)
    supported = (preferred,)
    return ClientConfig(
        director_url=args.director_url,
        bootstrap_token=token,
        device_id=args.device_id,
        tenant_id=args.tenant_id,
        supported_profiles=supported,
        preferred_profile=preferred,
        allow_insecure_loopback=bool(getattr(args, "allow_insecure_loopback", False)),
    )


async def run_cli(args: argparse.Namespace) -> int:
    config = client_config(args)
    codec = PyAvOpusCodec()
    session = DesktopSession(config, trace=LoggingTrace())
    if args.mode == "interactive":
        source = SoundDeviceAudioSource(
            SoundDeviceInputConfig(device=_device(args.input_device))
        )
        sink_factory = lambda: SoundDeviceAudioSink(  # noqa: E731
            SoundDeviceOutputConfig(device=_device(args.output_device))
        )
        app = DesktopApp(
            session,
            source=source,
            sink=sink_factory(),
            sink_factory=sink_factory,
            codec=codec,
            on_event=_print_event,
        )
        result = await app.run()
    else:
        if args.silence_frames <= 0:
            raise ValueError("--silence-frames must be positive")
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        pcm = (
            await asyncio.to_thread(args.input_pcm.read_bytes)
            if args.input_pcm is not None
            else b"\x00" * WIRE_BYTES_PER_FRAME * args.silence_frames
        )
        if not pcm:
            raise ValueError("headless PCM input must not be empty")
        source = FixturePcmSource(pcm, paced=True, pad_final_frame=True)
        recording = RecordingAudioSink() if args.output_pcm is not None else None
        sink = recording or NullAudioSink()
        app = DesktopApp(
            session,
            source=source,
            sink=sink,
            codec=codec,
            on_event=_print_event,
        )
        async with asyncio.timeout(args.timeout):
            result = await app.run(stop_after_playbacks=1)
        if recording is not None:
            await asyncio.to_thread(args.output_pcm.write_bytes, recording.pcm)
    print(
        "session complete "
        f"uplink_frames={result.uplink_frames} "
        f"playback_frames={result.playback_frames} "
        f"completed_playbacks={result.completed_playbacks}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(run_cli(args))
    except (ValueError, TimeoutError) as exc:
        parser.error(str(exc))
    except (OSError, OpusUnavailableError, SoundDeviceUnavailableError, RvaClientError) as exc:
        logging.getLogger(__name__).error("desktop client failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


def _device(value: str | None) -> int | str | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _environment_flag(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in _TRUE_ENV_VALUES:
        return True
    if value in _FALSE_ENV_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _read_bootstrap_token(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("unable to read --token-file") from exc
    if not raw or len(raw) > _MAX_TOKEN_FILE_BYTES:
        raise ValueError("--token-file must contain 1..4096 bytes")
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("--token-file must be UTF-8 text") from exc
    if not token or "\x00" in token:
        raise ValueError("--token-file contains an invalid token")
    return token


def _print_event(event: SessionEvent) -> None:
    if event.kind in {"transcript.delta", "transcript.final", "response.text"}:
        text = event.message.get("text")
        if isinstance(text, str) and text:
            print(f"{event.kind}: {text}")
    elif event.kind in {"session.opened", "session.close", "playback.stop"}:
        print(event.kind)


__all__ = ["build_parser", "client_config", "main", "run_cli"]

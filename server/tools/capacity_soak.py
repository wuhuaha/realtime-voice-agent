from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import struct
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
for source_root in (
    PRODUCT_ROOT / "clients" / "desktop_reference" / "src",
    PRODUCT_ROOT / "server" / "packages" / "voice_testkit" / "src",
):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from rva_desktop.app import DesktopApp  # noqa: E402
from rva_desktop.audio.fixture import FixturePcmSource, NullAudioSink, RecordingAudioSink  # noqa: E402
from rva_desktop.audio.opus import PyAvOpusCodec  # noqa: E402
from rva_desktop.audio.ports import (  # noqa: E402
    WIRE_BYTES_PER_FRAME,
    WIRE_FORMAT,
    WIRE_FRAME_DURATION_MS,
    WIRE_SAMPLES_PER_FRAME,
    AudioFormat,
    PcmFrame,
)
from rva_desktop.config import ClientConfig, MediaProfile  # noqa: E402
from rva_desktop.session.client import DesktopSession  # noqa: E402
from voice_testkit.subprocess_cluster import (  # noqa: E402
    BOOTSTRAP_TOKEN,
    INTERNAL_TOKEN,
    ProcessCluster,
    running_process_cluster,
)

DEFAULT_STEPS = (1, 5, 10)
LONG_CHURN_DURATIONS = (1_800.0, 7_200.0)
EXPECTED_PLAYBACK_FRAMES = 4


class HarnessInfrastructureError(RuntimeError):
    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(f"{stage}: {type(cause).__name__}")
        self.stage = stage
        self.exception_type = type(cause).__name__


@dataclass(frozen=True, slots=True)
class Scenario:
    concurrency: int
    duration_seconds: float
    seed: int
    profile: MediaProfile
    worker_max_sessions: int = 5
    require_observable_overlap: bool = False
    worker_count_override: int | None = None

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be positive")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be finite and positive")
        if self.worker_max_sessions < 1:
            raise ValueError("worker_max_sessions must be positive")
        if self.worker_count_override is not None and self.worker_count_override < 1:
            raise ValueError("worker_count_override must be positive")

    @property
    def worker_count(self) -> int:
        return self.worker_count_override or math.ceil(self.concurrency / self.worker_max_sessions)


@dataclass(frozen=True, slots=True)
class SteadyOptions:
    warmup_seconds: float
    measurement_seconds: float
    ramp_per_second: float
    device_prefix: str = "steady"

    def __post_init__(self) -> None:
        for name, value in (
            ("warmup_seconds", self.warmup_seconds),
            ("measurement_seconds", self.measurement_seconds),
            ("ramp_per_second", self.ramp_per_second),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not self.device_prefix or len(self.device_prefix) > 48:
            raise ValueError("device_prefix must contain 1..48 characters")


@dataclass(frozen=True, slots=True)
class SessionResult:
    device_id: str
    elapsed_ms: float
    uplink_frames: int
    playback_frames: int
    completed_playbacks: int
    route_reacquired_and_release_request_accepted: bool
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class SteadySessionResult:
    device_id: str
    connect_ms: float | None
    elapsed_ms: float
    uplink_frames: int
    playback_frames: int
    completed_playbacks: int
    source_late_frames: int
    route_reacquired_and_release_request_accepted: bool = False
    error_type: str | None = None


class _SteadyPcmSource:
    """Infinite real-time source used only by the provider-free capacity harness."""

    def __init__(self) -> None:
        self._frame = _speech_like_pcm()
        self._index = 0
        self._started_at: float | None = None
        self._started = False
        self._closed = False
        self.late_frames = 0

    @property
    def format(self) -> AudioFormat:
        return WIRE_FORMAT

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("audio source is closed")
        self._started = True

    async def read_frame(self) -> PcmFrame | None:
        if not self._started:
            raise RuntimeError("audio source is not started")
        if self._closed:
            return None
        if self._started_at is None:
            self._started_at = time.monotonic()
        deadline = self._started_at + self._index * (WIRE_FRAME_DURATION_MS / 1_000)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
        captured_at = time.monotonic()
        if captured_at - deadline > WIRE_FRAME_DURATION_MS / 1_000:
            self.late_frames += 1
        sequence = self._index
        self._index += 1
        return PcmFrame(
            data=self._frame,
            sequence=sequence,
            timestamp_samples=sequence * WIRE_SAMPLES_PER_FRAME,
            captured_at=captured_at,
        )

    async def close(self) -> None:
        self._closed = True


class _PreencodedBenchmarkCodec:
    """Keep client CPU out of the Server benchmark while preserving valid Opus ingress."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def encode_60ms(self, pcm16le: bytes) -> bytes:
        if len(pcm16le) != WIRE_BYTES_PER_FRAME:
            raise ValueError("benchmark codec received an invalid PCM frame")
        return self._payload

    def decode_60ms(self, payload: bytes) -> bytes:
        if not payload:
            raise ValueError("benchmark codec received an empty Opus payload")
        return b"\x00" * WIRE_BYTES_PER_FRAME

    def conceal_60ms(self) -> bytes:
        return b"\x00" * WIRE_BYTES_PER_FRAME

    def close(self) -> None:
        return None


class _OpenedGate:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.count = 0
        self.all_opened = asyncio.Event()

    def arrive(self) -> None:
        self.count += 1
        if self.count >= self.expected:
            self.all_opened.set()


def _speech_like_pcm() -> bytes:
    samples = (
        round(2_200 * math.sin(2 * math.pi * 220 * index / 16_000))
        + round(700 * math.sin(2 * math.pi * 660 * index / 16_000))
        for index in range(WIRE_SAMPLES_PER_FRAME)
    )
    return struct.pack(f"<{WIRE_SAMPLES_PER_FRAME}h", *samples)


def _benchmark_opus_payload() -> bytes:
    codec = PyAvOpusCodec()
    try:
        return codec.encode_60ms(_speech_like_pcm())
    finally:
        codec.close()


class JsonlRecorder:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("w", encoding="utf-8", newline="\n")
        self._lock = Lock()

    def emit(self, event: str, **fields: object) -> None:
        record = {"recorded_at": time.time(), "event": event, **fields}
        with self._lock:
            self._stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            self._stream.flush()

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> JsonlRecorder:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _SessionOverlapGate:
    """Keeps short fixture sessions open until their concurrency is observable."""

    def __init__(self, expected_sessions: int) -> None:
        self._expected_sessions = expected_sessions
        self._arrived: set[str] = set()
        self.all_arrived = asyncio.Event()
        self._released = asyncio.Event()

    async def hold(self, device_id: str) -> None:
        self._arrived.add(device_id)
        if len(self._arrived) >= self._expected_sessions:
            self.all_arrived.set()
        await self._released.wait()

    def release(self) -> None:
        self._released.set()


def parse_steps(value: str) -> tuple[int, ...]:
    try:
        steps = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("steps must be comma-separated integers") from exc
    if not steps or any(step < 1 for step in steps) or len(set(steps)) != len(steps):
        raise argparse.ArgumentTypeError("steps must be unique positive integers")
    return steps


def server_python() -> Path:
    executable = "python.exe" if sys.platform == "win32" else "python"
    folder = "Scripts" if sys.platform == "win32" else "bin"
    candidate = PRODUCT_ROOT / "server" / ".venv" / folder / executable
    if not candidate.is_file():
        raise RuntimeError(f"server environment is missing: {candidate}")
    return candidate


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def media_completion_error(
    *,
    uplink_frames: int,
    playback_frames: int,
    completed_playbacks: int,
) -> str | None:
    if uplink_frames <= 0 or playback_frames != EXPECTED_PLAYBACK_FRAMES or completed_playbacks != 1:
        return "media_incomplete"
    return None


def deterministic_event_completion_error(events: Sequence[Any]) -> str | None:
    kinds = [event.kind for event in events]
    if any(kind in {"session.error", "playback.stop"} for kind in kinds):
        return "event_terminal_not_completed"
    opened = [event for event in events if event.kind == "session.opened"]
    begins = [event for event in events if event.kind == "response.begin"]
    media = [event for event in events if event.kind == "media.audio"]
    ends = [event for event in events if event.kind == "response.end"]
    if len(opened) != 1 or len(begins) != 1 or len(media) != EXPECTED_PLAYBACK_FRAMES or len(ends) != 1:
        return "event_closure_incomplete"
    if ends[0].message.get("outcome") != "completed":
        return "event_terminal_not_completed"
    begin_index = events.index(begins[0])
    if events.index(opened[0]) > begin_index or any(events.index(event) < begin_index for event in (*media, *ends)):
        return "event_closure_out_of_order"
    target = begins[0].target
    if target is None or any(event.target != target for event in (*media, *ends)):
        return "event_closure_target_mismatch"
    return None


def workers_over_capacity(worker_peaks: dict[str, int], *, worker_max_sessions: int) -> dict[str, int]:
    return {worker_id: peak for worker_id, peak in worker_peaks.items() if peak > worker_max_sessions}


def classify_scenario_status(
    *,
    has_session_failures: bool,
    has_route_failures: bool,
    concurrency_observed: bool,
    capacity_excess: dict[str, int],
) -> str:
    if has_session_failures or has_route_failures or capacity_excess:
        return "failed"
    return "measured" if concurrency_observed else "inconclusive"


async def _workers(director_url: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(base_url=director_url, timeout=2) as client:
        response = await client.get("/internal/v1/workers", headers={"X-Internal-Token": INTERNAL_TOKEN})
        response.raise_for_status()
        value = response.json()
    if not isinstance(value, list):
        raise ValueError("worker registry response must be a list")
    for worker in value:
        if (
            not isinstance(worker, dict)
            or not isinstance(worker.get("worker_id"), str)
            or not isinstance(worker.get("active_sessions"), int)
            or isinstance(worker.get("active_sessions"), bool)
            or worker["active_sessions"] < 0
        ):
            raise ValueError("worker registry contains an invalid worker snapshot")
    return value


async def _wait_workers_idle(director_url: str, *, timeout: float = 8.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last: list[dict[str, Any]] = []
    last_error: Exception | None = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            last = await asyncio.wait_for(_workers(director_url), timeout=remaining)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code not in {408, 429} and status_code < 500:
                raise HarnessInfrastructureError("worker_idle_wait", exc) from exc
            last_error = exc
        except httpx.TransportError as exc:
            last_error = exc
        except httpx.HTTPError as exc:
            raise HarnessInfrastructureError("worker_idle_wait", exc) from exc
        except TimeoutError as exc:
            last_error = exc
        except (TypeError, ValueError) as exc:
            raise HarnessInfrastructureError("worker_idle_wait", exc) from exc
        else:
            last_error = None
            if last and all(worker["active_sessions"] == 0 for worker in last):
                return last
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(0.1, remaining))
    cause: Exception = last_error or TimeoutError(
        f"workers did not return to zero active sessions: {last}"
    )
    raise HarnessInfrastructureError(
        "worker_idle_wait",
        cause,
    )


async def _observe_worker_load(
    cluster: ProcessCluster,
    stop: asyncio.Event,
    *,
    target_concurrency: int,
    target_observed: asyncio.Event,
) -> tuple[dict[str, int], int]:
    peaks: dict[str, int] = {}
    total_peak = 0
    while not stop.is_set():
        try:
            workers = await _workers(cluster.director_url)
        except (httpx.HTTPError, TimeoutError, TypeError, ValueError):
            workers = []
        sample_total = 0
        for worker in workers:
            active = worker["active_sessions"]
            worker_id = worker["worker_id"]
            peaks[worker_id] = max(peaks.get(worker_id, 0), active)
            sample_total += active
        total_peak = max(total_peak, sample_total)
        if total_peak >= target_concurrency:
            target_observed.set()
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05)
        except TimeoutError:
            pass
    return peaks, total_peak


async def _verify_route_reacquired_and_release_request_accepted(
    director_url: str,
    scenario: Scenario,
    device_id: str,
) -> bool:
    deadline = time.monotonic() + 3.0
    headers = {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}
    request = {
        "tenant_id": "capacity-soak",
        "device_id": device_id,
        "supported_profiles": [scenario.profile.value],
        "control_protocol": "rva/1",
    }
    async with httpx.AsyncClient(base_url=director_url, timeout=None) as client:
        first_opened = await _post_route_request_with_retry(
            client, "/v1/session/bootstrap", headers=headers, payload=request, deadline=deadline
        )
        first_route = first_opened.json()
        _validate_route_payload(first_route)
        await _post_route_request_with_retry(
            client,
            "/v1/session/release",
            headers=headers,
            payload={
                "tenant_id": "capacity-soak",
                "device_id": device_id,
                "worker_id": first_route["worker_id"],
                "session_epoch": first_route["session_epoch"],
                "fencing_token": first_route["fencing_token"],
            },
            deadline=deadline,
        )
        second_opened = await _post_route_request_with_retry(
            client, "/v1/session/bootstrap", headers=headers, payload=request, deadline=deadline
        )
        second_route = second_opened.json()
        _validate_route_payload(second_route)
        route_advanced = (
            second_route["session_epoch"] != first_route["session_epoch"]
            and second_route["fencing_token"] > first_route["fencing_token"]
        )
        await _post_route_request_with_retry(
            client,
            "/v1/session/release",
            headers=headers,
            payload={
                "tenant_id": "capacity-soak",
                "device_id": device_id,
                "worker_id": second_route["worker_id"],
                "session_epoch": second_route["session_epoch"],
                "fencing_token": second_route["fencing_token"],
            },
            deadline=deadline,
        )
        return route_advanced


async def _post_route_request_with_retry(
    client: httpx.AsyncClient,
    path: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    deadline: float,
) -> httpx.Response:
    last_error: Exception | None = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            response = await asyncio.wait_for(
                client.post(path, headers=headers, json=payload),
                timeout=remaining,
            )
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"route request returned unexpected status {response.status_code}")
            return response
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code not in {408, 429} and not 500 <= status_code < 600:
                raise
            last_error = exc
            await exc.response.aclose()
        except (httpx.TransportError, TimeoutError) as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(0.1, remaining))
    raise last_error or TimeoutError("route verification deadline expired")


def _validate_route_payload(route: Any) -> None:
    if (
        not isinstance(route, dict)
        or not isinstance(route.get("worker_id"), str)
        or not route["worker_id"]
        or not isinstance(route.get("session_epoch"), str)
        or not route["session_epoch"]
        or not isinstance(route.get("fencing_token"), int)
        or isinstance(route.get("fencing_token"), bool)
        or route["fencing_token"] < 1
    ):
        raise ValueError("route response contains an invalid route payload")


async def _exercise_drain(
    cluster: ProcessCluster,
    scenario: Scenario,
    recorder: JsonlRecorder,
) -> dict[str, str]:
    if len(cluster.workers) < 2:
        return {"scenario": "worker_drain", "status": "not_run", "reason": "requires at least two workers"}
    drained_worker = cluster.workers[0]
    headers = {"X-Internal-Token": INTERNAL_TOKEN}
    async with httpx.AsyncClient(timeout=3) as client:
        response = await client.post(
            f"http://127.0.0.1:{drained_worker.http_port}/internal/v1/drain",
            headers=headers,
        )
        response.raise_for_status()
        deadline = time.monotonic() + 4
        draining = False
        while time.monotonic() < deadline:
            workers = await _workers(cluster.director_url)
            draining = any(
                worker["worker_id"] == drained_worker.worker_id and worker["draining"] is True
                for worker in workers
            )
            if draining:
                break
            await asyncio.sleep(0.1)
        if not draining:
            raise RuntimeError("drain state did not reach Director")
        device_id = f"capacity-drain-{uuid.uuid4().hex[:12]}"
        auth = {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}
        opened = await client.post(
            f"{cluster.director_url}/v1/session/bootstrap",
            headers=auth,
            json={
                "tenant_id": "capacity-soak",
                "device_id": device_id,
                "supported_profiles": [scenario.profile.value],
                "control_protocol": "rva/1",
            },
        )
        opened.raise_for_status()
        route = opened.json()
        if route["worker_id"] == drained_worker.worker_id:
            raise RuntimeError("Director routed a new lease to a draining worker")
        released = await client.post(
            f"{cluster.director_url}/v1/session/release",
            headers=auth,
            json={
                "tenant_id": "capacity-soak",
                "device_id": device_id,
                "worker_id": route["worker_id"],
                "session_epoch": route["session_epoch"],
                "fencing_token": route["fencing_token"],
            },
        )
        released.raise_for_status()
    recorder.emit(
        "fault.worker_drain.measured",
        drained_worker_id=drained_worker.worker_id,
        selected_worker_id=route["worker_id"],
    )
    return {"scenario": "worker_drain", "status": "measured", "reason": "fresh route avoided drained worker"}


async def _one_session(
    cluster: ProcessCluster,
    scenario: Scenario,
    device_id: str,
    recorder: JsonlRecorder,
    startup_delay: float,
    overlap_gate: _SessionOverlapGate | None,
) -> SessionResult:
    await asyncio.sleep(startup_delay)
    started = time.monotonic()
    events: list[Any] = []

    async def on_event(event: Any) -> None:
        events.append(event)
        if event.kind == "session.opened" and overlap_gate is not None:
            await overlap_gate.hold(device_id)

    session = DesktopSession(
        ClientConfig(
            director_url=cluster.director_url,
            bootstrap_token=BOOTSTRAP_TOKEN,
            device_id=device_id,
            tenant_id="capacity-soak",
            supported_profiles=(scenario.profile,),
            preferred_profile=scenario.profile,
            connect_timeout_seconds=10,
            control_timeout_seconds=10,
            # Capacity/churn validates lifecycle under host scheduling pressure.
            # Media latency budgets belong to the separate network benchmark.
            media_max_age_seconds=2.0,
            allow_insecure_loopback=True,
        )
    )
    codec = PyAvOpusCodec()
    source = FixturePcmSource(b"\x00" * WIRE_BYTES_PER_FRAME * 3, paced=True)
    sink = RecordingAudioSink(max_frames=4)
    try:
        codec.encode_60ms(b"\x00" * WIRE_BYTES_PER_FRAME)
        app = DesktopApp(session, source=source, sink=sink, codec=codec, on_event=on_event)
        result = await asyncio.wait_for(app.run(stop_after_playbacks=1), timeout=20)
        elapsed_ms = (time.monotonic() - started) * 1_000
        completion_error = media_completion_error(
            uplink_frames=result.uplink_frames,
            playback_frames=result.playback_frames,
            completed_playbacks=result.completed_playbacks,
        )
        completion_error = completion_error or deterministic_event_completion_error(events)
        event_counts = {kind: sum(event.kind == kind for event in events) for kind in {event.kind for event in events}}
        response_outcomes = [
            event.message.get("outcome") for event in events if event.kind == "response.end"
        ]
        if completion_error is not None:
            recorder.emit(
                "session.failed",
                device_id=device_id,
                profile=scenario.profile.value,
                elapsed_ms=round(elapsed_ms, 3),
                uplink_frames=result.uplink_frames,
                playback_frames=result.playback_frames,
                completed_playbacks=result.completed_playbacks,
                event_counts=event_counts,
                response_outcomes=response_outcomes,
                error_type=completion_error,
            )
            return SessionResult(
                device_id,
                elapsed_ms,
                result.uplink_frames,
                result.playback_frames,
                result.completed_playbacks,
                False,
                completion_error,
            )
        recorder.emit(
            "session.completed",
            device_id=device_id,
            profile=scenario.profile.value,
            elapsed_ms=round(elapsed_ms, 3),
            uplink_frames=result.uplink_frames,
            playback_frames=result.playback_frames,
            completed_playbacks=result.completed_playbacks,
            event_counts=event_counts,
            response_outcomes=response_outcomes,
        )
        return SessionResult(
            device_id,
            elapsed_ms,
            result.uplink_frames,
            result.playback_frames,
            result.completed_playbacks,
            False,
        )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1_000
        recorder.emit(
            "session.failed",
            device_id=device_id,
            profile=scenario.profile.value,
            elapsed_ms=round(elapsed_ms, 3),
            error_type=type(exc).__name__,
        )
        return SessionResult(device_id, elapsed_ms, 0, 0, 0, False, type(exc).__name__)


async def _one_steady_session(
    cluster: ProcessCluster,
    scenario: Scenario,
    options: SteadyOptions,
    device_id: str,
    recorder: JsonlRecorder,
    startup_delay: float,
    stop: asyncio.Event,
    opened_gate: _OpenedGate,
    opus_payload: bytes,
) -> SteadySessionResult:
    await asyncio.sleep(startup_delay)
    started = time.monotonic()
    connect_ms: float | None = None
    opened_reported = False
    event_counts: dict[str, int] = {}

    async def on_event(event: Any) -> None:
        nonlocal connect_ms, opened_reported
        event_counts[event.kind] = event_counts.get(event.kind, 0) + 1
        if event.kind == "session.opened" and not opened_reported:
            opened_reported = True
            connect_ms = (time.monotonic() - started) * 1_000
            opened_gate.arrive()

    session = DesktopSession(
        ClientConfig(
            director_url=cluster.director_url,
            bootstrap_token=BOOTSTRAP_TOKEN,
            device_id=device_id,
            tenant_id="capacity-soak",
            supported_profiles=(scenario.profile,),
            preferred_profile=scenario.profile,
            connect_timeout_seconds=15,
            control_timeout_seconds=15,
            media_max_age_seconds=2.0,
            allow_insecure_loopback=True,
        )
    )
    source = _SteadyPcmSource()
    app = DesktopApp(
        session,
        source=source,
        sink=NullAudioSink(),
        codec=_PreencodedBenchmarkCodec(opus_payload),
        on_event=on_event,
    )
    timeout = startup_delay + options.warmup_seconds + options.measurement_seconds + 45
    try:
        result = await asyncio.wait_for(app.run(stop_event=stop), timeout=timeout)
        elapsed_ms = (time.monotonic() - started) * 1_000
        minimum_uplink_frames = max(
            3,
            math.floor(
                (options.warmup_seconds + options.measurement_seconds)
                * 1_000
                / WIRE_FRAME_DURATION_MS
                * 0.995
            ),
        )
        error_type: str | None = None
        if connect_ms is None:
            error_type = "session_not_opened"
        elif result.uplink_frames < minimum_uplink_frames:
            error_type = "uplink_cadence_incomplete"
        elif result.playback_frames != EXPECTED_PLAYBACK_FRAMES or result.completed_playbacks != 1:
            error_type = "initial_playback_incomplete"
        elif (
            event_counts.get("session.opened") != 1
            or event_counts.get("response.begin") != 1
            or event_counts.get("response.end") != 1
            or event_counts.get("media.audio") != EXPECTED_PLAYBACK_FRAMES
        ):
            error_type = "event_closure_incomplete"
        recorder.emit(
            "steady.session.completed" if error_type is None else "steady.session.failed",
            device_id=device_id,
            profile=scenario.profile.value,
            connect_ms=round(connect_ms, 3) if connect_ms is not None else None,
            elapsed_ms=round(elapsed_ms, 3),
            uplink_frames=result.uplink_frames,
            playback_frames=result.playback_frames,
            completed_playbacks=result.completed_playbacks,
            source_late_frames=source.late_frames,
            event_counts=event_counts,
            error_type=error_type,
        )
        return SteadySessionResult(
            device_id=device_id,
            connect_ms=connect_ms,
            elapsed_ms=elapsed_ms,
            uplink_frames=result.uplink_frames,
            playback_frames=result.playback_frames,
            completed_playbacks=result.completed_playbacks,
            source_late_frames=source.late_frames,
            error_type=error_type,
        )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1_000
        recorder.emit(
            "steady.session.failed",
            device_id=device_id,
            profile=scenario.profile.value,
            connect_ms=round(connect_ms, 3) if connect_ms is not None else None,
            elapsed_ms=round(elapsed_ms, 3),
            event_counts=event_counts,
            error_type=type(exc).__name__,
        )
        return SteadySessionResult(
            device_id=device_id,
            connect_ms=connect_ms,
            elapsed_ms=elapsed_ms,
            uplink_frames=0,
            playback_frames=0,
            completed_playbacks=0,
            source_late_frames=source.late_frames,
            error_type=type(exc).__name__,
        )


async def _verify_steady_routes(
    director_url: str,
    scenario: Scenario,
    results: list[SteadySessionResult],
    recorder: JsonlRecorder,
) -> list[SteadySessionResult]:
    semaphore = asyncio.Semaphore(20)

    async def verify(result: SteadySessionResult) -> SteadySessionResult:
        if result.error_type is not None:
            return result
        try:
            async with semaphore:
                verified = await _verify_route_reacquired_and_release_request_accepted(
                    director_url,
                    scenario,
                    result.device_id,
                )
        except Exception:
            verified = False
        recorder.emit(
            (
                "route_reacquired_and_release_request_accepted.verified"
                if verified
                else "route_reacquired_and_release_request_accepted.not_verified"
            ),
            device_id=result.device_id,
            profile=scenario.profile.value,
            session_succeeded=result.error_type is None,
        )
        return replace(result, route_reacquired_and_release_request_accepted=verified)

    return list(await asyncio.gather(*(verify(result) for result in results)))


async def run_steady_scenario(
    cluster: ProcessCluster,
    scenario: Scenario,
    options: SteadyOptions,
    recorder: JsonlRecorder,
) -> dict[str, Any]:
    scenario_started = time.monotonic()
    stop = asyncio.Event()
    opened_gate = _OpenedGate(scenario.concurrency)
    opus_payload = _benchmark_opus_payload()
    recorder.emit(
        "steady.scenario.started",
        scenario=_scenario_dict(scenario),
        options=asdict(options),
        worker_count=len(cluster.workers),
        opus_payload_bytes=len(opus_payload),
    )
    observer_stop = asyncio.Event()
    target_observed = asyncio.Event()
    observer = asyncio.create_task(
        _observe_worker_load(
            cluster,
            observer_stop,
            target_concurrency=scenario.concurrency,
            target_observed=target_observed,
        ),
        name="steady-worker-load-observer",
    )
    tasks = [
        asyncio.create_task(
            _one_steady_session(
                cluster,
                scenario,
                options,
                f"{options.device_prefix}-{scenario.profile.value.replace('/', '-')}-{index}",
                recorder,
                index / options.ramp_per_second,
                stop,
                opened_gate,
                opus_payload,
            ),
            name=f"steady-session-{index}",
        )
        for index in range(scenario.concurrency)
    ]
    open_timeout = scenario.concurrency / options.ramp_per_second + 45
    all_opened = False
    try:
        await asyncio.wait_for(opened_gate.all_opened.wait(), timeout=open_timeout)
        all_opened = True
        await asyncio.sleep(options.warmup_seconds + options.measurement_seconds)
    except TimeoutError:
        recorder.emit(
            "steady.open_timeout",
            opened=opened_gate.count,
            expected=scenario.concurrency,
            timeout_seconds=round(open_timeout, 3),
        )
    finally:
        stop.set()
    results = list(await asyncio.gather(*tasks))
    observer_stop.set()
    await asyncio.gather(observer, return_exceptions=True)
    worker_peaks, active_sessions_peak_total = observer.result()
    workers = await _wait_workers_idle(cluster.director_url, timeout=30)
    verified = await _verify_steady_routes(cluster.director_url, scenario, results, recorder)

    attempted = len(verified)
    successful = [result for result in verified if result.error_type is None]
    initial_playback = [
        result
        for result in verified
        if result.playback_frames == EXPECTED_PLAYBACK_FRAMES and result.completed_playbacks == 1
    ]
    verified_routes = [result for result in successful if result.route_reacquired_and_release_request_accepted]
    sent_frames = sum(result.uplink_frames for result in verified)
    late_frames = sum(result.source_late_frames for result in verified)
    connect_values = [result.connect_ms for result in verified if result.connect_ms is not None]
    capacity_excess = workers_over_capacity(worker_peaks, worker_max_sessions=scenario.worker_max_sessions)
    session_rate = len(successful) / attempted if attempted else 0.0
    playback_rate = len(initial_playback) / attempted if attempted else 0.0
    route_rate = len(verified_routes) / attempted if attempted else 0.0
    cadence_late_rate = late_frames / sent_frames if sent_frames else 1.0
    generator_valid = cadence_late_rate <= 0.01
    target_seen = active_sessions_peak_total >= scenario.concurrency
    status = (
        "measured"
        if all_opened
        and session_rate >= 0.99
        and playback_rate >= 0.99
        and route_rate >= 0.99
        and generator_valid
        and target_seen
        and not capacity_excess
        else "failed"
    )
    failure_types = sorted({result.error_type for result in verified if result.error_type})
    if not all_opened:
        failure_types.append("all_sessions_not_opened")
    if not generator_valid:
        failure_types.append("client_generator_cadence_invalid")
    if not target_seen:
        failure_types.append("target_concurrency_not_observed")
    if route_rate < 0.99:
        failure_types.append("route_reacquire_below_threshold")
    if capacity_excess:
        failure_types.append("worker_capacity_exceeded")
    summary = {
        "status": status,
        "evidence_scope": "steady_provider_free_uplink_and_initial_downlink",
        "server_media_processing": {
            "status": "requires_server_close_log_aggregation",
            "reason": "client send counters cannot prove every packet crossed the Server decode boundary",
        },
        "scenario": _scenario_dict(scenario),
        "options": asdict(options),
        "elapsed_seconds": round(time.monotonic() - scenario_started, 3),
        "sessions_attempted": attempted,
        "sessions_succeeded": len(successful),
        "sessions_failed": attempted - len(successful),
        "session_survival_rate": round(session_rate, 6),
        "initial_playback_rate": round(playback_rate, 6),
        "route_reacquire_rate": round(route_rate, 6),
        "connect_latency_ms": {
            "p50": _percentile(connect_values, 0.50),
            "p95": _percentile(connect_values, 0.95),
            "p99": _percentile(connect_values, 0.99),
            "max": round(max(connect_values), 3) if connect_values else None,
        },
        "frames": {
            "client_uplink_sent": sent_frames,
            "initial_playback": sum(result.playback_frames for result in verified),
            "client_source_late": late_frames,
            "client_source_late_rate": round(cadence_late_rate, 8),
        },
        "client_generator_valid": generator_valid,
        "worker_active_sessions_final": {worker["worker_id"]: worker["active_sessions"] for worker in workers},
        "worker_active_sessions_peak": worker_peaks,
        "active_sessions_peak_total": active_sessions_peak_total,
        "target_concurrency_observed": target_seen,
        "workers_over_capacity": capacity_excess,
        "failure_types": sorted(set(failure_types)),
    }
    recorder.emit("steady.scenario.finished", summary=summary)
    return summary


async def run_scenario(cluster: ProcessCluster, scenario: Scenario, recorder: JsonlRecorder) -> dict[str, Any]:
    rng = random.Random(scenario.seed)
    scenario_started = time.monotonic()
    deadline = time.monotonic() + scenario.duration_seconds
    results: list[SessionResult] = []
    worker_peaks: dict[str, int] = {}
    active_sessions_peak_total = 0
    round_number = 0
    recorder.emit("scenario.started", scenario=_scenario_dict(scenario), worker_count=len(cluster.workers))
    while round_number == 0 or time.monotonic() < deadline:
        round_number += 1
        overlap_gate = (
            _SessionOverlapGate(scenario.concurrency)
            if scenario.require_observable_overlap
            else None
        )
        tasks = [
            asyncio.create_task(
                _one_session(
                    cluster,
                    scenario,
                    f"capacity-{scenario.profile.value.replace('/', '-')}-{round_number}-{index}",
                    recorder,
                    rng.uniform(0, 0.02),
                    overlap_gate,
                ),
                name=f"capacity-session-{round_number}-{index}",
            )
            for index in range(scenario.concurrency)
        ]
        stop_observer = asyncio.Event()
        target_observed = asyncio.Event()
        observer = asyncio.create_task(
            _observe_worker_load(
                cluster,
                stop_observer,
                target_concurrency=scenario.concurrency,
                target_observed=target_observed,
            ),
            name="capacity-worker-load-observer",
        )
        all_arrived = (
            asyncio.create_task(
                overlap_gate.all_arrived.wait(),
                name="capacity-session-overlap",
            )
            if overlap_gate is not None
            else None
        )
        try:
            if overlap_gate is not None and all_arrived is not None:
                completed, _ = await asyncio.wait(
                    {all_arrived, *tasks},
                    timeout=2.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if all_arrived in completed:
                    try:
                        await asyncio.wait_for(target_observed.wait(), timeout=1.0)
                    except TimeoutError:
                        pass
                overlap_gate.release()
            round_results = list(await asyncio.gather(*tasks))
        finally:
            if overlap_gate is not None:
                overlap_gate.release()
            stop_observer.set()
            cleanup_tasks = [*tasks, observer]
            if all_arrived is not None:
                cleanup_tasks.append(all_arrived)
            if sys.exception() is not None:
                for task in cleanup_tasks:
                    if not task.done():
                        task.cancel()
            elif all_arrived is not None and not all_arrived.done():
                all_arrived.cancel()
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        observed, observed_total_peak = observer.result()
        active_sessions_peak_total = max(active_sessions_peak_total, observed_total_peak)
        for worker_id, peak in observed.items():
            worker_peaks[worker_id] = max(worker_peaks.get(worker_id, 0), peak)
        await _wait_workers_idle(cluster.director_url)
        verified: list[SessionResult] = []
        for result in round_results:
            try:
                route_verified = (
                    result.error_type is None
                    and await _verify_route_reacquired_and_release_request_accepted(
                        cluster.director_url,
                        scenario,
                        result.device_id,
                    )
                )
            except Exception as exc:
                raise HarnessInfrastructureError("route_verification", exc) from exc
            verified.append(
                replace(
                    result,
                    route_reacquired_and_release_request_accepted=route_verified,
                )
            )
            recorder.emit(
                (
                    "route_reacquired_and_release_request_accepted.verified"
                    if route_verified
                    else "route_reacquired_and_release_request_accepted.not_verified"
                ),
                device_id=result.device_id,
                profile=scenario.profile.value,
                session_succeeded=result.error_type is None,
            )
        results.extend(verified)
    workers = await _wait_workers_idle(cluster.director_url)
    elapsed = [result.elapsed_ms for result in results if result.error_type is None]
    failures = [result for result in results if result.error_type is not None]
    release_failures = [
        result
        for result in results
        if result.error_type is None and not result.route_reacquired_and_release_request_accepted
    ]
    concurrency_observed = active_sessions_peak_total >= scenario.concurrency
    capacity_excess = workers_over_capacity(
        worker_peaks,
        worker_max_sessions=scenario.worker_max_sessions,
    )
    status = classify_scenario_status(
        has_session_failures=bool(failures),
        has_route_failures=bool(release_failures),
        concurrency_observed=concurrency_observed,
        capacity_excess=capacity_excess,
    )
    summary = {
        "status": status,
        "evidence_scope": "session_churn",
        "continuous_session_soak": {
            "status": "not_run",
            "reason": "this workload repeatedly opens and closes short deterministic sessions",
        },
        "scenario": _scenario_dict(scenario),
        "rounds": round_number,
        "elapsed_seconds": round(time.monotonic() - scenario_started, 3),
        "sessions_attempted": len(results),
        "sessions_succeeded": len(results) - len(failures),
        "sessions_failed": len(failures),
        "latency_ms": {
            "p50": _percentile(elapsed, 0.50),
            "p95": _percentile(elapsed, 0.95),
            "p99": _percentile(elapsed, 0.99),
            "max": round(max(elapsed), 3) if elapsed else None,
        },
        "frames": {
            "uplink": sum(result.uplink_frames for result in results),
            "playback": sum(result.playback_frames for result in results),
        },
        "route_reacquired_and_release_request_accepted_count": sum(
            result.route_reacquired_and_release_request_accepted for result in results
        ),
        "worker_active_sessions_final": {worker["worker_id"]: worker["active_sessions"] for worker in workers},
        "worker_active_sessions_peak": worker_peaks,
        "active_sessions_peak_total": active_sessions_peak_total,
        "target_concurrency_observed": concurrency_observed,
        "workers_over_capacity": capacity_excess,
        "failure_types": [
            *sorted({result.error_type for result in failures if result.error_type}),
            *(["route_reacquired_and_release_request_not_verified"] if release_failures else []),
            *(["worker_capacity_exceeded"] if capacity_excess else []),
            *(["target_concurrency_not_observed"] if not concurrency_observed else []),
        ],
    }
    recorder.emit("scenario.finished", summary=summary)
    return summary


def _scenario_dict(scenario: Scenario) -> dict[str, object]:
    value = asdict(scenario)
    value["profile"] = scenario.profile.value
    return value


def run_local_scenario(
    scenario: Scenario,
    recorder: JsonlRecorder,
    *,
    temp_root: Path,
    redis_url: str | None = None,
) -> dict[str, Any]:
    prefix = f"rva-capacity-{uuid.uuid4().hex}" if redis_url else None
    cluster_entered = False
    execution_completed = False
    try:
        with running_process_cluster(
            temp_root,
            worker_count=scenario.worker_count,
            worker_max_sessions=scenario.worker_max_sessions,
            udp_enabled=scenario.profile is MediaProfile.UDP_OPUS_GCM_V1,
            redis_url=redis_url,
            redis_prefix=prefix,
            python_executable=server_python(),
            route_lease_ttl_seconds=5,
        ) as cluster:
            cluster_entered = True

            async def execute() -> dict[str, Any]:
                try:
                    summary = await run_scenario(cluster, scenario, recorder)
                except HarnessInfrastructureError:
                    raise
                except Exception as exc:
                    raise HarnessInfrastructureError("scenario_execution", exc) from exc
                try:
                    summary["fault_scenarios"] = [await _exercise_drain(cluster, scenario, recorder)]
                except Exception as exc:
                    raise HarnessInfrastructureError("worker_drain", exc) from exc
                return summary

            summary = asyncio.run(execute())
            execution_completed = True
    except HarnessInfrastructureError:
        raise
    except Exception as exc:
        stage = "process_reclamation" if cluster_entered else "cluster_startup"
        if cluster_entered and not execution_completed:
            stage = "process_reclamation"
        raise HarnessInfrastructureError(stage, exc) from exc
    # The context manager raises unless all child processes exit and every reserved
    # TCP/UDP port can be rebound after reaping.
    summary["process_and_port_reclamation"] = "measured"
    return summary


def run_local_steady_scenario(
    scenario: Scenario,
    options: SteadyOptions,
    recorder: JsonlRecorder,
    *,
    temp_root: Path,
    redis_url: str | None = None,
) -> dict[str, Any]:
    prefix = f"rva-capacity-{uuid.uuid4().hex}" if redis_url else None
    cluster_entered = False
    execution_completed = False
    try:
        with running_process_cluster(
            temp_root,
            worker_count=scenario.worker_count,
            worker_max_sessions=scenario.worker_max_sessions,
            udp_enabled=scenario.profile is MediaProfile.UDP_OPUS_GCM_V1,
            redis_url=redis_url,
            redis_prefix=prefix,
            python_executable=server_python(),
            route_lease_ttl_seconds=max(
                60,
                min(300, options.warmup_seconds + options.measurement_seconds + 60),
            ),
        ) as cluster:
            cluster_entered = True
            try:
                summary = asyncio.run(run_steady_scenario(cluster, scenario, options, recorder))
            except Exception as exc:
                raise HarnessInfrastructureError("steady_scenario_execution", exc) from exc
            execution_completed = True
    except HarnessInfrastructureError:
        raise
    except Exception as exc:
        stage = "process_reclamation" if cluster_entered else "cluster_startup"
        if cluster_entered and not execution_completed:
            stage = "process_reclamation"
        raise HarnessInfrastructureError(stage, exc) from exc
    summary["process_and_port_reclamation"] = "measured"
    summary["fault_scenarios"] = []
    return summary


def run_external_scenario(
    scenario: Scenario,
    recorder: JsonlRecorder,
    *,
    director_url: str,
    steady_options: SteadyOptions | None = None,
) -> dict[str, Any]:
    cluster = ProcessCluster(director_url.rstrip("/"), 0, ())
    try:
        if steady_options is not None:
            summary = asyncio.run(run_steady_scenario(cluster, scenario, steady_options, recorder))
        else:
            summary = asyncio.run(run_scenario(cluster, scenario, recorder))
    except HarnessInfrastructureError:
        raise
    except Exception as exc:
        raise HarnessInfrastructureError("external_scenario_execution", exc) from exc
    summary["process_and_port_reclamation"] = "external_orchestrator_required"
    summary["fault_scenarios"] = []
    return summary


def infrastructure_failure_summary(
    scenario: Scenario,
    failure: HarnessInfrastructureError,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "evidence_scope": "session_churn",
        "scenario": _scenario_dict(scenario),
        "infrastructure_failure": {
            "stage": failure.stage,
            "exception_type": failure.exception_type,
        },
        "continuous_session_soak": {
            "status": "not_run",
            "reason": "scenario infrastructure failed before a complete measurement",
        },
        "process_and_port_reclamation": "context_managed",
        "fault_scenarios": [],
    }


def not_run_faults() -> list[dict[str, str]]:
    return [
        {
            "scenario": "provider_429",
            "status": "not_run",
            "reason": (
                "deterministic runner has no provider call; "
                "provider fault remains covered by focused provider tests"
            ),
        },
        {
            "scenario": "provider_timeout",
            "status": "not_run",
            "reason": (
                "deterministic runner has no provider call; "
                "provider fault remains covered by focused provider tests"
            ),
        },
        {
            "scenario": "worker_termination",
            "status": "not_run",
            "reason": "subprocess cluster intentionally does not expose child process handles",
        },
        {
            "scenario": "redis_short_outage",
            "status": "not_run",
            "reason": "requires an explicitly controlled external Redis process",
        },
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic RVA capacity, steady, and session-churn workloads.")
    parser.add_argument("mode", choices=("capacity", "steady", "churn"))
    parser.add_argument("--profile", choices=("wss-opus/1", "udp-opus-gcm/1"), default="wss-opus/1")
    parser.add_argument("--steps", type=parse_steps, default=DEFAULT_STEPS)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--worker-max-sessions", type=int, default=5)
    parser.add_argument("--worker-count", type=int)
    parser.add_argument("--duration-seconds", type=float, default=10.0)
    parser.add_argument("--warmup-seconds", type=float, default=30.0)
    parser.add_argument("--measurement-seconds", type=float, default=150.0)
    parser.add_argument("--ramp-per-second", type=float, default=50.0)
    parser.add_argument("--device-prefix", default="steady")
    parser.add_argument("--seed", type=int, default=20260805)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path, default=PRODUCT_ROOT / ".artifacts" / "capacity-soak")
    parser.add_argument("--redis-url")
    parser.add_argument("--director-url", help="Use an already running deterministic benchmark cluster.")
    parser.add_argument("--execute", action="store_true", help="Required for 30 minute and 2 hour churn runs.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profile = MediaProfile(args.profile)
    if args.mode == "churn" and not args.execute:
        summary = {
            "status": "not_run",
            "evidence_scope": "session_churn",
            "reason": "churn requires --execute; 30 minute and 2 hour evidence uses 1800 or 7200 seconds",
            "requested_duration_seconds": args.duration_seconds,
            "continuous_session_soak": {
                "status": "not_run",
                "reason": "continuous-session runner is not implemented",
            },
            "fault_scenarios": not_run_faults(),
        }
        with JsonlRecorder(args.raw) as recorder:
            recorder.emit("churn.not_run", **summary)
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 2

    if args.worker_count is not None and args.worker_count < 1:
        raise ValueError("worker_count must be positive")
    steady_options = (
        SteadyOptions(
            args.warmup_seconds,
            args.measurement_seconds,
            args.ramp_per_second,
            args.device_prefix,
        )
        if args.mode == "steady"
        else None
    )
    scenarios = (
        [
            Scenario(
                step,
                (
                    args.warmup_seconds + args.measurement_seconds
                    if args.mode == "steady"
                    else args.duration_seconds
                ),
                args.seed + index,
                profile,
                args.worker_max_sessions,
                require_observable_overlap=args.mode == "capacity",
                worker_count_override=args.worker_count,
            )
            for index, step in enumerate(args.steps)
        ]
        if args.mode in {"capacity", "steady"}
        else [
            Scenario(
                args.concurrency,
                args.duration_seconds,
                args.seed,
                profile,
                args.worker_max_sessions,
                worker_count_override=args.worker_count,
            )
        ]
    )
    if args.director_url and args.redis_url:
        raise ValueError("--redis-url cannot be combined with --director-url")
    if not args.director_url:
        args.temp_root.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    with JsonlRecorder(args.raw) as recorder:
        for index, scenario in enumerate(scenarios):
            try:
                if args.director_url:
                    result = run_external_scenario(
                        scenario,
                        recorder,
                        director_url=args.director_url,
                        steady_options=steady_options,
                    )
                else:
                    scenario_root = args.temp_root / f"{args.mode}-{index}-{uuid.uuid4().hex}"
                    scenario_root.mkdir(parents=True)
                    if steady_options is not None:
                        result = run_local_steady_scenario(
                            scenario,
                            steady_options,
                            recorder,
                            temp_root=scenario_root,
                            redis_url=args.redis_url,
                        )
                    else:
                        result = run_local_scenario(
                            scenario,
                            recorder,
                            temp_root=scenario_root,
                            redis_url=args.redis_url,
                        )
            except HarnessInfrastructureError as failure:
                result = infrastructure_failure_summary(scenario, failure)
                recorder.emit("scenario.infrastructure_failed", summary=result)
            summaries.append(result)
    output = {
        "status": (
            "failed"
            if any(item["status"] == "failed" for item in summaries)
            else "inconclusive"
            if any(item["status"] == "inconclusive" for item in summaries)
            else "measured"
        ),
        "evidence_scope": (
            "steady_provider_free_uplink_and_initial_downlink"
            if args.mode == "steady"
            else "session_churn"
        ),
        "continuous_session_soak": {
            "status": "measured" if args.mode == "steady" else "not_run",
            "reason": (
                "steady mode held the same sessions for the configured warmup and measurement window"
                if args.mode == "steady"
                else "this runner repeats short sessions rather than keeping one session continuously open"
            ),
        },
        "mode": args.mode,
        "seed": args.seed,
        "results": summaries,
        "fault_scenarios": [
            *(fault for result in summaries for fault in result.get("fault_scenarios", [])),
            *not_run_faults(),
        ],
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if output["status"] == "measured" else 1


if __name__ == "__main__":
    raise SystemExit(main())

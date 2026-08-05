from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = PRODUCT_ROOT / "server" / "tools" / "capacity_soak.py"
SPEC = importlib.util.spec_from_file_location("capacity_soak", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capacity_soak = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capacity_soak
SPEC.loader.exec_module(capacity_soak)


def test_capacity_steps_and_churn_guard(tmp_path: Path) -> None:
    assert capacity_soak.parse_steps("1,5,10") == (1, 5, 10)
    with pytest.raises(Exception, match="unique positive"):
        capacity_soak.parse_steps("1,1")

    summary = tmp_path / "summary.json"
    exit_code = capacity_soak.main(
        [
            "churn",
            "--duration-seconds",
            "10",
            "--raw",
            str(tmp_path / "raw.jsonl"),
            "--summary",
            str(summary),
        ]
    )
    assert exit_code == 2
    report = json.loads(summary.read_text(encoding="utf-8"))
    assert report["status"] == "not_run"
    assert {item["status"] for item in report["fault_scenarios"]} == {"not_run"}
    assert report["continuous_session_soak"]["status"] == "not_run"
    assert json.loads((tmp_path / "raw.jsonl").read_text(encoding="utf-8"))["event"] == "churn.not_run"


@pytest.mark.e2e_host
@pytest.mark.parametrize(
    ("profile", "concurrency", "worker_max_sessions"),
    [
        ("wss-opus/1", 1, 5),
        ("udp-opus-gcm/1", 1, 5),
        ("wss-opus/1", 5, 2),
        ("udp-opus-gcm/1", 5, 2),
    ],
)
def test_short_capacity_round_trip_releases_all_sessions(
    tmp_path: Path,
    profile: str,
    concurrency: int,
    worker_max_sessions: int,
) -> None:
    raw = tmp_path / "raw.jsonl"
    summary = tmp_path / "summary.json"
    exit_code = capacity_soak.main(
        [
            "capacity",
            "--steps",
            str(concurrency),
            "--profile",
            profile,
            "--duration-seconds",
            "0.01",
            "--worker-max-sessions",
            str(worker_max_sessions),
            "--seed",
            "7",
            "--raw",
            str(raw),
            "--summary",
            str(summary),
            "--temp-root",
            str(tmp_path / "cluster"),
        ]
    )
    assert exit_code == 0
    report = json.loads(summary.read_text(encoding="utf-8"))
    result = report["results"][0]
    assert result["status"] == "measured"
    assert result["sessions_attempted"] == concurrency
    assert result["sessions_succeeded"] == concurrency
    assert result["route_reacquired_and_release_request_accepted_count"] == concurrency
    assert result["target_concurrency_observed"] is True
    assert result["active_sessions_peak_total"] >= concurrency
    assert max(result["worker_active_sessions_peak"].values()) <= worker_max_sessions
    assert set(result["worker_active_sessions_final"].values()) == {0}
    assert result["process_and_port_reclamation"] == "measured"
    events = [json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines()]
    expected_events = [
        "scenario.started",
        *["session.completed"] * concurrency,
        *["route_reacquired_and_release_request_accepted.verified"] * concurrency,
        "scenario.finished",
    ]
    if concurrency > worker_max_sessions:
        expected_events.append("fault.worker_drain.measured")
        assert report["fault_scenarios"][0]["status"] == "measured"
    assert [event["event"] for event in events] == expected_events


@pytest.mark.parametrize(
    ("uplink", "playback", "completed"),
    [(0, 4, 1), (1, 3, 1), (1, 4, 0), (1, 4, 2)],
)
def test_media_completion_rejects_incomplete_success(
    uplink: int,
    playback: int,
    completed: int,
) -> None:
    assert capacity_soak.media_completion_error(
        uplink_frames=uplink,
        playback_frames=playback,
        completed_playbacks=completed,
    ) == "media_incomplete"


def test_media_completion_accepts_full_fixture_round_trip() -> None:
    assert capacity_soak.media_completion_error(
        uplink_frames=1,
        playback_frames=4,
        completed_playbacks=1,
    ) is None


def _event(kind: str, *, outcome: str | None = None, target: object | None = None) -> SimpleNamespace:
    message = {} if outcome is None else {"outcome": outcome}
    return SimpleNamespace(kind=kind, message=message, target=target)


def test_deterministic_event_completion_accepts_exact_completed_closure() -> None:
    target = object()
    events = [
        _event("session.opened"),
        _event("response.begin", target=target),
        *[_event("media.audio", target=target) for _ in range(4)],
        _event("response.end", outcome="completed", target=target),
    ]
    assert capacity_soak.deterministic_event_completion_error(events) is None


@pytest.mark.parametrize("outcome", ["cancelled", "failed", "stopped"])
def test_deterministic_event_completion_rejects_non_completed_terminal(outcome: str) -> None:
    target = object()
    events = [
        _event("session.opened"),
        _event("response.begin", target=target),
        *[_event("media.audio", target=target) for _ in range(4)],
        _event("response.end", outcome=outcome, target=target),
    ]
    assert capacity_soak.deterministic_event_completion_error(events) == "event_terminal_not_completed"


def test_deterministic_event_completion_rejects_missing_media_and_mismatched_target() -> None:
    target = object()
    incomplete = [
        _event("session.opened"),
        _event("response.begin", target=target),
        *[_event("media.audio", target=target) for _ in range(3)],
        _event("response.end", outcome="completed", target=target),
    ]
    assert capacity_soak.deterministic_event_completion_error(incomplete) == "event_closure_incomplete"

    mismatched = [
        _event("session.opened"),
        _event("response.begin", target=target),
        *[_event("media.audio", target=target) for _ in range(4)],
        _event("response.end", outcome="completed", target=object()),
    ]
    assert capacity_soak.deterministic_event_completion_error(mismatched) == "event_closure_target_mismatch"


def test_workers_over_capacity_reports_only_excess_peaks() -> None:
    excess = capacity_soak.workers_over_capacity(
        {"worker-a": 5, "worker-b": 6}, worker_max_sessions=5
    )
    assert excess == {"worker-b": 6}
    assert capacity_soak.classify_scenario_status(
        has_session_failures=False,
        has_route_failures=False,
        concurrency_observed=True,
        capacity_excess=excess,
    ) == "failed"


def test_wait_workers_idle_retries_transient_timeout_then_returns_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def transient_then_idle(_director_url: str) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("transient registry timeout")
        return [{"worker_id": "worker-1", "active_sessions": 0}]

    monkeypatch.setattr(capacity_soak, "_workers", transient_then_idle)
    result = asyncio.run(capacity_soak._wait_workers_idle("http://director.test", timeout=0.5))
    assert result == [{"worker_id": "worker-1", "active_sessions": 0}]
    assert calls == 2


def test_wait_workers_idle_reports_last_transport_error_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def always_timeout(_director_url: str) -> list[dict[str, object]]:
        raise httpx.ReadTimeout("persistent registry timeout")

    monkeypatch.setattr(capacity_soak, "_workers", always_timeout)
    with pytest.raises(capacity_soak.HarnessInfrastructureError) as caught:
        asyncio.run(capacity_soak._wait_workers_idle("http://director.test", timeout=0.02))
    assert caught.value.stage == "worker_idle_wait"
    assert caught.value.exception_type == "ReadTimeout"


def test_wait_workers_idle_reports_timeout_when_workers_remain_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def active_worker(_director_url: str) -> list[dict[str, object]]:
        return [{"worker_id": "worker-1", "active_sessions": 1}]

    monkeypatch.setattr(capacity_soak, "_workers", active_worker)
    with pytest.raises(capacity_soak.HarnessInfrastructureError) as caught:
        asyncio.run(capacity_soak._wait_workers_idle("http://director.test", timeout=0.02))
    assert caught.value.exception_type == "TimeoutError"


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_wait_workers_idle_retries_transient_http_status_then_returns_idle(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    calls = 0
    request = httpx.Request("GET", "http://director.test/internal/v1/workers")
    response = httpx.Response(status_code, request=request)
    failure = httpx.HTTPStatusError("service unavailable", request=request, response=response)

    async def invalid_status(_director_url: str) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return [{"worker_id": "worker-1", "active_sessions": 0}]

    monkeypatch.setattr(capacity_soak, "_workers", invalid_status)
    result = asyncio.run(capacity_soak._wait_workers_idle("http://director.test", timeout=0.5))
    assert result == [{"worker_id": "worker-1", "active_sessions": 0}]
    assert calls == 2


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_wait_workers_idle_reports_persistent_retryable_http_status_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    request = httpx.Request("GET", "http://director.test/internal/v1/workers")
    response = httpx.Response(status_code, request=request)
    failure = httpx.HTTPStatusError("service unavailable", request=request, response=response)

    async def invalid_status(_director_url: str) -> list[dict[str, object]]:
        raise failure

    monkeypatch.setattr(capacity_soak, "_workers", invalid_status)
    with pytest.raises(capacity_soak.HarnessInfrastructureError) as caught:
        asyncio.run(capacity_soak._wait_workers_idle("http://director.test", timeout=0.02))
    assert caught.value.exception_type == "HTTPStatusError"


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_wait_workers_idle_fails_fast_for_permanent_http_status(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    calls = 0
    request = httpx.Request("GET", "http://director.test/internal/v1/workers")
    response = httpx.Response(status_code, request=request)
    failure = httpx.HTTPStatusError("permanent registry failure", request=request, response=response)

    async def permanent_failure(_director_url: str) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(capacity_soak, "_workers", permanent_failure)
    with pytest.raises(capacity_soak.HarnessInfrastructureError) as caught:
        asyncio.run(capacity_soak._wait_workers_idle("http://director.test", timeout=0.5))
    assert caught.value.exception_type == "HTTPStatusError"
    assert calls == 1


def test_wait_workers_idle_enforces_hard_deadline_before_late_idle_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = False

    async def late_idle(_director_url: str) -> list[dict[str, object]]:
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return [{"worker_id": "worker-1", "active_sessions": 0}]

    monkeypatch.setattr(capacity_soak, "_workers", late_idle)
    with pytest.raises(capacity_soak.HarnessInfrastructureError) as caught:
        asyncio.run(capacity_soak._wait_workers_idle("http://director.test", timeout=0.02))
    assert caught.value.exception_type == "TimeoutError"
    assert cancelled is True


@pytest.mark.parametrize("failure", [ValueError("invalid worker payload"), TypeError("invalid worker payload")])
def test_wait_workers_idle_does_not_retry_invalid_registry_payload(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    calls = 0

    async def invalid_payload(_director_url: str) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(capacity_soak, "_workers", invalid_payload)
    with pytest.raises(capacity_soak.HarnessInfrastructureError) as caught:
        asyncio.run(capacity_soak._wait_workers_idle("http://director.test", timeout=0.5))
    assert caught.value.exception_type == type(failure).__name__
    assert calls == 1


def test_route_request_retries_transport_error_within_shared_deadline() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadError("transient route read failure", request=request)
        return httpx.Response(200, request=request, json={"ok": True})

    async def exercise() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await capacity_soak._post_route_request_with_retry(
                client,
                "http://director.test/v1/session/bootstrap",
                headers={},
                payload={},
                deadline=capacity_soak.time.monotonic() + 0.5,
            )

    assert asyncio.run(exercise()).status_code == 200
    assert calls == 2


def test_route_request_stops_retrying_at_hard_deadline() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadError("persistent route read failure", request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await capacity_soak._post_route_request_with_retry(
                client,
                "http://director.test/v1/session/bootstrap",
                headers={},
                payload={},
                deadline=capacity_soak.time.monotonic() + 0.02,
            )

    with pytest.raises(httpx.ReadError):
        asyncio.run(exercise())
    assert calls >= 1


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_route_request_retries_only_retryable_http_status(status_code: int) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code if calls == 1 else 200, request=request)

    async def exercise() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await capacity_soak._post_route_request_with_retry(
                client,
                "http://director.test/v1/session/bootstrap",
                headers={},
                payload={},
                deadline=capacity_soak.time.monotonic() + 0.5,
            )

    assert asyncio.run(exercise()).status_code == 200
    assert calls == 2


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_route_request_fails_fast_for_permanent_http_status(status_code: int) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await capacity_soak._post_route_request_with_retry(
                client,
                "http://director.test/v1/session/bootstrap",
                headers={},
                payload={},
                deadline=capacity_soak.time.monotonic() + 0.5,
            )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(exercise())
    assert calls == 1


def test_route_request_rejects_unexpected_success_status() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(204, request=request))
        ) as client:
            await capacity_soak._post_route_request_with_retry(
                client,
                "http://director.test/v1/session/bootstrap",
                headers={},
                payload={},
                deadline=capacity_soak.time.monotonic() + 0.5,
            )

    with pytest.raises(ValueError, match="unexpected status 204"):
        asyncio.run(exercise())


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"worker_id": "worker", "session_epoch": "", "fencing_token": 1},
        {"worker_id": "worker", "session_epoch": "epoch", "fencing_token": 0},
    ],
)
def test_route_payload_validation_fails_fast(payload: object) -> None:
    with pytest.raises(ValueError, match="invalid route payload"):
        capacity_soak._validate_route_payload(payload)


@pytest.mark.parametrize("stage", ["worker_idle_wait", "route_verification", "worker_drain"])
def test_cli_writes_failed_summary_for_infrastructure_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    def fail_scenario(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise capacity_soak.HarnessInfrastructureError(stage, TimeoutError())

    monkeypatch.setattr(capacity_soak, "run_local_scenario", fail_scenario)
    raw = tmp_path / "raw.jsonl"
    summary = tmp_path / "summary.json"
    exit_code = capacity_soak.main(
        [
            "capacity",
            "--steps",
            "1",
            "--duration-seconds",
            "0.01",
            "--raw",
            str(raw),
            "--summary",
            str(summary),
            "--temp-root",
            str(tmp_path / "cluster"),
        ]
    )

    assert exit_code == 1
    report = json.loads(summary.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    failure = report["results"][0]["infrastructure_failure"]
    assert failure == {"stage": stage, "exception_type": "TimeoutError"}
    raw_events = [json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines()]
    assert raw_events[-1]["event"] == "scenario.infrastructure_failed"
    assert raw_events[-1]["summary"]["status"] == "failed"

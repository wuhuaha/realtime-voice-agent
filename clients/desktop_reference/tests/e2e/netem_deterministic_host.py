from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import httpx
import pytest
from voice_testkit.subprocess_cluster import running_process_cluster

from rva_desktop.config import MediaProfile
from rva_desktop.errors import TransportError

E2E_DIR = Path(__file__).resolve().parent
TOOLS_DIR = E2E_DIR.parents[1] / "tools"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


baseline = _load_module("deterministic_host_baseline", E2E_DIR / "test_deterministic_host.py")
netem = _load_module("netem_harness_e2e", TOOLS_DIR / "netem_harness.py")


def _profiles(value: str) -> tuple[MediaProfile, ...]:
    supported = {profile.value: profile for profile in MediaProfile}
    requested = tuple(part.strip() for part in value.split(",") if part.strip())
    unknown = sorted(set(requested) - supported.keys())
    if not requested or unknown or len(requested) != len(set(requested)):
        raise ValueError(f"invalid netem profiles: {value}")
    return tuple(supported[name] for name in requested)


def _wait_for_zero_active_sessions(worker_url: str, *, timeout: float = 5.0) -> int | None:
    deadline = time.monotonic() + timeout
    last_value: int | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{worker_url}/health/ready", timeout=1.0)
            payload = response.json()
            last_value = int(payload["active_sessions"])
            if last_value == 0:
                return 0
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            pass
        time.sleep(0.1)
    return last_value


def _verify_route_reacquired_and_release_request_accepted(
    director_url: str,
    profile: MediaProfile,
) -> bool:
    headers = {"Authorization": f"Bearer {baseline.BOOTSTRAP_TOKEN}"}
    request = {
        "tenant_id": "desktop-e2e",
        "device_id": f"desktop-e2e-{profile.value.replace('/', '-')}",
        "supported_profiles": [profile.value],
        "control_protocol": "rva/1",
    }
    try:
        with httpx.Client(base_url=director_url, timeout=3.0) as client:
            first_opened = client.post("/v1/session/bootstrap", headers=headers, json=request)
            if first_opened.status_code != 200:
                return False
            first_route = first_opened.json()
            first_release = client.post(
                "/v1/session/release",
                headers=headers,
                json={
                    "tenant_id": request["tenant_id"],
                    "device_id": request["device_id"],
                    "worker_id": first_route["worker_id"],
                    "session_epoch": first_route["session_epoch"],
                    "fencing_token": first_route["fencing_token"],
                },
            )
            if first_release.status_code != 200:
                return False
            second_opened = client.post("/v1/session/bootstrap", headers=headers, json=request)
            if second_opened.status_code != 200:
                return False
            second_route = second_opened.json()
            route_advanced = (
                second_route["session_epoch"] != first_route["session_epoch"]
                and second_route["fencing_token"] > first_route["fencing_token"]
            )
            second_release = client.post(
                "/v1/session/release",
                headers=headers,
                json={
                    "tenant_id": request["tenant_id"],
                    "device_id": request["device_id"],
                    "worker_id": second_route["worker_id"],
                    "session_epoch": second_route["session_epoch"],
                    "fencing_token": second_route["fencing_token"],
                },
            )
            return route_advanced and second_release.status_code == 200
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return False


@pytest.mark.e2e_host
def test_isolated_netem_matrix(tmp_path: Path) -> None:
    if os.environ.get("RVA_NETEM_NAMESPACE") != "1":
        pytest.skip("run through tools/run-netem.sh inside an isolated Linux network namespace")

    scenarios = netem.select_scenarios(os.environ.get("RVA_NETEM_SCENARIOS", "clean"))
    profiles = _profiles(os.environ.get("RVA_NETEM_PROFILES", "wss-opus/1,udp-opus-gcm/1"))
    repeats = int(os.environ.get("RVA_NETEM_REPEATS", "1"))
    base_seed = int(os.environ.get("RVA_NETEM_SEED", "20260805"))
    netem.validate_run_parameters(repeats=repeats, seed=base_seed)
    output = Path(os.environ.get("RVA_NETEM_OUTPUT", str(tmp_path / "netem-results"))).resolve()
    controller = netem.NetemController()
    results = []

    for scenario_index, scenario in enumerate(scenarios):
        for profile in profiles:
            for repeat in range(repeats):
                seed = netem.derive_seed(
                    base_seed,
                    scenario_index=scenario_index,
                    repeat=repeat,
                )
                run_path = tmp_path / scenario.name / profile.value.replace("/", "-") / str(repeat)
                run_path.mkdir(parents=True)
                udp_enabled = profile is MediaProfile.UDP_OPUS_GCM_V1
                expected = "completed"
                observed = "failed"
                error_type = None
                error_code = None
                error_detail = None
                worker_active_sessions_after = None
                route_reacquired_and_release_request_accepted = False
                tc_qdisc_statistics = ""
                tc_filter_statistics = ""
                tc_profile_handle = netem.profile_netem_handle(scenario, profile=profile.value)
                tc_profile_counters = None
                tc_evidence_required = tc_profile_handle is not None
                attempt_duration_ms = 0.0
                tc_seed_control = "not_applicable"
                paired_randomness = False
                exercise_outcome = None
                try:
                    with running_process_cluster(
                        run_path,
                        worker_count=1,
                        udp_enabled=udp_enabled,
                        python_executable=baseline._server_python(),
                        internal_token=baseline.INTERNAL_TOKEN,
                        bootstrap_token=baseline.BOOTSTRAP_TOKEN,
                        grant_signing_key=baseline.GRANT_SIGNING_KEY,
                        lab_token=baseline.LAB_TOKEN,
                    ) as cluster:
                        worker = cluster.workers[0]
                        tc_seed_control, paired_randomness = controller.apply(
                            scenario,
                            tcp_ports=[worker.http_port],
                            udp_ports=[worker.udp_port],
                            seed=seed,
                        )
                        started = time.monotonic()
                        try:
                            exercise = (
                                baseline._exercise_netem_desktop_app_profile
                                if netem.use_composition_root(scenario, profile=profile.value)
                                else baseline._exercise_profile
                            )
                            exercise_result = asyncio.run(exercise(cluster.director_url, profile))
                            exercise_outcome = getattr(exercise_result, "outcome", "completed")
                        except Exception as exc:
                            error_type = type(exc).__name__
                            frames = traceback.extract_tb(exc.__traceback__)
                            location = frames[-1] if frames else None
                            error_detail = (
                                f"{location.name}:{location.lineno}: {exc!r}"
                                if location is not None
                                else repr(exc)
                            )
                            if isinstance(exc, TransportError):
                                error_code = exc.code
                        attempt_duration_ms = round((time.monotonic() - started) * 1_000, 3)
                        tc_qdisc_statistics, tc_filter_statistics = controller.statistics()
                        if tc_profile_handle is not None:
                            tc_profile_counters = netem.netem_counters(
                                tc_qdisc_statistics,
                                handle=tc_profile_handle,
                            )
                        worker_active_sessions_after = _wait_for_zero_active_sessions(
                            f"http://127.0.0.1:{worker.http_port}"
                        )
                        if worker_active_sessions_after == 0:
                            route_reacquired_and_release_request_accepted = (
                                _verify_route_reacquired_and_release_request_accepted(
                                    cluster.director_url,
                                    profile,
                                )
                            )
                except Exception as exc:  # The raw record preserves bounded matrix failures for later comparison.
                    error_type = type(exc).__name__
                    frames = traceback.extract_tb(exc.__traceback__)
                    location = frames[-1] if frames else None
                    error_detail = (
                        f"{location.name}:{location.lineno}: {exc!r}"
                        if location is not None
                        else repr(exc)
                    )
                    error_code = getattr(exc, "code", None) if isinstance(exc, TransportError) else None
                finally:
                    controller.reset()
                expected, observed, expectation_met = netem.evaluate_attempt(
                    scenario,
                    profile=profile.value,
                    error_type=error_type,
                    error_code=error_code,
                    worker_active_sessions_after=worker_active_sessions_after,
                    route_reacquired_and_release_request_accepted=(
                        route_reacquired_and_release_request_accepted
                    ),
                    tc_evidence_required=tc_evidence_required,
                    tc_rules_hit=(
                        tc_profile_counters["admitted"] > 0 if tc_profile_counters is not None else None
                    ),
                    tc_dropped=(
                        tc_profile_counters["dropped"] if tc_profile_counters is not None else None
                    ),
                    exercise_outcome=exercise_outcome,
                )
                results.append(
                    netem.AttemptResult(
                        scenario=scenario.name,
                        profile=profile.value,
                        repeat=repeat,
                        seed=seed,
                        pair_id=netem.pair_id(scenario=scenario.name, repeat=repeat),
                        expected=expected,
                        observed=observed,
                        expectation_met=expectation_met,
                        attempt_duration_ms=attempt_duration_ms,
                        session_completion_latency_ms=(
                            attempt_duration_ms if observed == "completed" else None
                        ),
                        error_type=error_type,
                        error_code=error_code,
                        error_detail=error_detail,
                        worker_active_sessions_after=worker_active_sessions_after,
                        route_reacquired_and_release_request_accepted=(
                            route_reacquired_and_release_request_accepted
                        ),
                        tc_evidence_required=tc_evidence_required,
                        tc_rules_hit=(
                            tc_profile_counters["admitted"] > 0
                            if tc_profile_counters is not None
                            else None
                        ),
                        tc_profile_handle=tc_profile_handle,
                        tc_profile_counters=tc_profile_counters,
                        impairment_observation=netem.classify_impairment_observation(
                            scenario,
                            tc_profile_counters,
                        ),
                        tc_seed_control=tc_seed_control,
                        seed_applied=tc_seed_control == "applied",
                        paired_randomness=paired_randomness,
                        tc_qdisc_statistics=tc_qdisc_statistics,
                        tc_filter_statistics=tc_filter_statistics,
                    )
                )

    raw_path = output / "raw.jsonl"
    aggregate_path = output / "aggregate.json"
    netem.write_results(results, raw_path=raw_path, aggregate_path=aggregate_path)
    aggregate = netem.aggregate_results(results)
    assert aggregate["all_expectations_met"], f"netem expectations failed; inspect {raw_path}"

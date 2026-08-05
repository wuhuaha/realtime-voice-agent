from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "netem_harness.py"
SPEC = importlib.util.spec_from_file_location("netem_harness_unit", TOOLS)
assert SPEC is not None and SPEC.loader is not None
netem = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = netem
SPEC.loader.exec_module(netem)


def test_scenario_selection_is_explicit_and_rejects_unknown_or_duplicates() -> None:
    assert [item.name for item in netem.select_scenarios("clean, jitter,udp-blocked")] == [
        "clean",
        "jitter",
        "udp-blocked",
    ]
    with pytest.raises(ValueError, match="unknown"):
        netem.select_scenarios("clean,typo")
    with pytest.raises(ValueError, match="must not be repeated"):
        netem.select_scenarios("clean,clean")


def test_loss_matrix_and_reorder_cadence_are_preregistered() -> None:
    assert [netem.SCENARIOS[f"random-loss-{percent}"].arguments for percent in (1, 3, 5)] == [
        ("loss", "random", "1%"),
        ("loss", "random", "3%"),
        ("loss", "random", "5%"),
    ]
    assert netem.SCENARIOS["burst-loss"].arguments == ("loss", "random", "3%", "60%")
    reorder = netem.SCENARIOS["reorder"].arguments
    assert reorder[:3] == ("delay", "100ms", "20ms")
    assert int(reorder[1].removesuffix("ms")) >= 80


def test_tc_commands_target_only_supplied_dynamic_worker_ports() -> None:
    commands = netem.tc_setup_commands(
        netem.SCENARIOS["delay"],
        interface="lo",
        tcp_ports=[31_001],
        udp_ports=[31_002],
        seed=17,
    )
    rendered = [" ".join(command) for command in commands]
    assert sum("netem delay 80ms" in command for command in rendered) == 2
    assert all(" seed " not in f" {command} " for command in rendered)
    assert any("parent 1:3 handle 30:" in command for command in rendered)
    assert any("parent 1:4 handle 40:" in command for command in rendered)
    assert sum(" 31001 " in f" {command} " for command in rendered) == 2
    assert sum(" 31002 " in f" {command} " for command in rendered) == 2
    assert all(" 31000 " not in f" {command} " for command in rendered)


def test_udp_blocked_does_not_shape_worker_tcp() -> None:
    commands = netem.tc_setup_commands(
        netem.SCENARIOS["udp-blocked"],
        interface="lo",
        tcp_ports=[31_001],
        udp_ports=[31_002],
        seed=19,
    )
    rendered = "\n".join(" ".join(command) for command in commands)
    assert " 31001 " not in f" {rendered} "
    assert rendered.count(" 31002 ") == 2
    assert "parent 1:4 handle 40: netem loss 100%" in rendered
    assert " seed " not in f" {rendered} "


def test_only_udp_blocked_uses_low_level_probe() -> None:
    for name, scenario in netem.SCENARIOS.items():
        assert netem.use_composition_root(scenario, profile="wss-opus/1") is True
        assert netem.use_composition_root(scenario, profile="udp-opus-gcm/1") is (name != "udp-blocked")


def test_random_netem_commands_can_be_rendered_with_or_without_seed() -> None:
    scenario = netem.SCENARIOS["random-loss-3"]
    seeded = netem.tc_setup_commands(
        scenario,
        interface="lo",
        tcp_ports=[31_001],
        udp_ports=[],
        seed=23,
    )
    fallback = netem.tc_setup_commands(
        scenario,
        interface="lo",
        tcp_ports=[31_001],
        udp_ports=[],
        seed=23,
        apply_seed=False,
    )
    assert any(command[-2:] == ("seed", "23") for command in seeded)
    assert all("seed" not in command for command in fallback)


def test_aggregate_reports_observations_without_inventing_transport_metrics(tmp_path: Path) -> None:
    results = [
        netem.AttemptResult(
            "clean", "wss-opus/1", 0, 20, "clean:0", "completed", "completed", True, 10.0, 10.0
        ),
        netem.AttemptResult(
            "clean", "wss-opus/1", 1, 21, "clean:1", "completed", "completed", True, 30.0, 30.0
        ),
        netem.AttemptResult(
            "udp-blocked",
            "udp-opus-gcm/1",
            0,
            22,
            "udp-blocked:0",
            "blocked_or_failed",
            "blocked_or_failed",
            True,
            50.0,
            None,
            "TimeoutError",
        ),
    ]
    aggregate = netem.aggregate_results(results)
    assert aggregate["evidence_scope"] == "completion_and_bounded_recovery"
    assert aggregate["environment"] == "isolated_loopback_netns"
    assert aggregate["comparison_limit"] == "not_transport_performance_evidence"
    assert aggregate["all_expectations_met"] is True
    assert aggregate["groups"][0]["session_completion_latency_ms"] == {
        "min": 10.0,
        "p50": 10.0,
        "p95": 30.0,
        "max": 30.0,
    }
    assert "packet_loss" not in aggregate["groups"][0]

    netem.write_results(results, raw_path=tmp_path / "raw.jsonl", aggregate_path=tmp_path / "aggregate.json")
    assert len((tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    assert (tmp_path / "aggregate.json").read_text(encoding="utf-8").endswith("\n")


def test_aggregate_preserves_failed_measurement_as_observation() -> None:
    result = netem.AttemptResult(
        "random-loss-3",
        "udp-opus-gcm/1",
        0,
        22,
        "random-loss-3:0",
        "completed",
        "blocked_or_failed",
        False,
        50.0,
        None,
        "TimeoutError",
    )
    aggregate = netem.aggregate_results([result])
    assert aggregate["all_expectations_met"] is False
    assert aggregate["groups"][0]["completed"] == 0
    assert aggregate["groups"][0]["blocked_or_failed"] == 1
    assert aggregate["groups"][0]["session_completion_latency_ms"] == {
        "min": None,
        "p50": None,
        "p95": None,
        "max": None,
    }


def test_impaired_scenario_failure_is_not_treated_as_measurement_success() -> None:
    expected, observed, met = netem.evaluate_attempt(
        netem.SCENARIOS["random-loss-3"],
        profile="udp-opus-gcm/1",
        error_type="TimeoutError",
        error_code=None,
        worker_active_sessions_after=None,
        route_reacquired_and_release_request_accepted=False,
        tc_evidence_required=True,
        tc_rules_hit=False,
    )
    assert (expected, observed, met) == ("completed_and_cleanup_verified", "failed", False)


@pytest.mark.parametrize(
    ("error_type", "error_code", "active_sessions", "met"),
    [
        ("TransportError", "udp_probe_timeout", 0, True),
        ("TransportError", "udp_probe_timeout", 1, False),
        ("TimeoutError", None, 0, False),
        (None, None, 0, False),
    ],
)
def test_udp_blocked_requires_exact_probe_failure_and_zero_sessions(
    error_type: str | None,
    error_code: str | None,
    active_sessions: int,
    met: bool,
) -> None:
    expected, _, actual = netem.evaluate_attempt(
        netem.SCENARIOS["udp-blocked"],
        profile="udp-opus-gcm/1",
        error_type=error_type,
        error_code=error_code,
        worker_active_sessions_after=active_sessions,
        route_reacquired_and_release_request_accepted=True,
        tc_evidence_required=True,
        tc_rules_hit=True,
    )
    assert expected == "udp_probe_timeout_and_cleanup_verified"
    assert actual is met


@pytest.mark.parametrize(("repeats", "seed"), [(0, 1), (51, 1), (1, 0), (1, 2_147_483_648)])
def test_run_parameter_bounds(repeats: int, seed: int) -> None:
    with pytest.raises(ValueError):
        netem.validate_run_parameters(repeats=repeats, seed=seed)


def test_derived_seed_is_stable_and_wraps_within_tc_bounds() -> None:
    assert netem.derive_seed(100, scenario_index=2, repeat=3) == 20_103
    assert netem.derive_seed(100, scenario_index=2, repeat=3) == netem.derive_seed(
        100, scenario_index=2, repeat=3
    )
    assert netem.derive_seed(netem.MAX_SEED, scenario_index=0, repeat=1) == 1
    with pytest.raises(ValueError, match="non-negative"):
        netem.derive_seed(1, scenario_index=-1, repeat=0)


def test_pair_identity_is_shared_by_profiles() -> None:
    assert netem.pair_id(scenario="jitter", repeat=4) == "jitter:4"


def test_netem_counters_parse_protocol_specific_handle() -> None:
    output = (
        "qdisc prio 1: root\n Sent 200 bytes 2 pkt\n"
        "qdisc netem 30: parent 1:3 limit 1000\n Sent 120 bytes 3 pkt (dropped 1)\n"
    )
    assert netem.netem_counters(output, handle="30") == {
        "sent": 3,
        "dropped": 1,
        "admitted": 4,
    }
    assert netem.netem_counters("qdisc prio 1: root\n Sent 0 bytes 0 pkt\n", handle="30") is None


def test_netem_admitted_packet_count_includes_full_loss_drops() -> None:
    output = (
        "qdisc netem 30: parent 1:3 limit 1000 loss 100%\n"
        " Sent 0 bytes 0 pkt (dropped 7, overlimits 0 requeues 0)\n"
    )
    assert netem.netem_counters(output, handle="30") == {
        "sent": 0,
        "dropped": 7,
        "overlimits": 0,
        "requeues": 0,
        "admitted": 7,
    }


def test_udp_profile_cannot_pass_from_tcp_handle_traffic_only() -> None:
    qdisc = (
        "qdisc netem 30: parent 1:3 limit 1000 delay 80ms\n Sent 200 bytes 2 pkt (dropped 0)\n"
        "qdisc netem 40: parent 1:4 limit 1000 delay 80ms\n Sent 0 bytes 0 pkt (dropped 0)\n"
    )
    udp = netem.netem_counters(qdisc, handle="40")
    assert udp is not None and udp["admitted"] == 0
    assert netem.evaluate_attempt(
        netem.SCENARIOS["delay"],
        profile="udp-opus-gcm/1",
        error_type=None,
        error_code=None,
        worker_active_sessions_after=0,
        route_reacquired_and_release_request_accepted=True,
        tc_evidence_required=True,
        tc_rules_hit=False,
    )[2] is False


def test_random_loss_without_effective_drop_is_separate_from_completion() -> None:
    assert netem.classify_impairment_observation(
        netem.SCENARIOS["random-loss-1"], {"sent": 5, "dropped": 0, "admitted": 5}
    ) == "no_impairment_observed"
    assert netem.classify_impairment_observation(
        netem.SCENARIOS["random-loss-1"], {"sent": 4, "dropped": 1, "admitted": 5}
    ) == "impairment_observed"


def test_managed_qdisc_detection_covers_tcp_and_udp_handles() -> None:
    assert netem.managed_netem_handles("qdisc netem 30:\nqdisc netem 40:\n") == {"30", "40"}


def test_strict_prerun_reset_rejects_stale_managed_qdisc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        netem.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=""),
    )
    monkeypatch.setattr(
        netem.NetemController,
        "_qdisc_statistics",
        lambda _self: "qdisc netem 40: parent 1:4\n Sent 0 bytes 0 pkt\n",
    )
    with pytest.raises(RuntimeError, match="remained after reset"):
        netem.NetemController().reset(require_clean=True)


def test_seed_unsupported_gets_one_strict_cleanup_and_unseeded_fallback() -> None:
    class FakeController(netem.NetemController):
        def __init__(self) -> None:
            super().__init__()
            self.resets: list[bool] = []
            self.command_batches: list[tuple[tuple[str, ...], ...]] = []

        def reset(self, *, require_clean: bool = False) -> None:
            self.resets.append(require_clean)

        def _run_commands(self, commands: tuple[tuple[str, ...], ...]) -> None:
            materialized = tuple(commands)
            self.command_batches.append(materialized)
            if len(self.command_batches) == 1:
                raise netem.subprocess.CalledProcessError(
                    1,
                    materialized[1],
                    stderr='What is "seed"?',
                )

    controller = FakeController()
    seed_control, paired = controller.apply(
        netem.SCENARIOS["jitter"],
        tcp_ports=[31_001],
        udp_ports=[],
        seed=29,
    )
    assert (seed_control, paired) == ("unavailable", False)
    assert controller.resets == [True, True]
    assert any("seed" in command for command in controller.command_batches[0])
    assert all("seed" not in command for command in controller.command_batches[1])

    controller.apply(
        netem.SCENARIOS["jitter"],
        tcp_ports=[31_001],
        udp_ports=[],
        seed=30,
    )
    assert all("seed" not in command for command in controller.command_batches[2])


def test_unavailable_seed_metadata_prevents_paired_comparison() -> None:
    result = netem.AttemptResult(
        "jitter",
        "wss-opus/1",
        0,
        20,
        "jitter:0",
        "completed_and_cleanup_verified",
        "completed",
        True,
        10.0,
        10.0,
        tc_seed_control="unavailable",
        seed_applied=False,
        paired_randomness=False,
    )
    aggregate = netem.aggregate_results([result])
    assert aggregate["tc_seed_control"] == "unavailable"
    assert aggregate["seed_applied"] is False
    assert aggregate["paired_randomness"] is False
    assert aggregate["comparison_limit"] == (
        "completion_and_bounded_recovery_unpaired_random_impairment"
    )


def test_udp_random_loss_accepts_only_verified_recovery_with_an_actual_drop() -> None:
    common = {
        "scenario": netem.SCENARIOS["random-loss-3"],
        "profile": "udp-opus-gcm/1",
        "error_type": None,
        "error_code": None,
        "worker_active_sessions_after": 0,
        "route_reacquired_and_release_request_accepted": True,
        "tc_evidence_required": True,
        "tc_rules_hit": True,
        "exercise_outcome": "bounded_recovery_verified",
    }
    expected, observed, met = netem.evaluate_attempt(**common, tc_dropped=1)
    assert expected == "completed_or_bounded_recovery_and_cleanup_verified"
    assert observed == "bounded_recovery_verified"
    assert met is True
    assert netem.evaluate_attempt(**common, tc_dropped=0)[2] is False

    recovered = netem.AttemptResult(
        "random-loss-3",
        "udp-opus-gcm/1",
        0,
        20,
        "random-loss-3:0",
        expected,
        observed,
        True,
        150.0,
        None,
    )
    group = netem.aggregate_results([recovered])["groups"][0]
    assert group["bounded_recovery_verified"] == 1
    assert group["blocked_or_failed"] == 0


@pytest.mark.parametrize("scenario", ["clean", "delay", "jitter", "reorder"])
def test_non_loss_scenarios_never_accept_bounded_recovery(scenario: str) -> None:
    assert netem.evaluate_attempt(
        netem.SCENARIOS[scenario],
        profile="udp-opus-gcm/1",
        error_type=None,
        error_code=None,
        worker_active_sessions_after=0,
        route_reacquired_and_release_request_accepted=True,
        tc_evidence_required=scenario != "clean",
        tc_rules_hit=True if scenario != "clean" else None,
        tc_dropped=0,
        exercise_outcome="bounded_recovery_verified",
    )[2] is False


def test_middle_plc_completion_remains_a_completed_attempt() -> None:
    expected, observed, met = netem.evaluate_attempt(
        netem.SCENARIOS["random-loss-5"],
        profile="udp-opus-gcm/1",
        error_type=None,
        error_code=None,
        worker_active_sessions_after=0,
        route_reacquired_and_release_request_accepted=True,
        tc_evidence_required=True,
        tc_rules_hit=True,
        tc_dropped=1,
        exercise_outcome="completed",
    )
    assert expected == "completed_or_bounded_recovery_and_cleanup_verified"
    assert (observed, met) == ("completed", True)


def test_success_requires_cleanup_route_and_applicable_tc_hit() -> None:
    scenario = netem.SCENARIOS["delay"]
    common = {
        "scenario": scenario,
        "profile": "wss-opus/1",
        "error_type": None,
        "error_code": None,
        "worker_active_sessions_after": 0,
        "route_reacquired_and_release_request_accepted": True,
        "tc_evidence_required": True,
    }
    assert netem.evaluate_attempt(**common, tc_rules_hit=True)[2] is True
    assert netem.evaluate_attempt(**common, tc_rules_hit=False)[2] is False
    assert netem.evaluate_attempt(
        **{**common, "worker_active_sessions_after": 1}, tc_rules_hit=True
    )[2] is False
    assert netem.evaluate_attempt(
        **{**common, "route_reacquired_and_release_request_accepted": False}, tc_rules_hit=True
    )[2] is False

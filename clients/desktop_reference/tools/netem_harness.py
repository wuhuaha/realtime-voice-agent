from __future__ import annotations

import json
import math
import re
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NetemScenario:
    name: str
    arguments: tuple[str, ...]
    udp_only: bool = False
    udp_expected_blocked: bool = False


SCENARIOS: dict[str, NetemScenario] = {
    "clean": NetemScenario("clean", ()),
    "delay": NetemScenario("delay", ("delay", "80ms")),
    "random-loss-1": NetemScenario("random-loss-1", ("loss", "random", "1%")),
    "random-loss-3": NetemScenario("random-loss-3", ("loss", "random", "3%")),
    "random-loss-5": NetemScenario("random-loss-5", ("loss", "random", "5%")),
    "burst-loss": NetemScenario("burst-loss", ("loss", "random", "3%", "60%")),
    "jitter": NetemScenario("jitter", ("delay", "80ms", "30ms", "distribution", "normal")),
    # 100 ms base delay makes immediate reordered packets cross the 60 ms Opus cadence.
    "reorder": NetemScenario("reorder", ("delay", "100ms", "20ms", "reorder", "25%", "50%")),
    "udp-blocked": NetemScenario(
        "udp-blocked",
        ("loss", "100%"),
        udp_only=True,
        udp_expected_blocked=True,
    ),
}

MAX_SEED = 2_147_483_647


@dataclass(frozen=True)
class AttemptResult:
    scenario: str
    profile: str
    repeat: int
    seed: int
    pair_id: str
    expected: str
    observed: str
    expectation_met: bool
    attempt_duration_ms: float
    session_completion_latency_ms: float | None
    error_type: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    worker_active_sessions_after: int | None = None
    route_reacquired_and_release_request_accepted: bool = False
    tc_evidence_required: bool = False
    tc_rules_hit: bool | None = None
    tc_profile_handle: str | None = None
    tc_profile_counters: dict[str, int] | None = None
    impairment_observation: str = "not_applicable"
    tc_seed_control: str = "not_applicable"
    seed_applied: bool = False
    paired_randomness: bool = False
    tc_qdisc_statistics: str = ""
    tc_filter_statistics: str = ""
    evidence_scope: str = "completion_and_bounded_recovery"


def select_scenarios(names: str) -> tuple[NetemScenario, ...]:
    requested = tuple(part.strip() for part in names.split(",") if part.strip())
    if not requested:
        raise ValueError("at least one netem scenario is required")
    unknown = sorted(set(requested) - SCENARIOS.keys())
    if unknown:
        raise ValueError(f"unknown netem scenario(s): {', '.join(unknown)}")
    if len(set(requested)) != len(requested):
        raise ValueError("netem scenarios must not be repeated")
    return tuple(SCENARIOS[name] for name in requested)


def validate_run_parameters(*, repeats: int, seed: int) -> None:
    if not 1 <= repeats <= 50:
        raise ValueError("repeats must be between 1 and 50")
    if not 1 <= seed <= MAX_SEED:
        raise ValueError("seed must be between 1 and 2147483647")


def derive_seed(base_seed: int, *, scenario_index: int, repeat: int) -> int:
    validate_run_parameters(repeats=1, seed=base_seed)
    if min(scenario_index, repeat) < 0:
        raise ValueError("seed indexes must be non-negative")
    # A scenario/repeat pair intentionally uses the same seed for WSS and UDP.
    offset = scenario_index * 10_000 + repeat
    return (base_seed - 1 + offset) % MAX_SEED + 1


def pair_id(*, scenario: str, repeat: int) -> str:
    if repeat < 0:
        raise ValueError("repeat must be non-negative")
    return f"{scenario}:{repeat}"


def evaluate_attempt(
    scenario: NetemScenario,
    *,
    profile: str,
    error_type: str | None,
    error_code: str | None,
    worker_active_sessions_after: int | None,
    route_reacquired_and_release_request_accepted: bool,
    tc_evidence_required: bool,
    tc_rules_hit: bool | None,
    tc_dropped: int | None = None,
    exercise_outcome: str | None = None,
) -> tuple[str, str, bool]:
    cleanup_verified = (
        worker_active_sessions_after == 0
        and route_reacquired_and_release_request_accepted
        and (not tc_evidence_required or tc_rules_hit is True)
    )
    udp_blocked = scenario.udp_expected_blocked and profile == "udp-opus-gcm/1"
    if udp_blocked:
        expected = "udp_probe_timeout_and_cleanup_verified"
        observed = (
            "udp_probe_timeout"
            if error_code == "udp_probe_timeout"
            else ("completed" if error_type is None else "failed")
        )
        return expected, observed, observed == "udp_probe_timeout" and cleanup_verified
    observed = exercise_outcome or ("completed" if error_type is None else "failed")
    bounded_recovery_allowed = (
        profile == "udp-opus-gcm/1"
        and (scenario.name.startswith("random-loss") or scenario.name == "burst-loss")
        and tc_dropped is not None
        and tc_dropped > 0
    )
    expected = (
        "completed_or_bounded_recovery_and_cleanup_verified"
        if bounded_recovery_allowed
        else "completed_and_cleanup_verified"
    )
    outcome_met = observed == "completed" or (
        observed == "bounded_recovery_verified" and bounded_recovery_allowed
    )
    return expected, observed, outcome_met and cleanup_verified


def scenario_uses_randomness(scenario: NetemScenario) -> bool:
    return scenario.name.startswith("random-loss") or scenario.name in {"burst-loss", "jitter", "reorder"}


def tc_setup_commands(
    scenario: NetemScenario,
    *,
    interface: str,
    tcp_ports: Sequence[int],
    udp_ports: Sequence[int],
    seed: int,
    apply_seed: bool = True,
) -> tuple[tuple[str, ...], ...]:
    if not scenario.arguments:
        return ()
    validate_run_parameters(repeats=1, seed=seed)
    selected: list[tuple[str, int]] = []
    if not scenario.udp_only:
        selected.extend(("tcp", port) for port in tcp_ports)
    selected.extend(("udp", port) for port in udp_ports)
    if not selected:
        raise ValueError("at least one Worker media port is required")
    for _, port in selected:
        if not 1 <= port <= 65_535:
            raise ValueError(f"invalid port: {port}")

    commands: list[tuple[str, ...]] = [
        ("tc", "qdisc", "replace", "dev", interface, "root", "handle", "1:", "prio", "bands", "4")
    ]
    selected_protocols = {protocol for protocol, _ in selected}
    netem_bindings = {"tcp": ("1:3", "30:"), "udp": ("1:4", "40:")}
    for protocol in ("tcp", "udp"):
        if protocol not in selected_protocols:
            continue
        parent, handle = netem_bindings[protocol]
        command = (
                "tc",
                "qdisc",
                "replace",
                "dev",
                interface,
                "parent",
                parent,
                "handle",
                handle,
                "netem",
                *scenario.arguments,
            )
        if apply_seed and scenario_uses_randomness(scenario):
            command = (*command, "seed", str(seed))
        commands.append(command)
    protocol_numbers = {"tcp": "6", "udp": "17"}
    flowids = {"tcp": "1:3", "udp": "1:4"}
    for protocol, port in selected:
        for direction in ("sport", "dport"):
            commands.append(
                (
                    "tc",
                    "filter",
                    "add",
                    "dev",
                    interface,
                    "protocol",
                    "ip",
                    "parent",
                    "1:",
                    "prio",
                    "1",
                    "u32",
                    "match",
                    "ip",
                    "protocol",
                    protocol_numbers[protocol],
                    "0xff",
                    "match",
                    "ip",
                    direction,
                    str(port),
                    "0xffff",
                    "flowid",
                    flowids[protocol],
                )
            )
    return tuple(commands)


class NetemController:
    def __init__(self, interface: str = "lo") -> None:
        self._interface = interface
        self._seed_supported: bool | None = None

    def reset(self, *, require_clean: bool = False) -> None:
        subprocess.run(
            ["tc", "qdisc", "del", "dev", self._interface, "root"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if require_clean:
            statistics = self._qdisc_statistics()
            handles = managed_netem_handles(statistics)
            if handles:
                raise RuntimeError(f"managed netem qdisc remained after reset: {sorted(handles)}")

    def apply(
        self,
        scenario: NetemScenario,
        *,
        tcp_ports: Sequence[int],
        udp_ports: Sequence[int],
        seed: int,
    ) -> tuple[str, bool]:
        self.reset(require_clean=True)
        random_seed_relevant = scenario_uses_randomness(scenario)
        apply_seed = random_seed_relevant and self._seed_supported is not False
        commands = tc_setup_commands(
            scenario,
            interface=self._interface,
            tcp_ports=tcp_ports,
            udp_ports=udp_ports,
            seed=seed,
            apply_seed=apply_seed,
        )
        try:
            self._run_commands(commands)
        except subprocess.CalledProcessError as exc:
            output = f"{exc.stdout or ''}\n{exc.stderr or ''}"
            if not apply_seed or not tc_seed_is_unsupported(output):
                self.reset(require_clean=True)
                raise
            self.reset(require_clean=True)
            fallback = tc_setup_commands(
                scenario,
                interface=self._interface,
                tcp_ports=tcp_ports,
                udp_ports=udp_ports,
                seed=seed,
                apply_seed=False,
            )
            try:
                self._run_commands(fallback)
            except subprocess.CalledProcessError:
                self.reset(require_clean=True)
                raise
            self._seed_supported = False
            return "unavailable", False
        if apply_seed:
            self._seed_supported = True
            return "applied", True
        return ("unavailable", False) if random_seed_relevant else ("not_applicable", False)

    @staticmethod
    def _run_commands(commands: Sequence[tuple[str, ...]]) -> None:
        for command in commands:
            subprocess.run(command, check=True, capture_output=True, text=True)

    def statistics(self) -> tuple[str, str]:
        qdisc = self._qdisc_statistics()
        filters = subprocess.run(
            ["tc", "-s", "filter", "show", "dev", self._interface, "parent", "1:"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return qdisc, filters

    def _qdisc_statistics(self) -> str:
        return subprocess.run(
            ["tc", "-s", "qdisc", "show", "dev", self._interface],
            check=True,
            capture_output=True,
            text=True,
        ).stdout


def managed_netem_handles(qdisc_statistics: str) -> set[str]:
    return set(re.findall(r"(?m)^qdisc netem (30|40):", qdisc_statistics))


def tc_seed_is_unsupported(output: str) -> bool:
    normalized = output.lower()
    return "seed" in normalized and any(
        marker in normalized for marker in ('what is "seed"', "unknown", "unsupported", "illegal")
    )


def netem_counters(qdisc_statistics: str, *, handle: str) -> dict[str, int] | None:
    """Return auditable counters for one protocol-specific netem handle."""
    if handle not in {"30", "40"}:
        raise ValueError("unsupported managed netem handle")
    section = re.search(
        rf"(?ms)^qdisc netem {handle}:.*?(?=^qdisc |\Z)",
        qdisc_statistics,
    )
    if section is None:
        return None
    sent = re.search(r"Sent\s+\d+\s+bytes\s+(\d+)\s+pkt", section.group(0))
    if sent is None:
        return None
    counters = {"sent": int(sent.group(1))}
    for name, value in re.findall(r"\b(dropped|overlimits|requeues|reordered)\s+(\d+)\b", section.group(0)):
        counters[name] = int(value)
    counters.setdefault("dropped", 0)
    counters["admitted"] = counters["sent"] + counters["dropped"]
    return counters


def profile_netem_handle(scenario: NetemScenario, *, profile: str) -> str | None:
    if not scenario.arguments or (scenario.udp_only and profile != "udp-opus-gcm/1"):
        return None
    return "40" if profile == "udp-opus-gcm/1" else "30"


def use_composition_root(scenario: NetemScenario, *, profile: str) -> bool:
    """Only the UDP-blocked probe bypasses DesktopApp to preserve its exact error contract."""
    return not (scenario.udp_expected_blocked and profile == "udp-opus-gcm/1")


def classify_impairment_observation(
    scenario: NetemScenario,
    counters: dict[str, int] | None,
) -> str:
    if counters is None:
        return "not_applicable"
    if counters["admitted"] == 0:
        return "rules_not_hit"
    if scenario.name.startswith("random-loss") or scenario.name == "burst-loss":
        return "impairment_observed" if counters["dropped"] > 0 else "no_impairment_observed"
    if scenario.name == "reorder":
        reordered = counters.get("reordered")
        if reordered is None:
            return "impairment_counter_unavailable"
        return "impairment_observed" if reordered > 0 else "no_impairment_observed"
    return "configured_impairment_traversed"


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def aggregate_results(results: Iterable[AttemptResult]) -> dict[str, Any]:
    materialized = list(results)
    groups: dict[tuple[str, str], list[AttemptResult]] = defaultdict(list)
    for result in materialized:
        groups[(result.scenario, result.profile)].append(result)
    summaries = []
    for (scenario, profile), attempts in sorted(groups.items()):
        elapsed = [
            attempt.session_completion_latency_ms
            for attempt in attempts
            if attempt.session_completion_latency_ms is not None
        ]
        met = sum(attempt.expectation_met for attempt in attempts)
        summaries.append(
            {
                "scenario": scenario,
                "profile": profile,
                "attempts": len(attempts),
                "expectations_met": met,
                "expectation_rate": round(met / len(attempts), 6),
                "completed": sum(attempt.observed == "completed" for attempt in attempts),
                "bounded_recovery_verified": sum(
                    attempt.observed == "bounded_recovery_verified" for attempt in attempts
                ),
                "blocked_or_failed": sum(
                    attempt.observed not in {"completed", "bounded_recovery_verified"}
                    for attempt in attempts
                ),
                "impairment_observations": {
                    observation: sum(attempt.impairment_observation == observation for attempt in attempts)
                    for observation in sorted({attempt.impairment_observation for attempt in attempts})
                },
                "session_completion_latency_ms": {
                    "min": round(min(elapsed), 3) if elapsed else None,
                    "p50": _percentile(elapsed, 0.50),
                    "p95": _percentile(elapsed, 0.95),
                    "max": round(max(elapsed), 3) if elapsed else None,
                },
            }
        )
    return {
        "schema_version": 1,
        "evidence_scope": "completion_and_bounded_recovery",
        "environment": "isolated_loopback_netns",
        "comparison_limit": (
            "completion_and_bounded_recovery_unpaired_random_impairment"
            if any(attempt.tc_seed_control == "unavailable" for attempt in materialized)
            else "not_transport_performance_evidence"
        ),
        "tc_seed_control": (
            "unavailable"
            if any(attempt.tc_seed_control == "unavailable" for attempt in materialized)
            else "applied"
            if any(attempt.tc_seed_control == "applied" for attempt in materialized)
            else "not_applicable"
        ),
        "seed_applied": any(attempt.seed_applied for attempt in materialized),
        "paired_randomness": bool(materialized)
        and any(attempt.tc_seed_control != "not_applicable" for attempt in materialized)
        and all(
            attempt.paired_randomness
            for attempt in materialized
            if attempt.tc_seed_control != "not_applicable"
        ),
        "attempts": len(materialized),
        "all_expectations_met": bool(materialized) and all(result.expectation_met for result in materialized),
        "groups": summaries,
    }


def write_results(results: Sequence[AttemptResult], *, raw_path: Path, aggregate_path: Path) -> None:
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8", newline="\n") as stream:
        for result in results:
            stream.write(json.dumps(asdict(result), ensure_ascii=True, sort_keys=True) + "\n")
    aggregate_path.write_text(
        json.dumps(aggregate_results(results), ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

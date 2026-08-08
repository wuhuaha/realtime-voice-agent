from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = PRODUCT_ROOT / "server" / "tools" / "capacity_cluster.py"
SPEC = importlib.util.spec_from_file_location("capacity_cluster", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capacity_cluster = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capacity_cluster
SPEC.loader.exec_module(capacity_cluster)


def test_resource_plan_preserves_total_budget() -> None:
    plan = capacity_cluster.resource_plan(4, 4096, 2)
    assert plan.worker_count == 2
    assert plan.director_cpus + plan.redis_cpus + plan.worker_cpus_each * 2 == pytest.approx(4)
    allocated_memory = plan.director_memory_mib + plan.redis_memory_mib + plan.worker_memory_mib_each * 2
    assert allocated_memory <= 4096
    assert 4096 - allocated_memory < 2


def test_cluster_uses_maximum_valid_route_lease_ttl() -> None:
    environment = capacity_cluster._base_environment()  # noqa: SLF001
    assert environment["VOICE_ROUTE_LEASE_TTL_SECONDS"] == "300"
    assert environment["VOICE_LAB_TOKEN"] == "validator-host-e2e-lab-token"


def test_cluster_parser_defaults_to_high_but_bounded_pids_limit() -> None:
    args = capacity_cluster.build_parser().parse_args(
        [
            "start",
            "--name", "test",
            "--image", "test:latest",
            "--server-cpus", "1",
            "--server-memory-mib", "512",
            "--worker-count", "1",
            "--worker-max-sessions", "100",
            "--manifest", "manifest.json",
        ]
    )
    assert args.pids_limit == 65535


@pytest.mark.skipif(os.name != "posix", reason="requires Linux /proc")
def test_process_sample_reads_current_process_fd_and_thread_counts() -> None:
    sample = capacity_cluster._process_sample(os.getpid())  # noqa: SLF001
    assert sample["pid"] == os.getpid()
    assert isinstance(sample["fd_count"], int) and sample["fd_count"] > 0
    assert isinstance(sample["threads"], int) and sample["threads"] > 0
    cgroup_memory = sample["cgroup_memory"]
    if cgroup_memory is None:
        pytest.skip("requires cgroup v2 memory controller")
    assert isinstance(cgroup_memory["memory_current"], int)
    assert isinstance(cgroup_memory["swap_current"], int)


def test_cgroup_memory_sample_rejects_non_process_pid() -> None:
    assert capacity_cluster._cgroup_memory_sample(0) is None  # noqa: SLF001


@pytest.mark.parametrize(
    ("cpus", "memory", "workers", "message"),
    [
        (0.5, 512, 1, "server_cpus"),
        (1, 256, 1, "server_memory_mib"),
        (1, 512, 0, "worker_count"),
        (1, 512, 3, "128 MiB"),
    ],
)
def test_resource_plan_rejects_invalid_or_unusable_budget(
    cpus: float,
    memory: int,
    workers: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        capacity_cluster.resource_plan(cpus, memory, workers)

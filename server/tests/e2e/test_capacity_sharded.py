from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = PRODUCT_ROOT / "server" / "tools" / "capacity_sharded.py"
SPEC = importlib.util.spec_from_file_location("capacity_sharded", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capacity_sharded = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capacity_sharded
SPEC.loader.exec_module(capacity_sharded)


@pytest.mark.parametrize(
    ("total", "shards", "expected"),
    [
        (10, 1, [10]),
        (10, 3, [4, 3, 3]),
        (3, 8, [1, 1, 1]),
    ],
)
def test_shard_counts_preserve_total(total: int, shards: int, expected: list[int]) -> None:
    counts = capacity_sharded._counts(total, shards)
    assert counts == expected
    assert sum(counts) == total


def test_sharded_parser_rejects_non_positive_values() -> None:
    parser = capacity_sharded.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--director-url",
                "http://127.0.0.1:8080",
                "--profile",
                "wss-opus/1",
                "--total-concurrency",
                "0",
                "--worker-max-sessions",
                "1",
                "--artifact-dir",
                "artifacts",
                "--summary",
                "summary.json",
            ]
        )


def test_aggregate_preserves_infrastructure_failure_without_crashing(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "client"
    artifact_dir.mkdir()
    (artifact_dir / "shard-0.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "status": "failed",
                        "infrastructure_failure": {
                            "stage": "worker_idle_wait",
                            "exception_type": "TimeoutError",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "shard-0.jsonl").write_text("", encoding="utf-8")
    args = Namespace(
        profile="udp-opus-gcm/1",
        total_concurrency=200,
        shards=1,
        worker_count=4,
        worker_max_sessions=1600,
        warmup_seconds=30,
        measurement_seconds=150,
        ramp_per_second=50,
    )

    result = capacity_sharded._aggregate(args, artifact_dir, [1])  # noqa: SLF001

    assert result["status"] == "invalid"
    assert result["client_generator_valid"] is False
    assert result["sessions_attempted"] == 0
    assert result["active_sessions_peak_total"] == 0
    assert result["failure_types"] == ["TimeoutError"]
    assert result["infrastructure_failures"] == [
        {"stage": "worker_idle_wait", "exception_type": "TimeoutError"}
    ]

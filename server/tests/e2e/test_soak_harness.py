from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = PRODUCT_ROOT / "server" / "tools" / "capacity_soak.py"
SPEC = importlib.util.spec_from_file_location("capacity_soak_for_soak_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capacity_soak = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capacity_soak
SPEC.loader.exec_module(capacity_soak)


def test_scenario_worker_count_uses_configured_session_capacity() -> None:
    profile = capacity_soak.MediaProfile.WSS_OPUS_V1
    assert capacity_soak.Scenario(1, 1.0, 1, profile).worker_count == 1
    assert capacity_soak.Scenario(5, 1.0, 1, profile).worker_count == 1
    assert capacity_soak.Scenario(10, 1.0, 1, profile).worker_count == 2
    assert capacity_soak.Scenario(5, 1.0, 1, profile, worker_max_sessions=2).worker_count == 3
    with pytest.raises(ValueError, match="concurrency"):
        capacity_soak.Scenario(0, 1.0, 1, profile)
    with pytest.raises(ValueError, match="duration"):
        capacity_soak.Scenario(1, 0.0, 1, profile)
    with pytest.raises(ValueError, match="worker_max_sessions"):
        capacity_soak.Scenario(1, 1.0, 1, profile, worker_max_sessions=0)


def test_jsonl_recorder_writes_structured_records(tmp_path: Path) -> None:
    path = tmp_path / "raw.jsonl"
    with capacity_soak.JsonlRecorder(path) as recorder:
        recorder.emit("sample", seed=17, status="measured")
    line = path.read_text(encoding="utf-8")
    assert '"event": "sample"' in line
    assert '"seed": 17' in line

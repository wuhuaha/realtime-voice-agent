from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and positive")
    return parsed


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def _counts(total: int, shards: int) -> list[int]:
    base, remainder = divmod(total, shards)
    return [base + (1 if index < remainder else 0) for index in range(shards) if base or index < remainder]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run sharded provider-free steady clients against one RVA cluster.")
    parser.add_argument("--director-url", required=True)
    parser.add_argument("--profile", choices=("wss-opus/1", "udp-opus-gcm/1"), required=True)
    parser.add_argument("--total-concurrency", type=_positive_int, required=True)
    parser.add_argument("--shards", type=_positive_int, default=1)
    parser.add_argument("--worker-count", type=_positive_int, default=1)
    parser.add_argument("--worker-max-sessions", type=_positive_int, required=True)
    parser.add_argument("--warmup-seconds", type=_positive_float, default=30)
    parser.add_argument("--measurement-seconds", type=_positive_float, default=150)
    parser.add_argument("--ramp-per-second", type=_positive_float, default=50)
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    return parser


def _command(args: argparse.Namespace, *, index: int, concurrency: int, artifact_dir: Path) -> list[str]:
    per_shard_ramp = max(1.0, args.ramp_per_second / min(args.shards, args.total_concurrency))
    return [
        sys.executable,
        str(Path(__file__).with_name("capacity_soak.py")),
        "steady",
        "--director-url",
        args.director_url,
        "--profile",
        args.profile,
        "--steps",
        str(concurrency),
        "--worker-count",
        str(args.worker_count),
        "--worker-max-sessions",
        str(args.worker_max_sessions),
        "--warmup-seconds",
        str(args.warmup_seconds),
        "--measurement-seconds",
        str(args.measurement_seconds),
        "--ramp-per-second",
        str(per_shard_ramp),
        "--device-prefix",
        f"load-s{index}",
        "--seed",
        str(args.seed + index),
        "--raw",
        str(artifact_dir / f"shard-{index}.jsonl"),
        "--summary",
        str(artifact_dir / f"shard-{index}.json"),
    ]


def _aggregate(args: argparse.Namespace, artifact_dir: Path, return_codes: list[int]) -> dict[str, Any]:
    shard_summaries = [
        json.loads((artifact_dir / f"shard-{index}.json").read_text(encoding="utf-8"))
        for index in range(len(return_codes))
    ]
    results = [summary["results"][0] for summary in shard_summaries]
    required = {
        "sessions_attempted",
        "sessions_succeeded",
        "frames",
        "route_reacquire_rate",
        "worker_active_sessions_peak",
        "worker_active_sessions_final",
        "active_sessions_peak_total",
        "client_generator_valid",
        "failure_types",
    }
    valid_results = [result for result in results if required <= result.keys()]
    infrastructure_failures = [
        result.get("infrastructure_failure", {"stage": "unknown", "exception_type": "unknown"})
        for result in results
        if not required <= result.keys()
    ]
    connect_values: list[float] = []
    for index in range(len(return_codes)):
        for line in (artifact_dir / f"shard-{index}.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") in {"steady.session.completed", "steady.session.failed"}:
                value = event.get("connect_ms")
                if isinstance(value, int | float):
                    connect_values.append(float(value))
    attempted = sum(int(result["sessions_attempted"]) for result in valid_results)
    succeeded = sum(int(result["sessions_succeeded"]) for result in valid_results)
    playback_frames = sum(int(result["frames"]["initial_playback"]) for result in valid_results)
    sent_frames = sum(int(result["frames"]["client_uplink_sent"]) for result in valid_results)
    late_frames = sum(int(result["frames"]["client_source_late"]) for result in valid_results)
    route_verified = sum(
        round(float(result["route_reacquire_rate"]) * int(result["sessions_attempted"]))
        for result in valid_results
    )
    worker_peaks: dict[str, int] = {}
    for result in valid_results:
        for worker_id, peak in result["worker_active_sessions_peak"].items():
            worker_peaks[worker_id] = max(worker_peaks.get(worker_id, 0), int(peak))
    measured = not infrastructure_failures and all(
        code == 0 and result["status"] == "measured"
        for code, result in zip(return_codes, results, strict=True)
    )
    status = "invalid" if infrastructure_failures else ("measured" if measured else "failed")
    return {
        "status": status,
        "evidence_scope": "sharded_steady_provider_free_uplink_and_initial_downlink",
        "profile": args.profile,
        "total_concurrency": args.total_concurrency,
        "shards": len(return_codes),
        "worker_count": args.worker_count,
        "worker_max_sessions": args.worker_max_sessions,
        "warmup_seconds": args.warmup_seconds,
        "measurement_seconds": args.measurement_seconds,
        "ramp_per_second": args.ramp_per_second,
        "sessions_attempted": attempted,
        "sessions_succeeded": succeeded,
        "session_survival_rate": round(succeeded / attempted, 6) if attempted else 0.0,
        "initial_playback_rate": round(
            playback_frames / (attempted * 4),
            6,
        ) if attempted else 0.0,
        "route_reacquire_rate": round(route_verified / attempted, 6) if attempted else 0.0,
        "connect_latency_ms": {
            "p50": _percentile(connect_values, 0.50),
            "p95": _percentile(connect_values, 0.95),
            "p99": _percentile(connect_values, 0.99),
            "max": round(max(connect_values), 3) if connect_values else None,
        },
        "frames": {
            "client_uplink_sent": sent_frames,
            "initial_playback": playback_frames,
            "client_source_late": late_frames,
            "client_source_late_rate": round(late_frames / sent_frames, 8) if sent_frames else 1.0,
        },
        "client_generator_valid": bool(valid_results)
        and not infrastructure_failures
        and all(bool(result["client_generator_valid"]) for result in valid_results),
        "active_sessions_peak_total": max(
            (int(result["active_sessions_peak_total"]) for result in valid_results), default=0
        ),
        "worker_active_sessions_peak": worker_peaks,
        "worker_active_sessions_final": (
            valid_results[-1]["worker_active_sessions_final"] if valid_results else {}
        ),
        "failure_types": sorted(
            {
                *(item for result in valid_results for item in result["failure_types"]),
                *(str(item.get("exception_type", "infrastructure_failure")) for item in infrastructure_failures),
            }
        ),
        "infrastructure_failures": infrastructure_failures,
        "shard_return_codes": return_codes,
        "shard_summaries": [f"shard-{index}.json" for index in range(len(return_codes))],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.shards > args.total_concurrency:
        args.shards = args.total_concurrency
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    processes: list[tuple[subprocess.Popen[bytes], Any]] = []
    started_at = time.time()
    try:
        for index, concurrency in enumerate(_counts(args.total_concurrency, args.shards)):
            stream = (args.artifact_dir / f"shard-{index}.log").open("wb")
            process = subprocess.Popen(
                _command(args, index=index, concurrency=concurrency, artifact_dir=args.artifact_dir),
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            processes.append((process, stream))
        return_codes = [process.wait() for process, _ in processes]
    except BaseException:
        for process, _ in processes:
            if process.poll() is None:
                process.terminate()
        for process, _ in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        raise
    finally:
        for _, stream in processes:
            stream.close()
    output = _aggregate(args, args.artifact_dir, return_codes)
    output["elapsed_seconds"] = round(time.time() - started_at, 3)
    args.summary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if output["status"] == "measured" else 1


if __name__ == "__main__":
    raise SystemExit(main())

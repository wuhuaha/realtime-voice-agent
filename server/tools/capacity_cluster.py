from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen

INTERNAL_TOKEN = "validator-host-e2e-internal-token"
BOOTSTRAP_TOKEN = "validator-host-e2e-bootstrap-token"
GRANT_SIGNING_KEY = "validator-host-e2e-grant-signing-key"
LAB_TOKEN = "validator-host-e2e-lab-token"
ROUTE_LEASE_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ResourcePlan:
    server_cpus: float
    server_memory_mib: int
    director_cpus: float
    director_memory_mib: int
    redis_cpus: float
    redis_memory_mib: int
    worker_cpus_each: float
    worker_memory_mib_each: int
    worker_count: int


def resource_plan(server_cpus: float, server_memory_mib: int, worker_count: int) -> ResourcePlan:
    if not math.isfinite(server_cpus) or server_cpus < 1:
        raise ValueError("server_cpus must be finite and at least 1")
    if server_memory_mib < 512:
        raise ValueError("server_memory_mib must be at least 512")
    if worker_count < 1:
        raise ValueError("worker_count must be positive")
    director_cpus = min(0.5, max(0.1, server_cpus * 0.10))
    redis_cpus = min(0.25, max(0.05, server_cpus * 0.05))
    worker_cpu_total = server_cpus - director_cpus - redis_cpus
    if worker_cpu_total < worker_count * 0.1:
        raise ValueError("server_cpus leaves less than 0.1 CPU for each Worker")
    director_memory = min(512, max(128, round(server_memory_mib * 0.15)))
    redis_memory = min(256, max(64, round(server_memory_mib * 0.10)))
    worker_memory_total = server_memory_mib - director_memory - redis_memory
    if worker_memory_total < worker_count * 128:
        raise ValueError("server_memory_mib leaves less than 128 MiB for each Worker")
    return ResourcePlan(
        server_cpus=server_cpus,
        server_memory_mib=server_memory_mib,
        director_cpus=round(director_cpus, 3),
        director_memory_mib=director_memory,
        redis_cpus=round(redis_cpus, 3),
        redis_memory_mib=redis_memory,
        worker_cpus_each=round(worker_cpu_total / worker_count, 3),
        worker_memory_mib_each=worker_memory_total // worker_count,
        worker_count=worker_count,
    )


def _run(command: list[str], *, capture: bool = True) -> str:
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    return completed.stdout.strip() if capture else ""


def _docker_run(
    *,
    name: str,
    network: str,
    image: str,
    cpus: float,
    memory_mib: int,
    environment: dict[str, str],
    ports: list[str],
    command: list[str],
    pids_limit: int,
    network_alias: str | None = None,
) -> str:
    arguments = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--cpus",
        str(cpus),
        "--memory",
        f"{memory_mib}m",
        "--pids-limit",
        str(pids_limit),
        "--ulimit",
        "nofile=65535:65535",
        "--label",
        "rva.capacity-benchmark=true",
    ]
    if network_alias:
        arguments.extend(("--network-alias", network_alias))
    for publication in ports:
        arguments.extend(("--publish", publication))
    for key, value in environment.items():
        arguments.extend(("--env", f"{key}={value}"))
    arguments.append(image)
    arguments.extend(command)
    return _run(arguments)


def _wait_ready(url: str, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:  # noqa: S310 - loopback URL is generated locally
                if response.status == 200:
                    return
        except Exception as exc:  # readiness retries preserve the final exception type
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"readiness failed for {url}: {type(last_error).__name__ if last_error else 'timeout'}")


def _base_environment() -> dict[str, str]:
    return {
        "VOICE_ENV": "development",
        "VOICE_INTERNAL_TOKEN": INTERNAL_TOKEN,
        "VOICE_GRANT_SIGNING_KEY": GRANT_SIGNING_KEY,
        "VOICE_DEVICE_BOOTSTRAP_TOKEN": BOOTSTRAP_TOKEN,
        "VOICE_LAB_TOKEN": LAB_TOKEN,
        "VOICE_ALLOW_SHARED_BOOTSTRAP_AUTH": "true",
        "VOICE_ROUTE_LEASE_TTL_SECONDS": str(ROUTE_LEASE_TTL_SECONDS),
    }


def _capture_startup_failure(artifact_dir: Path, containers: list[str]) -> None:
    failure_dir = artifact_dir / "startup-failure"
    failure_dir.mkdir(parents=True, exist_ok=True)
    for container in containers:
        log = subprocess.run(
            ["docker", "logs", container],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
        (failure_dir / f"{container}.log").write_bytes(log)
        state = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", container],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
        (failure_dir / f"{container}.state.json").write_bytes(state)


def _start(args: argparse.Namespace) -> int:
    if args.pids_limit < 1:
        raise ValueError("pids_limit must be positive")
    plan = resource_plan(args.server_cpus, args.server_memory_mib, args.worker_count)
    per_worker_sessions = math.ceil(args.worker_max_sessions / args.worker_count)
    if per_worker_sessions > 1024:
        raise ValueError("per-Worker max_sessions cannot exceed the protocol contract limit of 1024")
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    network = f"{args.name}-net"
    containers: list[str] = []
    director_port = args.base_port
    worker_http_ports = [args.base_port + 1 + index for index in range(args.worker_count)]
    worker_udp_ports = [args.base_port + 100 + index for index in range(args.worker_count)]
    try:
        _run(["docker", "network", "create", network])
        redis_name = f"{args.name}-redis"
        containers.append(redis_name)
        _docker_run(
            name=redis_name,
            network=network,
            network_alias="redis",
            image=args.redis_image,
            cpus=plan.redis_cpus,
            memory_mib=plan.redis_memory_mib,
            environment={},
            ports=[],
            command=["redis-server", "--save", "", "--appendonly", "no"],
            pids_limit=args.pids_limit,
        )
        director_name = f"{args.name}-director"
        containers.append(director_name)
        director_env = {
            **_base_environment(),
            "VOICE_DIRECTOR_BIND_HOST": "0.0.0.0",
            "VOICE_DIRECTOR_BIND_PORT": "8080",
            "VOICE_COORDINATION_BACKEND": "redis",
            "VOICE_REDIS_URL": "redis://redis:6379/0",
            "VOICE_COORDINATION_PREFIX": args.name,
            "VOICE_HEARTBEAT_INTERVAL_SECONDS": "1",
        }
        _docker_run(
            name=director_name,
            network=network,
            image=args.image,
            cpus=plan.director_cpus,
            memory_mib=plan.director_memory_mib,
            environment=director_env,
            ports=[f"127.0.0.1:{director_port}:8080/tcp"],
            command=["session-director"],
            pids_limit=args.pids_limit,
        )
        _wait_ready(f"http://127.0.0.1:{director_port}/health/ready")
        for index, (http_port, udp_port) in enumerate(
            zip(worker_http_ports, worker_udp_ports, strict=True),
            start=1,
        ):
            worker_name = f"{args.name}-worker-{index}"
            containers.append(worker_name)
            worker_env = {
                **_base_environment(),
                "VOICE_WORKER_ID": worker_name,
                "VOICE_WORKER_BIND_HOST": "0.0.0.0",
                "VOICE_WORKER_BIND_PORT": "8081",
                "VOICE_WORKER_MAX_SESSIONS": str(per_worker_sessions),
                "VOICE_DIRECTOR_URL": "http://" + director_name + ":8080",
                "VOICE_HEARTBEAT_INTERVAL_SECONDS": "1",
                "VOICE_RVA_ENABLED": "true",
                "VOICE_RVA_PUBLIC_WS_URL": f"ws://127.0.0.1:{http_port}/rva/v1/voice",
                "VOICE_RVA_UDP_ENABLED": "true",
                "VOICE_UDP_BIND_HOST": "0.0.0.0",
                "VOICE_UDP_BIND_PORT": "8092",
                "VOICE_UDP_ADVERTISE_HOST": "127.0.0.1",
                "VOICE_UDP_ADVERTISE_PORT": str(udp_port),
                "VOICE_UDP_SESSION_LIFETIME_SECONDS": "900",
                "VOICE_RUNNER": "deterministic",
                "VOICE_PROVIDER_READINESS_REQUIRED": "false",
                "VOICE_RVA_UPLINK_MAX_AGE_SECONDS": "2.0",
            }
            _docker_run(
                name=worker_name,
                network=network,
                image=args.image,
                cpus=plan.worker_cpus_each,
                memory_mib=plan.worker_memory_mib_each,
                environment=worker_env,
                ports=[
                    f"127.0.0.1:{http_port}:8081/tcp",
                    f"127.0.0.1:{udp_port}:8092/udp",
                ],
                command=["realtime-worker"],
                pids_limit=args.pids_limit,
            )
            _wait_ready(f"http://127.0.0.1:{http_port}/health/ready")
        image_id = _run(["docker", "image", "inspect", "--format", "{{.Id}}", args.image])
        manifest = {
            "schema_version": "1.0",
            "name": args.name,
            "started_at": time.time(),
            "image": args.image,
            "image_id": image_id,
            "redis_image": args.redis_image,
            "network": network,
            "containers": containers,
            "director_url": f"http://127.0.0.1:{director_port}",
            "worker_http_ports": worker_http_ports,
            "worker_udp_ports": worker_udp_ports,
            "worker_max_sessions_total": per_worker_sessions * args.worker_count,
            "worker_max_sessions_each": per_worker_sessions,
            "nofile": 65535,
            "pids_limit": args.pids_limit,
            "resources": asdict(plan),
        }
        args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False))
        return 0
    except BaseException:
        _capture_startup_failure(args.manifest.parent, containers)
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL)
        subprocess.run(["docker", "network", "rm", network], check=False, stdout=subprocess.DEVNULL)
        raise


def _system_sample() -> dict[str, Any]:
    meminfo = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        meminfo[key] = value.strip()
    vmstat = {}
    for line in Path("/proc/vmstat").read_text(encoding="utf-8").splitlines():
        key, value = line.split()
        if key in {"pswpin", "pswpout"}:
            vmstat[key] = int(value)
    return {
        "loadavg": Path("/proc/loadavg").read_text(encoding="utf-8").strip(),
        "mem_available": meminfo.get("MemAvailable"),
        "swap_free": meminfo.get("SwapFree"),
        **vmstat,
    }


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _cgroup_memory_sample(pid: int) -> dict[str, Any] | None:
    if pid <= 0:
        return None
    try:
        lines = (Path("/proc") / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    relative = next(
        (line.split(":", 2)[2] for line in lines if line.startswith("0::")),
        None,
    )
    if relative is None:
        return None
    root = Path("/sys/fs/cgroup") / relative.lstrip("/")
    events: dict[str, int] = {}
    try:
        for line in (root / "memory.events").read_text(encoding="utf-8").splitlines():
            key, value = line.split()
            events[key] = int(value)
    except (OSError, ValueError):
        pass
    return {
        "path": str(root),
        "memory_current": _read_int(root / "memory.current"),
        "memory_peak": _read_int(root / "memory.peak"),
        "swap_current": _read_int(root / "memory.swap.current"),
        "events": events,
    }


def _process_sample(pid: int) -> dict[str, Any]:
    if pid <= 0:
        return {"pid": pid, "fd_count": None, "threads": None, "cgroup_memory": None}
    root = Path("/proc") / str(pid)
    try:
        fd_count = sum(1 for _ in (root / "fd").iterdir())
    except OSError:
        fd_count = None
    threads = None
    try:
        for line in (root / "status").read_text(encoding="utf-8").splitlines():
            if line.startswith("Threads:"):
                threads = int(line.split(":", 1)[1].strip())
                break
    except (OSError, ValueError):
        pass
    return {
        "pid": pid,
        "fd_count": fd_count,
        "threads": threads,
        "cgroup_memory": _cgroup_memory_sample(pid),
    }


def _container_process_samples(containers: list[str]) -> dict[str, dict[str, Any]]:
    raw = _run(["docker", "inspect", "--format", "{{.State.Pid}}", *containers])
    pids = [int(value) for value in raw.splitlines()]
    if len(pids) != len(containers):
        raise RuntimeError("docker inspect returned an unexpected PID count")
    samples = {name: _process_sample(pid) for name, pid in zip(containers, pids, strict=True)}
    for name, sample in samples.items():
        if sample["fd_count"] is not None and sample["threads"] is not None:
            continue
        try:
            namespace_raw = _run(
                [
                    "docker",
                    "exec",
                    name,
                    "sh",
                    "-c",
                    (
                        "find /proc/1/fd -maxdepth 1 -type l 2>/dev/null | wc -l; "
                        "grep '^Threads:' /proc/1/status | awk '{print $2}'"
                    ),
                ]
            ).splitlines()
        except (subprocess.CalledProcessError, ValueError):
            continue
        if len(namespace_raw) == 2:
            sample["fd_count"] = int(namespace_raw[0])
            sample["threads"] = int(namespace_raw[1])
    return samples


def _sample(args: argparse.Namespace) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    containers = list(manifest["containers"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration_seconds
    with args.output.open("w", encoding="utf-8", newline="\n") as stream:
        while time.monotonic() < deadline:
            raw = _run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers])
            container_samples = [json.loads(line) for line in raw.splitlines() if line]
            stream.write(
                json.dumps(
                    {
                        "recorded_at": time.time(),
                        "containers": container_samples,
                        "processes": _container_process_samples(containers),
                        "system": _system_sample(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
            stream.flush()
            time.sleep(args.interval_seconds)
    return 0


def _stop(args: argparse.Namespace) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    artifact_dir = args.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    containers = list(manifest["containers"])
    for container in containers:
        log = subprocess.run(
            ["docker", "logs", container],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
        (artifact_dir / f"{container}.log").write_bytes(log)
        state = _run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .State}}",
                container,
            ]
        )
        (artifact_dir / f"{container}.state.json").write_text(state + "\n", encoding="utf-8")
    for container in reversed(containers):
        subprocess.run(["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL)
    subprocess.run(["docker", "network", "rm", manifest["network"]], check=False, stdout=subprocess.DEVNULL)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage an isolated provider-free RVA Docker benchmark cluster.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--name", required=True)
    start.add_argument("--image", required=True)
    start.add_argument("--redis-image", default="redis:7.4.5-alpine")
    start.add_argument("--server-cpus", type=float, required=True)
    start.add_argument("--server-memory-mib", type=int, required=True)
    start.add_argument("--worker-count", type=int, required=True)
    start.add_argument("--worker-max-sessions", type=int, required=True)
    start.add_argument("--pids-limit", type=int, default=65535)
    start.add_argument("--base-port", type=int, default=19080)
    start.add_argument("--manifest", type=Path, required=True)
    start.set_defaults(handler=_start)

    sample = subparsers.add_parser("sample")
    sample.add_argument("--manifest", type=Path, required=True)
    sample.add_argument("--duration-seconds", type=float, required=True)
    sample.add_argument("--interval-seconds", type=float, default=2)
    sample.add_argument("--output", type=Path, required=True)
    sample.set_defaults(handler=_sample)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--manifest", type=Path, required=True)
    stop.add_argument("--artifact-dir", type=Path, required=True)
    stop.set_defaults(handler=_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

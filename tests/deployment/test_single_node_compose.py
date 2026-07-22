from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = ROOT / "deployment" / "single-node"
COMPOSE_PATH = DEPLOYMENT / "compose.yaml"
ENV_EXAMPLE_PATH = DEPLOYMENT / "env.example"
DOCKERFILE_PATH = DEPLOYMENT / "Dockerfile"
PREFLIGHT_PATH = DEPLOYMENT / "preflight.py"


def _compose() -> dict[str, object]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_compose_has_expected_single_node_topology() -> None:
    compose = _compose()
    services = compose["services"]
    assert set(services) == {"redis", "director", "worker"}
    assert "ports" not in services["redis"]
    assert services["director"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["worker"]["depends_on"]["director"]["condition"] == "service_healthy"
    assert services["redis"]["volumes"] == ["redis-data:/data"]
    assert compose["networks"]["coordination"]["internal"] is True
    assert compose["networks"]["worker-control"]["internal"] is True
    assert "coordination" not in services["worker"]["networks"]
    assert "worker-control" not in services["redis"]["networks"]


def test_services_are_bounded_and_health_checked() -> None:
    services = _compose()["services"]
    for service in services.values():
        assert service["restart"] == "unless-stopped"
        assert service["read_only"] is True
        assert service["healthcheck"]["test"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["cap_drop"] == ["ALL"]
    assert services["redis"]["user"] == "redis"


def test_worker_publishes_websocket_http_and_fixed_udp() -> None:
    services = _compose()["services"]
    worker_ports = services["worker"]["ports"]
    director_ports = services["director"]["ports"]
    assert all(port.endswith(":8080/tcp") for port in director_ports)
    assert any(port.endswith(":8081/tcp") for port in worker_ports)
    assert any(port.endswith(":8092/udp") for port in worker_ports)
    assert all(":0/udp" not in port for port in worker_ports)
    worker_environment = services["worker"]["environment"]
    assert worker_environment["VOICE_WORKER_BIND_PORT"] == "8081"
    assert worker_environment["VOICE_UDP_BIND_PORT"] == "8092"
    assert worker_environment["VOICE_UDP_ADVERTISE_PORT"] == "${VOICE_UDP_PUBLIC_PORT:-8092}"


def test_application_environment_is_allow_listed_per_role() -> None:
    services = _compose()["services"]
    director_environment = services["director"]["environment"]
    worker_environment = services["worker"]["environment"]
    assert "VOICE_DEVICE_CREDENTIALS" in director_environment
    assert "VOICE_REDIS_URL" in director_environment
    assert "VOICE_LLM_API_KEY" not in director_environment
    assert "VOICE_DEVICE_CREDENTIALS" not in worker_environment
    assert "VOICE_REDIS_URL" not in worker_environment
    assert "VOICE_LLM_API_KEY" in worker_environment

    for environment in (director_environment, worker_environment):
        for name, value in environment.items():
            if any(marker in name for marker in ("TOKEN", "API_KEY", "SIGNING_KEY", "CREDENTIALS")):
                assert isinstance(value, str) and value.startswith("${")

    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"\bsk-[A-Za-z0-9_-]{8,}", compose_text)


def test_env_example_defaults_capacity_and_fixed_udp_port() -> None:
    env_text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "VOICE_WORKER_MAX_SESSIONS=5" in env_text
    assert "VOICE_UDP_PUBLIC_PORT=8092" in env_text
    assert "VOICE_UDP_BIND_PORT=" not in env_text
    assert "VOICE_UDP_ADVERTISE_PORT=" not in env_text
    assert not re.search(r"\bsk-[A-Za-z0-9_-]{8,}", env_text)
    assert "replace-with-" in env_text


def test_image_uses_locked_workspace_and_non_root_runtime() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "server/uv.lock" in dockerfile
    assert dockerfile.count("uv sync --frozen") == 2
    assert "--no-dev" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert ".env" not in dockerfile
    assert 'ENTRYPOINT ["python", "/opt/rva/preflight.py"]' in dockerfile


def test_preflight_fails_closed_on_repository_placeholder() -> None:
    environment = {"PATH": str(Path(sys.executable).parent), "VOICE_INTERNAL_TOKEN": "replace-with-secret"}
    result = subprocess.run(
        [sys.executable, str(PREFLIGHT_PATH), sys.executable, "-c", "print('started')"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert result.returncode == 78
    assert "VOICE_INTERNAL_TOKEN" in result.stderr
    assert "started" not in result.stdout


def test_preflight_executes_application_without_placeholders() -> None:
    result = subprocess.run(
        [sys.executable, str(PREFLIGHT_PATH), sys.executable, "-c", "print('started')"],
        capture_output=True,
        check=False,
        env={"PATH": str(Path(sys.executable).parent), "VOICE_INTERNAL_TOKEN": "runtime-secret"},
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "started"

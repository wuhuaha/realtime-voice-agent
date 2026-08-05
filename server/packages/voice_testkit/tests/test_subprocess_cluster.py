from __future__ import annotations

import socket

from voice_testkit.subprocess_cluster import ProcessCluster, WorkerProcess, _ports_released


def _occupied_tcp_port(*, reusable: bool = False) -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if reusable:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener, int(listener.getsockname()[1])


def _occupied_udp_port() -> tuple[socket.socket, int]:
    endpoint = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    endpoint.bind(("127.0.0.1", 0))
    return endpoint, int(endpoint.getsockname()[1])


def test_ports_released_requires_exclusive_tcp_and_udp_rebind() -> None:
    director, director_port = _occupied_tcp_port()
    worker_http, worker_http_port = _occupied_tcp_port()
    worker_udp, worker_udp_port = _occupied_udp_port()
    cluster = ProcessCluster(
        director_url=f"http://127.0.0.1:{director_port}",
        director_port=director_port,
        workers=(WorkerProcess("worker-1", worker_http_port, worker_udp_port),),
    )
    try:
        assert _ports_released(cluster) is False
        director.close()
        assert _ports_released(cluster) is False
        worker_http.close()
        assert _ports_released(cluster) is False
        worker_udp.close()
        assert _ports_released(cluster) is True
    finally:
        director.close()
        worker_http.close()
        worker_udp.close()


def test_ports_released_rejects_active_reusable_tcp_listener() -> None:
    listener, port = _occupied_tcp_port(reusable=True)
    cluster = ProcessCluster(
        director_url=f"http://127.0.0.1:{port}",
        director_port=port,
        workers=(),
    )
    try:
        assert _ports_released(cluster) is False
    finally:
        listener.close()
    assert _ports_released(cluster) is True


def test_ports_released_accepts_restart_after_established_connection_closes() -> None:
    listener, port = _occupied_tcp_port(reusable=True)
    client = socket.create_connection(("127.0.0.1", port), timeout=1)
    accepted, _ = listener.accept()
    cluster = ProcessCluster(
        director_url=f"http://127.0.0.1:{port}",
        director_port=port,
        workers=(),
    )
    try:
        assert _ports_released(cluster) is False
        accepted.shutdown(socket.SHUT_WR)
        assert client.recv(1) == b""
    finally:
        accepted.close()
        client.close()
        listener.close()
    assert _ports_released(cluster) is True

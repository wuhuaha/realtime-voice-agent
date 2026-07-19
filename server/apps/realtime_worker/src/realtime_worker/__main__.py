from __future__ import annotations

import logging

import uvicorn

from .config import Settings


def main() -> None:
    settings = Settings()
    settings.validate_runtime()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "realtime_worker.app:create_app",
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        ws_max_size=settings.max_control_bytes,
        ws_max_queue=1,
        ws_per_message_deflate=False,
        ws_ping_interval=settings.websocket_ping_interval_seconds,
        ws_ping_timeout=settings.websocket_ping_timeout_seconds,
    )


if __name__ == "__main__":
    main()

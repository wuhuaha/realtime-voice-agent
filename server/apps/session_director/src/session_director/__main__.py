from __future__ import annotations

import uvicorn

from .config import DirectorSettings


def main() -> None:
    settings = DirectorSettings()
    settings.validate_runtime()
    uvicorn.run(
        "session_director.app:create_app",
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        ws_per_message_deflate=False,
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from ..errors import TransportError


class WebSocketConnection(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


WebSocketConnector = Callable[[str, Mapping[str, str]], Awaitable[WebSocketConnection]]


class _AuthorizationRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = re.sub(
            r"(?i)(authorization\s*:\s*bearer\s+)\S+",
            r"\1<redacted>",
            message,
        )
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


_WEBSOCKET_LOGGER = logging.getLogger("rva_desktop.websocket")
_WEBSOCKET_LOGGER.addFilter(_AuthorizationRedactionFilter())


class WssTransport:
    def __init__(self, connector: WebSocketConnector | None = None) -> None:
        self._connector = connector or _connect
        self._connection: WebSocketConnection | None = None

    async def open(self, url: str, *, grant: str, device_id: str) -> None:
        if self._connection is not None:
            raise TransportError("transport_already_open")
        try:
            self._connection = await self._connector(
                url,
                {"Authorization": f"Bearer {grant}", "Device-Id": device_id},
            )
        except Exception as exc:
            raise TransportError("wss_connect_failed", type(exc).__name__, retryable=True) from exc

    async def send_control(self, wire: str) -> None:
        await self._send(wire)

    async def send_media(self, wire: bytes) -> None:
        await self._send(wire)

    async def receive(self) -> str | bytes:
        if self._connection is None:
            raise TransportError("transport_not_open")
        try:
            return await self._connection.recv()
        except Exception as exc:
            raise TransportError("wss_receive_failed", type(exc).__name__, retryable=True) from exc

    async def close(self, *, code: int = 1000, reason: str = "normal") -> None:
        connection = self._connection
        if connection is None:
            return
        close_task = asyncio.create_task(
            asyncio.wait_for(connection.close(code=code, reason=reason), timeout=5.0),
            name="desktop-wss-close",
        )
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            try:
                await close_task
            except Exception:
                pass
            raise
        except Exception:
            pass
        finally:
            if self._connection is connection:
                self._connection = None

    async def _send(self, wire: str | bytes) -> None:
        if self._connection is None:
            raise TransportError("transport_not_open")
        try:
            await self._connection.send(wire)
        except Exception as exc:
            raise TransportError("wss_send_failed", type(exc).__name__, retryable=True) from exc


async def _connect(url: str, headers: Mapping[str, str]) -> WebSocketConnection:
    from websockets.asyncio.client import connect

    return await connect(
        url,
        additional_headers=headers,
        max_size=32_768,
        compression=None,
        logger=_WEBSOCKET_LOGGER,
    )

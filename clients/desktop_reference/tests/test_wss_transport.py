from __future__ import annotations

import asyncio

import pytest

from rva_desktop.errors import TransportError
from rva_desktop.transport.wss import WssTransport, _connect


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.inbound: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.closed: tuple[int, str] | None = None

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        return await self.inbound.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


def test_wss_transport_sets_bound_identity_headers_and_preserves_frame_types() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        captured: dict[str, object] = {}

        async def connector(url: str, headers: dict[str, str]) -> FakeConnection:
            captured.update(url=url, headers=dict(headers))
            return connection

        transport = WssTransport(connector)
        await transport.open("wss://worker.test/rva/v1/voice", grant="secret-grant", device_id="desktop-1")
        await transport.send_control("{}")
        await transport.send_media(b"opus")
        connection.inbound.put_nowait("control")
        connection.inbound.put_nowait(b"media")
        assert await transport.receive() == "control"
        assert await transport.receive() == b"media"
        await transport.close(reason="done")

        assert captured == {
            "url": "wss://worker.test/rva/v1/voice",
            "headers": {"Authorization": "Bearer secret-grant", "Device-Id": "desktop-1"},
        }
        assert connection.sent == ["{}", b"opus"]
        assert connection.closed == (1000, "done")

    asyncio.run(scenario())


def test_wss_close_finishes_when_caller_is_cancelled() -> None:
    async def scenario() -> None:
        class BlockingConnection(FakeConnection):
            def __init__(self) -> None:
                super().__init__()
                self.close_started = asyncio.Event()
                self.allow_close = asyncio.Event()

            async def close(self, code: int = 1000, reason: str = "") -> None:
                self.close_started.set()
                await self.allow_close.wait()
                await super().close(code=code, reason=reason)

        connection = BlockingConnection()

        async def connector(_url: str, _headers: dict[str, str]) -> BlockingConnection:
            return connection

        transport = WssTransport(connector)
        await transport.open("wss://worker.test/rva/v1/voice", grant="secret-grant", device_id="desktop-1")
        closing = asyncio.create_task(transport.close(reason="cancelled-caller"))
        await asyncio.wait_for(connection.close_started.wait(), timeout=1)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        connection.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert connection.closed == (1000, "cancelled-caller")
        with pytest.raises(TransportError, match="transport_not_open"):
            await transport.send_control("{}")

    asyncio.run(scenario())


def test_default_websocket_logger_redacts_authorization_grant(monkeypatch, caplog) -> None:
    async def scenario() -> None:
        captured: dict[str, object] = {}
        connection = FakeConnection()

        async def fake_connect(_url: str, **kwargs: object) -> FakeConnection:
            captured.update(kwargs)
            return connection

        monkeypatch.setattr("websockets.asyncio.client.connect", fake_connect)
        result = await _connect(
            "wss://worker.test/rva/v1/voice",
            {"Authorization": "Bearer connect-grant-secret"},
        )
        assert result is connection
        logger = captured["logger"]
        assert hasattr(logger, "debug")
        with caplog.at_level("DEBUG", logger="rva_desktop.websocket"):
            logger.debug("> %s: %s", "Authorization", "Bearer connect-grant-secret")  # type: ignore[union-attr]
        assert "connect-grant-secret" not in caplog.text
        assert "Authorization: Bearer <redacted>" in caplog.text

    asyncio.run(scenario())

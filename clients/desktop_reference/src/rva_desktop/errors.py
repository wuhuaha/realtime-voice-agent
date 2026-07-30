"""Stable client failures suitable for diagnostics and reconnect policy."""


class RvaClientError(RuntimeError):
    def __init__(self, code: str, detail: str = "", *, retryable: bool = False) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code
        self.retryable = retryable


class ProtocolError(RvaClientError):
    pass


class AuthenticationError(RvaClientError):
    pass


class TransportError(RvaClientError):
    pass


class FreshReopenRequired(TransportError):
    def __init__(self, detail: str = "session freshness deadline reached") -> None:
        super().__init__("fresh_reopen_required", detail, retryable=True)


class SessionClosed(RvaClientError):
    def __init__(self, detail: str = "session is closed") -> None:
        super().__init__("session_closed", detail)

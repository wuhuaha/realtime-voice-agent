"""Errors whose messages are safe to expose at a process boundary."""


class ConfigurationError(ValueError):
    """A required runtime setting is absent or incompatible."""


class ProviderError(RuntimeError):
    """A provider did not meet its local transport contract."""

    def __init__(self, provider: str, message: str, *, retryable: bool) -> None:
        self.provider = provider
        self.retryable = retryable
        super().__init__(f"{provider}: {message}")


class BackpressureError(ProviderError):
    """A bounded queue filled before the downstream consumer could make progress."""

    def __init__(self, provider: str, queue_name: str) -> None:
        super().__init__(provider, f"{queue_name} queue is full", retryable=True)

from enum import StrEnum


class DiscoveryErrorCode(StrEnum):
    """Stable Block A error codes exposed to callers and the CLI."""

    SOCKET_ERROR = "DISCOVERY_SOCKET_ERROR"
    TIMEOUT = "DISCOVERY_TIMEOUT"


class DiscoveryError(RuntimeError):
    def __init__(self, code: DiscoveryErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class MndpParseError(ValueError):
    """Raised when an MNDP datagram is structurally invalid or truncated."""

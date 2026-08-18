from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PendingRegistration:
    router_id: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    ros_version: str = "7"
    tls_fingerprint: str = ""
    identity: str | None = None
    trusted_fingerprint: bool = False

    @property
    def display_fingerprint(self) -> str:
        raw = self.tls_fingerprint.upper()
        return ":".join(raw[index : index + 2] for index in range(0, len(raw), 2))


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    router_id: str
    identity: str
    tool_count: int
    health: dict[str, Any]
    system_status: dict[str, Any]

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MndpField:
    type_id: int
    value: str | int | bytes


@dataclass(frozen=True, slots=True)
class MndpPacket:
    sequence: int
    fields: tuple[MndpField, ...]


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """An untrusted MNDP self-announcement, normalized for Block A consumers."""

    mac: str | None = None
    ipv4_addresses: tuple[str, ...] = ()
    ipv6_addresses: tuple[str, ...] = ()
    identity: str | None = None
    version: str | None = None
    board: str | None = None
    uptime_seconds: int | None = None
    interfaces: tuple[str, ...] = ()
    platform: str | None = None
    software_id: str | None = None
    source_ip: str | None = None

    @property
    def key(self) -> str:
        if self.software_id:
            return f"software:{self.software_id}"
        if self.mac:
            return f"mac:{self.mac}"
        if self.source_ip:
            return f"source:{self.source_ip}"
        return f"anonymous:{self.identity or ''}:{self.board or ''}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mac": self.mac,
            "ipv4_addresses": list(self.ipv4_addresses),
            "ipv6_addresses": list(self.ipv6_addresses),
            "identity": self.identity,
            "version": self.version,
            "board": self.board,
            "uptime_seconds": self.uptime_seconds,
            "interfaces": list(self.interfaces),
            "platform": self.platform,
            "software_id": self.software_id,
            "source_ip": self.source_ip,
            "authenticated": False,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    devices: tuple[DeviceInfo, ...]
    malformed_packets: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)
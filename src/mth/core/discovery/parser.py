import ipaddress
import struct
from enum import IntEnum

from mth.core.discovery.errors import MndpParseError
from mth.core.discovery.models import DeviceInfo, MndpField, MndpPacket


class MndpType(IntEnum):
    MAC_ADDRESS = 1
    IDENTITY = 5
    VERSION = 7
    PLATFORM = 8
    UPTIME = 10
    SOFTWARE_ID = 11
    BOARD = 12
    UNPACK = 14
    IPV6_ADDRESS = 15
    INTERFACE_NAME = 16
    IPV4_ADDRESS = 17


_STRING_TYPES = {
    MndpType.IDENTITY,
    MndpType.VERSION,
    MndpType.PLATFORM,
    MndpType.SOFTWARE_ID,
    MndpType.BOARD,
    MndpType.INTERFACE_NAME,
}
_ANNOUNCEMENT_TYPES = frozenset(
    field_type for field_type in MndpType if field_type is not MndpType.UNPACK
)

def parse_mndp_packet(data: bytes) -> MndpPacket:
    """Decode one strict MNDP datagram.

    RouterOS uses a four-byte little-endian sequence followed by TLVs whose type and length are
    unsigned 16-bit big-endian values. Unknown TLVs are retained as raw bytes.
    """

    if len(data) < 4:
        raise MndpParseError("MNDP datagram is shorter than the 4-byte sequence header")

    sequence = struct.unpack_from("<I", data, 0)[0]
    offset = 4
    fields: list[MndpField] = []

    while offset < len(data):
        if len(data) - offset < 4:
            raise MndpParseError(f"truncated TLV header at byte {offset}")

        type_id, value_length = struct.unpack_from(">HH", data, offset)
        offset += 4
        value_end = offset + value_length
        if value_end > len(data):
            raise MndpParseError(
                f"truncated TLV value for type {type_id}: expected {value_length} bytes"
            )

        raw_value = data[offset:value_end]
        offset = value_end
        fields.append(MndpField(type_id=type_id, value=_decode_value(type_id, raw_value)))

    return MndpPacket(sequence=sequence, fields=tuple(fields))

def packet_to_device(packet: MndpPacket, source_ip: str | None = None) -> DeviceInfo:
    values: dict[int, list[str | int | bytes]] = {}
    for item in packet.fields:
        values.setdefault(item.type_id, []).append(item.value)

    ipv4 = tuple(
        value
        for value in _unique_strings(values.get(MndpType.IPV4_ADDRESS, []))
        if _is_usable_ipv4(value)
    )
    if not ipv4 and source_ip and _is_usable_ipv4(source_ip):
        ipv4 = (source_ip,)

    return DeviceInfo(
        mac=_last_string(values, MndpType.MAC_ADDRESS),
        ipv4_addresses=ipv4,
        ipv6_addresses=_unique_strings(values.get(MndpType.IPV6_ADDRESS, [])),
        identity=_last_string(values, MndpType.IDENTITY),
        version=_last_string(values, MndpType.VERSION),
        board=_last_string(values, MndpType.BOARD),
        uptime_seconds=_last_integer(values, MndpType.UPTIME),
        interfaces=_unique_strings(values.get(MndpType.INTERFACE_NAME, [])),
        platform=_last_string(values, MndpType.PLATFORM),
        software_id=_last_string(values, MndpType.SOFTWARE_ID),
        source_ip=source_ip,
    )

def is_mndp_announcement(packet: MndpPacket) -> bool:
    """Return false for an echoed probe or a packet containing only unknown metadata."""

    return any(field.type_id in _ANNOUNCEMENT_TYPES for field in packet.fields)

def merge_devices(current: DeviceInfo, update: DeviceInfo) -> DeviceInfo:
    if current.key != update.key:
        raise ValueError("cannot merge different MNDP devices")

    prefer_update_interface = bool(update.ipv4_addresses) and not current.ipv4_addresses

    return DeviceInfo(
        mac=(update.mac if prefer_update_interface else current.mac) or update.mac,
        ipv4_addresses=_merge_unique(current.ipv4_addresses, update.ipv4_addresses),
        ipv6_addresses=_merge_unique(current.ipv6_addresses, update.ipv6_addresses),
        identity=update.identity or current.identity,
        version=update.version or current.version,
        board=update.board or current.board,
        uptime_seconds=(
            update.uptime_seconds
            if update.uptime_seconds is not None
            else current.uptime_seconds
        ),
        interfaces=_merge_unique(current.interfaces, update.interfaces),
        platform=update.platform or current.platform,
        software_id=update.software_id or current.software_id,
        source_ip=(update.source_ip if prefer_update_interface else current.source_ip)
        or update.source_ip,
    )

def _decode_value(type_id: int, raw_value: bytes) -> str | int | bytes:
    try:
        field_type = MndpType(type_id)
    except ValueError:
        return raw_value

    if field_type is MndpType.MAC_ADDRESS:
        if len(raw_value) != 6:
            raise MndpParseError(f"invalid MAC TLV length: {len(raw_value)}")
        return ":".join(f"{octet:02X}" for octet in raw_value)

    if field_type in _STRING_TYPES:
        return raw_value.decode("utf-8", errors="replace").rstrip("\x00")

    if field_type is MndpType.UPTIME:
        if len(raw_value) != 4:
            raise MndpParseError(f"invalid uptime TLV length: {len(raw_value)}")
        return int.from_bytes(raw_value, byteorder="little", signed=False)

    if field_type is MndpType.IPV4_ADDRESS:
        if len(raw_value) != 4:
            raise MndpParseError(f"invalid IPv4 TLV length: {len(raw_value)}")
        return str(ipaddress.IPv4Address(raw_value))

    if field_type is MndpType.IPV6_ADDRESS:
        if len(raw_value) != 16:
            raise MndpParseError(f"invalid IPv6 TLV length: {len(raw_value)}")
        return str(ipaddress.IPv6Address(raw_value))

    return raw_value


def _last_string(values: dict[int, list[str | int | bytes]], type_id: int) -> str | None:
    candidates = values.get(type_id, [])
    return candidates[-1] if candidates and isinstance(candidates[-1], str) else None


def _last_integer(values: dict[int, list[str | int | bytes]], type_id: int) -> int | None:
    candidates = values.get(type_id, [])
    return candidates[-1] if candidates and isinstance(candidates[-1], int) else None


def _unique_strings(values: list[str | int | bytes]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str)))


def _merge_unique(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))


def _is_usable_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return isinstance(address, ipaddress.IPv4Address) and not address.is_unspecified
    except ValueError:
        return False
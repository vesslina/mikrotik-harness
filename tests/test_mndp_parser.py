import ipaddress
import struct

import pytest

from mth.core.discovery.errors import MndpParseError
from mth.core.discovery.parser import (
    MndpType,
    is_mndp_announcement,
    merge_devices,
    packet_to_device,
    parse_mndp_packet,
)


def _tlv(type_id: int, value: bytes) -> bytes:
    return struct.pack(">HH", type_id, len(value)) + value


def _packet(*fields: bytes, sequence: int = 7) -> bytes:
    return struct.pack("<I", sequence) + b"".join(fields)


def test_parse_known_fields() -> None:
    raw = _packet(
        _tlv(MndpType.MAC_ADDRESS, bytes.fromhex("080027AFE23E")),
        _tlv(MndpType.IDENTITY, b"MikroTik-CHR"),
        _tlv(MndpType.VERSION, b"7.16.2 (stable)"),
        _tlv(MndpType.BOARD, b"CHR"),
        _tlv(MndpType.UPTIME, struct.pack("<I", 90_061)),
        _tlv(MndpType.INTERFACE_NAME, b"ether1"),
        _tlv(MndpType.IPV4_ADDRESS, ipaddress.IPv4Address("192.168.56.103").packed),
    )

    packet = parse_mndp_packet(raw)
    device = packet_to_device(packet, source_ip="192.168.56.103")

    assert packet.sequence == 7
    assert device.mac == "08:00:27:AF:E2:3E"
    assert device.identity == "MikroTik-CHR"
    assert device.version == "7.16.2 (stable)"
    assert device.board == "CHR"
    assert device.uptime_seconds == 90_061
    assert device.interfaces == ("ether1",)
    assert device.ipv4_addresses == ("192.168.56.103",)


def test_unknown_tlv_is_preserved_as_bytes() -> None:
    packet = parse_mndp_packet(_packet(_tlv(0xCAFE, b"opaque")))

    
    assert packet.fields[0].type_id == 0xCAFE
    assert packet.fields[0].value == b"opaque"


def test_active_probe_echo_is_not_a_device_announcement() -> None:
    packet = parse_mndp_packet(b"\x00\x00\x00\x00")

    assert packet.fields == ()
    assert is_mndp_announcement(packet) is False

@pytest.mark.parametrize(
    "payload, message",
    [
        (b"\x00\x00\x00", "sequence header"),
        (struct.pack("<I", 1) + b"\x00", "truncated TLV header"),
        (_packet(struct.pack(">HH", MndpType.IDENTITY, 4) + b"ab"), "truncated TLV value"),
        (_packet(_tlv(MndpType.MAC_ADDRESS, b"short")), "invalid MAC TLV length"),
        (_packet(_tlv(MndpType.IPV4_ADDRESS, b"bad")), "invalid IPv4 TLV length"),
    ],
)
def test_rejects_malformed_or_truncated_packets(payload: bytes, message: str) -> None:
    with pytest.raises(MndpParseError, match=message):
        parse_mndp_packet(payload)


def test_duplicate_device_announcements_are_merged() -> None:
    first = packet_to_device(
        parse_mndp_packet(
            _packet(
                _tlv(MndpType.MAC_ADDRESS, bytes.fromhex("080027AFE23E")),
                _tlv(MndpType.IDENTITY, b"chr"),
                _tlv(MndpType.INTERFACE_NAME, b"ether1"),
                _tlv(MndpType.IPV4_ADDRESS, ipaddress.IPv4Address("192.168.56.103").packed),
            )
        ),
        source_ip="192.168.56.103",
    )
    second = packet_to_device(
        parse_mndp_packet(
            _packet(
                _tlv(MndpType.MAC_ADDRESS, bytes.fromhex("080027AFE23E")),
                _tlv(MndpType.VERSION, b"7.16.2"),
                _tlv(MndpType.INTERFACE_NAME, b"bridge"),
                _tlv(MndpType.IPV4_ADDRESS, ipaddress.IPv4Address("10.0.0.1").packed),
            )
        ),
        source_ip="192.168.56.103",
    )

    merged = merge_devices(first, second)

    assert merged.identity == "chr"
    assert merged.version == "7.16.2"
    assert merged.interfaces == ("ether1", "bridge")
    assert merged.ipv4_addresses == ("192.168.56.103", "10.0.0.1")


def test_same_router_interfaces_merge_by_software_id_and_keep_connectable_mac() -> None:
    no_ip_interface = packet_to_device(
        parse_mndp_packet(
            _packet(
                _tlv(MndpType.MAC_ADDRESS, bytes.fromhex("080027BEDB9F")),
                _tlv(MndpType.SOFTWARE_ID, b"vfGBUYu42WL"),
                _tlv(MndpType.INTERFACE_NAME, b"ether2"),
                _tlv(MndpType.IPV4_ADDRESS, ipaddress.IPv4Address("0.0.0.0").packed),
            )
        ),
        source_ip="0.0.0.0",
    )
    ip_interface = packet_to_device(
        parse_mndp_packet(
            _packet(
                _tlv(MndpType.MAC_ADDRESS, bytes.fromhex("080027AFE23E")),
                _tlv(MndpType.SOFTWARE_ID, b"vfGBUYu42WL"),
                _tlv(MndpType.INTERFACE_NAME, b"ether1"),
                _tlv(MndpType.IPV4_ADDRESS, ipaddress.IPv4Address("192.168.56.103").packed),
            )
        ),
        source_ip="192.168.56.103",
    )

    merged = merge_devices(no_ip_interface, ip_interface)

    assert merged.key == "software:vfGBUYu42WL"
    assert merged.mac == "08:00:27:AF:E2:3E"
    assert merged.ipv4_addresses == ("192.168.56.103",)
    assert merged.interfaces == ("ether2", "ether1")
    assert merged.source_ip == "192.168.56.103"

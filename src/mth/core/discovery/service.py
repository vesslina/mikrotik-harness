import socket
import time
from collections.abc import Iterable

from mth.core.discovery.errors import DiscoveryError, DiscoveryErrorCode, MndpParseError
from mth.core.discovery.models import DeviceInfo, DiscoveryResult
from mth.core.discovery.parser import (
    is_mndp_announcement,
    merge_devices,
    packet_to_device,
    parse_mndp_packet,
)

MNDP_PORT = 5678
MNDP_PROBE = b"\x00\x00\x00\x00"
MAX_DATAGRAM_SIZE = 65535

def discover_devices(
    *,
    timeout: float = 3.0,
    bind_address: str = "0.0.0.0",
    broadcasts: Iterable[str] | None = None,
    port: int = MNDP_PORT,
    active: bool = True,
    probe_count: int = 3,
    probe_interval: float = 0.05,
) -> DiscoveryResult:
    """Actively probe for MNDP neighbors and collect announcements until the deadline."""

    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if probe_count < 1:
        raise ValueError("probe_count must be at least one")

    destinations = None if broadcasts is None else tuple(dict.fromkeys(broadcasts))
    if active and destinations == ():
        raise ValueError("at least one broadcast address is required for an active probe")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((bind_address, port))

        if active:
            source_addresses = (
                (bind_address,) if bind_address != "0.0.0.0" else _local_ipv4_addresses()
            )
            _send_probes(
                sock,
                destinations,
                source_addresses,
                port,
                probe_count,
                probe_interval,
            )

        deadline = time.monotonic() + timeout
        devices: dict[str, DeviceInfo] = {}
        malformed_packets = 0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                payload, source = sock.recvfrom(MAX_DATAGRAM_SIZE)
            except TimeoutError:
                break

            try:
                packet = parse_mndp_packet(payload)
            except MndpParseError:
                malformed_packets += 1
                continue

            # Windows can loop our four-zero probe back to the receiving socket. The probe has a
            # valid sequence header but no TLVs, so it must not become an anonymous device row.
            if not is_mndp_announcement(packet):
                continue

            device = packet_to_device(packet, source_ip=source[0])

            previous = devices.get(device.key)
            devices[device.key] = merge_devices(previous, device) if previous else device

        ordered = tuple(
            sorted(
                devices.values(),
                key=lambda device: (
                    device.identity or "",
                    device.mac or "",
                    device.source_ip or "",
                ),
            )
        )
        warnings = (
            (f"Ignored {malformed_packets} malformed MNDP datagram(s)",)
            if malformed_packets
            else ()
        )
        return DiscoveryResult(
            devices=ordered,
            malformed_packets=malformed_packets,
            warnings=warnings,
        )
    except OSError as error:
        raise DiscoveryError(
            DiscoveryErrorCode.SOCKET_ERROR,
            f"MNDP socket failed on {bind_address}:{port}: {error}",
        ) from error
    finally:
        sock.close()

def _send_probes(
    listener: socket.socket,
    destinations: tuple[str, ...] | None,
    source_addresses: tuple[str, ...],
    port: int,
    probe_count: int,
    probe_interval: float,
) -> None:
    if destinations is not None:
        _transmit_probes(
            ((listener, destination) for destination in destinations),
            port,
            probe_count,
            probe_interval,
        )
        return

    # A limited broadcast sent from 0.0.0.0 on multi-homed Windows follows only the default route.
    # Binding one short-lived sender per local address makes the same broadcast reach every NIC
    # without guessing its subnet prefix length or adding an interface-enumeration dependency.
    senders: list[socket.socket] = []
    try:
        for source_address in source_addresses:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sender.bind((source_address, 0))
            except OSError:
                sender.close()
                continue
            senders.append(sender)

        if not senders:
            senders.append(listener)

        _transmit_probes(
            ((sender, "255.255.255.255") for sender in senders),
            port,
            probe_count,
            probe_interval,
        )
    finally:
        for sender in senders:
            if sender is not listener:
                sender.close()


def _transmit_probes(
    routes: Iterable[tuple[socket.socket, str]],
    port: int,
    probe_count: int,
    probe_interval: float,
) -> None:
    resolved_routes = tuple(routes)
    for attempt in range(probe_count):
        for sender, destination in resolved_routes:
            sender.sendto(MNDP_PROBE, (destination, port))
        if attempt + 1 < probe_count and probe_interval > 0:
            time.sleep(probe_interval)


def _local_ipv4_addresses() -> tuple[str, ...]:
    addresses = (
        str(result[4][0])
        for result in socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        )
    )
    return tuple(
        address
        for address in dict.fromkeys(addresses)
        if address not in {"0.0.0.0", "127.0.0.1"}
    )

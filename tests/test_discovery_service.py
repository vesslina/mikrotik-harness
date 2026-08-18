import socket

from mth.core.discovery import service


def test_local_ipv4_addresses_are_unique_and_skip_unspecified(monkeypatch) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "test-host")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("192.168.56.1", 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("192.168.56.1", 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("0.0.0.0", 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("127.0.0.1", 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("192.168.1.10", 0)),
        ],
    )

    assert service._local_ipv4_addresses() == ("192.168.56.1", "192.168.1.10")


def test_probe_is_the_four_byte_discovery_request() -> None:
    assert bytes(4) == service.MNDP_PROBE
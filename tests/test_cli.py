import json

from mth import cli
from mth.core.discovery.models import DeviceInfo, DiscoveryResult


def test_cli_json_output(monkeypatch, capsys) -> None:
    device = DeviceInfo(
        mac="08:00:27:AF:E2:3E",
        ipv4_addresses=("192.168.56.103",),
        identity="chr",
        version="7.16.2",
        board="CHR",
        interfaces=("ether1",),
        source_ip="192.168.56.103",
    )
    monkeypatch.setattr(
        cli,
        "discover_devices",
        lambda **_kwargs: DiscoveryResult(devices=(device,)),
    )

    assert cli.main(["discover", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["devices"][0]["mac"] == "08:00:27:AF:E2:3E"
    assert output["devices"][0]["authenticated"] is False


def test_cli_returns_stable_timeout_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "discover_devices",
        lambda **_kwargs: DiscoveryResult(devices=()),
    )

    assert cli.main(["discover", "--timeout", "0.1"]) == 2
    assert "DISCOVERY_TIMEOUT" in capsys.readouterr().err


def test_cli_launches_tui_by_default(monkeypatch) -> None:
    received: dict[str, object] = {}

    def fake_run_tui(**kwargs: object) -> int:
        received.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_tui", fake_run_tui)

    assert cli.main([]) == 0
    assert received == {
        "timeout": 3.0,
        "bind_address": "0.0.0.0",
        "broadcasts": None,
        "port": 5678,
        "active": True,
    }

import asyncio

from textual.widgets import DataTable, Input, Static

from mth.core.discovery.models import DeviceInfo, DiscoveryResult
from mth.ui.textual.app import DiscoveryApp


def _device() -> DeviceInfo:
    return DeviceInfo(
        mac="08:00:27:AF:E2:3E",
        ipv4_addresses=("192.168.56.103",),
        identity="MikroTik",
        version="7.21.5",
        board="CHR",
        interfaces=("ether1", "ether2"),
        software_id="vfGBUYu42WL",
        source_ip="192.168.56.103",
    )


def test_discovery_populates_table_and_selection() -> None:
    async def scenario() -> None:
        device = _device()
        app = DiscoveryApp(
            discoverer=lambda **_kwargs: DiscoveryResult(devices=(device,)),
            timeout=0.1,
        )

        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = app.query_one("#devices", DataTable)
            assert table.row_count == 1
            assert "Selected MikroTik" in str(app.query_one("#status", Static).content)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()

            assert app.query_one("#connect-to", Input).value == "192.168.56.103"
            assert app.query_one("#password", Input).password is True
            assert "Selected MikroTik" in str(app.query_one("#status", Static).content)

    asyncio.run(scenario())


def test_refresh_runs_discovery_again() -> None:
    async def scenario() -> None:
        calls = 0

        def discover(**_kwargs: object) -> DiscoveryResult:
            nonlocal calls
            calls += 1
            return DiscoveryResult(devices=(_device(),))

        app = DiscoveryApp(discoverer=discover, timeout=0.1)

        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("r")
            await app.workers.wait_for_complete()
            assert calls == 2

    asyncio.run(scenario())


def test_connect_form_reports_backend_boundary() -> None:
    async def scenario() -> None:
        app = DiscoveryApp(
            discoverer=lambda **_kwargs: DiscoveryResult(devices=()),
            timeout=0.1,
        )

        async with app.run_test(size=(120, 40)):
            await app.workers.wait_for_complete()
            app.query_one("#connect-to", Input).value = "192.168.56.103"
            app.action_connect()

            status = str(app.query_one("#status", Static).content)
            assert "Connection target ready" in status
            assert "MikroMCP registration is the next Block A slice" in status

    asyncio.run(scenario())

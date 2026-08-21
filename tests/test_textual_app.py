import asyncio

from textual.widgets import Button, DataTable, Input, Static

from mth.core.discovery.models import DeviceInfo, DiscoveryResult
from mth.core.registration import PendingRegistration, RegistrationResult
from mth.ui.textual import clipboard as system_clipboard
from mth.ui.textual.app import DiscoveryApp
from mth.ui.textual.chat import ChatScreen
from mth.ui.textual.i18n import Language


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
            language=Language.EN,
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

        app = DiscoveryApp(discoverer=discover, timeout=0.1, language=Language.EN)

        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("r")
            await app.workers.wait_for_complete()
            assert calls == 2

    asyncio.run(scenario())


def test_discovery_shows_each_address_of_one_router() -> None:
    async def scenario() -> None:
        device = DeviceInfo(
            mac="08:00:27:AF:E2:3E",
            ipv4_addresses=("172.20.20.1", "192.168.56.103"),
            identity="MikroTik",
            version="7.21.5",
            board="CHR",
            interfaces=("ether1", "ether2"),
            software_id="vfGBUYu42WL",
            source_ip="172.20.20.1",
        )
        app = DiscoveryApp(
            discoverer=lambda **_kwargs: DiscoveryResult(devices=(device,)),
            timeout=0.1,
            language=Language.EN,
        )

        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            table = app.query_one("#devices", DataTable)
            assert table.row_count == 2
            table.move_cursor(row=1)
            await pilot.press("enter")
            assert app.query_one("#connect-to", Input).value == "192.168.56.103"

    asyncio.run(scenario())


def test_connect_requires_password() -> None:
    async def scenario() -> None:
        app = DiscoveryApp(
            discoverer=lambda **_kwargs: DiscoveryResult(devices=()),
            timeout=0.1,
            language=Language.EN,
        )

        async with app.run_test(size=(120, 40)):
            await app.workers.wait_for_complete()
            app.query_one("#connect-to", Input).value = "192.168.56.103"
            app.action_connect()

            status = str(app.query_one("#status", Static).content)
            assert "non-empty RouterOS password" in status

    asyncio.run(scenario())


def test_discovery_uses_russian_ui_when_selected() -> None:
    async def scenario() -> None:
        app = DiscoveryApp(
            discoverer=lambda **_kwargs: DiscoveryResult(devices=()),
            timeout=0.1,
            language=Language.RU,
        )

        async with app.run_test(size=(120, 40)):
            await app.workers.wait_for_complete()

            assert app.sub_title == "Поиск и выбор устройства RouterOS"
            assert str(app.query_one("#connect", Button).label) == "Подключиться"
            assert "Устройства не найдены" in str(
                app.query_one("#status", Static).content
            )
            assert (
                str(app.query_one("#trust-fingerprint-inline", Button).label)
                == "Доверять и подключиться"
            )

    asyncio.run(scenario())


def test_app_bridges_textual_and_system_clipboards(monkeypatch) -> None:
    written: list[str] = []
    monkeypatch.setattr(system_clipboard, "read_text", lambda: "из Windows clipboard")
    monkeypatch.setattr(system_clipboard, "write_text", lambda value: written.append(value))
    app = DiscoveryApp(
        discoverer=lambda **_kwargs: DiscoveryResult(devices=()),
        language=Language.RU,
    )

    assert app.clipboard == "из Windows clipboard"
    app.copy_to_clipboard("текст из mth")
    assert written == ["текст из mth"]


class _FakeRegistrar:
    def __init__(self, *, already_trusted: bool) -> None:
        self.already_trusted = already_trusted
        self.registered = False

    def prepare(self, **kwargs: object) -> PendingRegistration:
        return PendingRegistration(
            router_id="mikrotik-afe23e",
            host=str(kwargs["host"]),
            port=443,
            username=str(kwargs["username"]),
            password=str(kwargs["password"]),
            ros_version="7.21.5",
            tls_fingerprint="ab" * 32,
            identity="MikroTik",
            trusted_fingerprint=self.already_trusted,
        )

    async def register_and_verify(
        self, pending: PendingRegistration
    ) -> RegistrationResult:
        self.registered = True
        return RegistrationResult(
            router_id=pending.router_id,
            identity="MikroTik",
            tool_count=122,
            health={"healthy": True, "rosVersion": "7.21.5"},
            system_status={
                "sections": {
                    "resource": {"version": "7.21.5", "cpu-load": "3"},
                    "identity": {"name": "MikroTik"},
                }
            },
        )


def test_first_connection_requires_fingerprint_confirmation() -> None:
    async def scenario() -> None:
        registrar = _FakeRegistrar(already_trusted=False)
        app = DiscoveryApp(
            discoverer=lambda **_kwargs: DiscoveryResult(devices=(_device(),)),
            registrar=registrar,
            timeout=0.1,
            language=Language.EN,
        )

        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one("#password", Input).value = "secret"
            app.action_connect()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app.query_one("#fingerprint-inline").display is True
            assert app.query_one("#connection").display is False
            assert registrar.registered is False
            await pilot.click("#trust-fingerprint-inline")
            await pilot.pause()
            await app.workers.wait_for_complete()

            assert registrar.registered is True
            assert isinstance(app.screen, ChatScreen)
            assert "Connected to MikroTik" in str(app.query_one("#status", Static).content)
            assert "Live MCP tools: 122" in str(
                app.query_one("#backend-status", Static).content
            )
            assert app.query_one("#password", Input).value == ""

    asyncio.run(scenario())

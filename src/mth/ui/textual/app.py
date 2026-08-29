import asyncio
from collections.abc import Callable, Iterable
from contextlib import suppress
from typing import Protocol

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from mth.agent.secret_store import ProviderSecretPaths, ProviderSecretStore
from mth.core.discovery import DiscoveryError, discover_devices
from mth.core.discovery.models import DeviceInfo, DiscoveryResult
from mth.core.registration import (
    PendingRegistration,
    RegistrationError,
    RegistrationErrorCode,
    RegistrationResult,
    RegistrationService,
)
from mth.ui.textual import clipboard as system_clipboard
from mth.ui.textual.chat import ChatProfile, ChatScreen
from mth.ui.textual.i18n import Language, UiSettingsStore, tr

Discoverer = Callable[..., DiscoveryResult]


class Registrar(Protocol):
    def prepare(
        self,
        *,
        host: str,
        username: str,
        password: str,
        device: DeviceInfo | None = None,
        port: int = 443,
    ) -> PendingRegistration: ...

    async def register_and_verify(
        self, pending: PendingRegistration
    ) -> RegistrationResult: ...


class FingerprintScreen(ModalScreen[bool]):
    """Explicit trust-on-first-use gate for a RouterOS TLS certificate."""

    CSS = """
    FingerprintScreen {
        align: center middle;
    }

    #fingerprint-dialog {
        width: 86;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }

    #fingerprint-value {
        margin: 1 0;
        color: $warning;
    }

    #fingerprint-actions {
        height: auto;
        align-horizontal: right;
    }

    #fingerprint-actions Button {
        margin-left: 1;
    }
    """

    def __init__(self, pending: PendingRegistration) -> None:
        super().__init__()
        self._pending = pending

    def compose(self) -> ComposeResult:
        with Vertical(id="fingerprint-dialog"):
            yield Static(
                "First connection: verify this RouterOS TLS SHA-256 fingerprint "
                "against a trusted source before accepting.",
                markup=False,
            )
            yield Static(self._pending.display_fingerprint, id="fingerprint-value", markup=False)
            yield Static(
                f"Target: {self._pending.host}:{self._pending.port}",
                markup=False,
            )
            with Center(id="fingerprint-actions"):
                yield Button("Cancel", id="reject-fingerprint")
                yield Button("Trust and connect", id="trust-fingerprint", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "trust-fingerprint")


class DiscoveryApp(App[None]):
    """Block A terminal UI for discovery and connection target selection."""

    TITLE = "MikroTik Harness"
    SUB_TITLE = "Discovery and select a RouterOS device"

    CSS = """
    Screen {
        layout: vertical;
        background: #090909;
        color: #f0f0f0;
    }

    Header { background: #090909; color: white; border-bottom: solid #ff3b30; }
    Header .header--title { color: white; }
    Header .header--subtitle { color: #aeb4ba; }
    Footer { background: #111315; color: #aeb4ba; }
    Footer > .footer--highlight { background: #ff3b30; color: white; }

    #status {
        height: 3;
        padding: 1 2;
        background: #111315;
        color: #f0f0f0;
    }

    #devices {
        height: 1fr;
        min-height: 8;
        margin: 0 1;
        border: round #ff3b30;
        background: #090909;
        color: white;
    }
    DataTable > .datatable--header { background: #1b1b1b; color: white; text-style: bold; }
    DataTable > .datatable--cursor { background: #5a1717; color: white; }
    DataTable > .datatable--hover { background: #2b2b2b; color: white; }

    #connection {
        height: auto;
        padding: 1 2;
        border-top: solid #ff3b30;
        background: #090909;
    }

    #connection-fields {
        height: auto;
    }

    .connection-field {
        width: 1fr;
        height: auto;
        margin-right: 1;
    }

    .connection-field:last-child {
        margin-right: 0;
    }

    .connection-field Label {
        margin-bottom: 1;
    }

    #connect {
        width: 100%;
        height: 3;
        min-height: 3;
        margin-top: 1;
        background: #ff3b30;
        color: white;
        border: none;
    }
    #connection Input { background: #1b1b1b; color: white; border: solid #363636; }
    #connection Input:focus { border: solid #ff3b30; }
    #backend-status { color: #aeb4ba; }
    #fingerprint-inline {
        display: none;
        height: auto;
        padding: 1 3;
        border-top: solid #ff3b30;
        background: #090909;
    }
    #fingerprint-inline-title { height: 2; color: white; text-style: bold; }
    #fingerprint-inline-value { height: auto; margin: 1 0; color: #ff8a73; }
    #fingerprint-inline-help { height: 2; color: #8b949e; }
    #fingerprint-inline-actions { height: auto; align-horizontal: right; }
    #fingerprint-inline-actions Button { margin-left: 1; }
    #trust-fingerprint-inline { background: #ff3b30; color: white; }
    """

    def __init__(
        self,
        *,
        discoverer: Discoverer = discover_devices,
        timeout: float = 3.0,
        bind_address: str = "0.0.0.0",
        broadcasts: Iterable[str] | None = None,
        port: int = 5678,
        active: bool = True,
        registrar: Registrar | None = None,
        settings_store: UiSettingsStore | None = None,
        language: Language | None = None,
        credential_store: ProviderSecretStore | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings_store or UiSettingsStore()
        self._credential_store = credential_store or ProviderSecretStore(
            ProviderSecretPaths(
                file=self._settings.paths.file.with_name("discovery-secrets.json"),
                key_file=self._settings.paths.file.with_name("discovery-secrets.key"),
            )
        )
        self.language = language or self._settings.language()
        self.sub_title = tr(self.language, "discovery.subtitle")
        self.bind("r", "refresh", description=tr(self.language, "discovery.refresh"))
        self.bind("q", "quit", description=tr(self.language, "discovery.quit"))
        self._discoverer = discoverer
        self._timeout = timeout
        self._bind_address = bind_address
        self._broadcasts = tuple(broadcasts) if broadcasts is not None else None
        self._port = port
        self._active = active
        self._registrar = registrar or RegistrationService()
        self._devices: dict[str, DeviceInfo] = {}
        self._device_rows: dict[str, tuple[DeviceInfo, str]] = {}
        self._selected_device: DeviceInfo | None = None
        self._discovery_generation = 0
        self._pending_fingerprint: PendingRegistration | None = None

    @property
    def clipboard(self) -> str:
        """Prefer the OS clipboard when a terminal sends Ctrl+V as a key."""

        try:
            system_value = system_clipboard.read_text()
        except (OSError, ValueError):
            system_value = None
        return super().clipboard if system_value is None else system_value

    def copy_to_clipboard(self, text: str) -> None:
        """Keep Textual's OSC52 clipboard and the Windows clipboard in sync."""

        super().copy_to_clipboard(text)
        with suppress(OSError, ValueError):
            system_clipboard.write_text(text)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(tr(self.language, "discovery.ready"), id="status", markup=False)
        yield DataTable(id="devices", zebra_stripes=True)
        with Vertical(id="connection"):
            with Horizontal(id="connection-fields"):
                with Vertical(classes="connection-field"):
                    yield Label(tr(self.language, "discovery.connect_to"))
                    yield Input(
                        value=self._settings.last_address(),
                        placeholder=tr(self.language, "discovery.address"),
                        id="connect-to",
                    )
                with Vertical(classes="connection-field"):
                    yield Label(tr(self.language, "discovery.login"))
                    yield Input(
                        value="admin",
                        placeholder=tr(self.language, "discovery.login_placeholder"),
                        id="login",
                    )
                with Vertical(classes="connection-field"):
                    yield Label(tr(self.language, "discovery.password"))
                    yield Input(
                        password=True,
                        value=self._saved_password(),
                        placeholder=tr(self.language, "discovery.password_placeholder"),
                        id="password",
                    )
            yield Button(tr(self.language, "discovery.connect"), id="connect")
            yield Static(
                tr(self.language, "discovery.backend_idle"),
                id="backend-status",
                markup=False,
            )
        with Vertical(id="fingerprint-inline"):
            yield Static("", id="fingerprint-inline-title")
            yield Static("", id="fingerprint-inline-body", markup=False)
            yield Static("", id="fingerprint-inline-value", markup=False)
            yield Static("", id="fingerprint-inline-target", markup=False)
            with Horizontal(id="fingerprint-inline-actions"):
                yield Button(tr(self.language, "inline.cancel"), id="reject-fingerprint-inline")
                yield Button(tr(self.language, "discovery.trust"), id="trust-fingerprint-inline")
            yield Static(tr(self.language, "inline.help"), id="fingerprint-inline-help")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#devices", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "MAC",
            "IP",
            "Устройство" if self.language is Language.RU else "Identity",
            "Версия" if self.language is Language.RU else "Version",
            "Плата" if self.language is Language.RU else "Board",
        )
        table.focus()
        self.action_refresh()

    def action_refresh(self) -> None:
        self._discovery_generation += 1
        self._set_status(tr(self.language, "discovery.searching"))
        self._discover(self._discovery_generation)

    @work(thread=True, exclusive=True, group="discovery", exit_on_error=False)
    def _discover(self, generation: int) -> None:
        try:
            result = self._discoverer(
                timeout=self._timeout,
                bind_address=self._bind_address,
                broadcasts=self._broadcasts,
                port=self._port,
                active=self._active,
            )
        except (DiscoveryError, ValueError) as error:
            self.call_from_thread(self._show_discovery_error, str(error), generation)
            return
        except Exception as error:  # defensive UI boundary: keep the terminal usable
            self.call_from_thread(
                self._show_discovery_error,
                f"Unexpected discovery error: {error}",
                generation,
            )
            return

        self.call_from_thread(self._apply_result, result, generation)

    def _apply_result(self, result: DiscoveryResult, generation: int) -> None:
        if generation != self._discovery_generation:
            return

        table = self.query_one("#devices", DataTable)
        table.clear(columns=False)
        self._devices = {device.key: device for device in result.devices}
        self._device_rows = {}

        for device in result.devices:
            addresses = self._device_addresses(device) or ("—",)
            for index, address in enumerate(addresses):
                row_key = f"{device.key}:{index}"
                self._device_rows[row_key] = (device, address)
                table.add_row(
                    device.mac or "—",
                    address,
                    device.identity or "—",
                    device.version or "—",
                    device.board or "—",
                    key=row_key,
                )

        if result.devices:
            warning_suffix = f" {result.warnings[0]}" if result.warnings else ""
            self._set_status(
                tr(
                    self.language,
                    "discovery.found",
                    count=len(result.devices),
                    warning=warning_suffix,
                )
            )
            table.focus()
        else:
            self._set_status(tr(self.language, "discovery.none"))

    def _show_discovery_error(self, message: str, generation: int) -> None:
        if generation != self._discovery_generation:
            return
        self._set_status(tr(self.language, "discovery.error", message=message))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "devices":
            self._select_device(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "devices":
            self._select_device(str(event.row_key.value))

    def _select_device(self, key: str) -> None:
        selected = self._device_rows.get(key)
        if selected is None:
            return
        device, address = selected
        if address != "—":
            self._selected_device = device
            self.query_one("#connect-to", Input).value = address
            self._set_status(
                tr(
                    self.language,
                    "discovery.selected",
                    identity=device.identity or device.mac or address,
                    address=address,
                )
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect":
            self.action_connect()
        elif event.button.id == "trust-fingerprint-inline":
            self._fingerprint_decided_inline(True)
        elif event.button.id == "reject-fingerprint-inline":
            self._fingerprint_decided_inline(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in {"connect-to", "login", "password"}:
            self.action_connect()

    def action_connect(self) -> None:
        target = self.query_one("#connect-to", Input).value.strip()
        login = self.query_one("#login", Input).value.strip()

        if not target:
            self._set_status(tr(self.language, "discovery.need_address"))
            return
        if not login:
            self._set_status(tr(self.language, "discovery.need_login"))
            return
        password = self.query_one("#password", Input).value
        if not password:
            self._set_status(tr(self.language, "discovery.need_password"))
            return

        self._remember_connection(target, password)

        device = self._selected_device
        if device is not None and target not in self._device_addresses(device):
            device = None
        self._set_connecting(True)
        self._set_status(tr(self.language, "discovery.connecting", target=target))
        self._prepare_registration(target, login, password, device)

    @work(thread=True, exclusive=True, group="registration", exit_on_error=False)
    def _prepare_registration(
        self,
        target: str,
        login: str,
        password: str,
        device: DeviceInfo | None,
    ) -> None:
        try:
            pending = self._registrar.prepare(
                host=target,
                username=login,
                password=password,
                device=device,
            )
        except RegistrationError as error:
            self.call_from_thread(self._show_registration_error, error)
            return
        except Exception as error:  # defensive UI boundary
            wrapped = RegistrationError(
                RegistrationErrorCode.BACKEND_UNAVAILABLE,
                f"Unexpected registration error: {error}",
            )
            self.call_from_thread(self._show_registration_error, wrapped)
            return
        self.call_from_thread(self._review_fingerprint, pending)

    def _review_fingerprint(self, pending: PendingRegistration) -> None:
        if pending.trusted_fingerprint:
            self._verify_registration(pending)
            return
        self._set_status(tr(self.language, "discovery.fingerprint_captured"))
        self._pending_fingerprint = pending
        self.query_one("#connection").display = False
        self.query_one("#fingerprint-inline").display = True
        self.query_one("#fingerprint-inline-title", Static).update(
            tr(self.language, "discovery.fingerprint_title")
        )
        self.query_one("#fingerprint-inline-body", Static).update(
            tr(self.language, "discovery.fingerprint_body")
        )
        self.query_one("#fingerprint-inline-value", Static).update(
            pending.display_fingerprint
        )
        self.query_one("#fingerprint-inline-target", Static).update(
            f'{tr(self.language, "discovery.target")}: {pending.host}:{pending.port}'
        )
        self.query_one("#trust-fingerprint-inline", Button).focus()

    def _fingerprint_decided_inline(self, trusted: bool) -> None:
        pending = self._pending_fingerprint
        self._pending_fingerprint = None
        self.query_one("#fingerprint-inline").display = False
        self.query_one("#connection").display = True
        if pending is not None:
            self._fingerprint_decided(pending, trusted)

    def _fingerprint_decided(self, pending: PendingRegistration, trusted: bool) -> None:
        if not trusted:
            self._set_connecting(False)
            self._set_status(tr(self.language, "discovery.cancelled"))
            return
        self._set_status(tr(self.language, "discovery.registering"))
        self._verify_registration(pending)

    @work(thread=True, exclusive=True, group="registration", exit_on_error=False)
    def _verify_registration(self, pending: PendingRegistration) -> None:
        try:
            result = asyncio.run(self._registrar.register_and_verify(pending))
        except RegistrationError as error:
            self.call_from_thread(self._show_registration_error, error)
            return
        except Exception as error:  # defensive UI boundary
            wrapped = RegistrationError(
                RegistrationErrorCode.BACKEND_HEALTH_FAILED,
                f"Unexpected backend error: {error}",
            )
            self.call_from_thread(self._show_registration_error, wrapped)
            return
        self.call_from_thread(self._show_connected, result, pending)

    def _show_connected(
        self,
        result: RegistrationResult,
        pending: PendingRegistration,
    ) -> None:
        self._set_connecting(False)
        self._set_status(
            tr(self.language, "discovery.connected", identity=result.identity)
        )
        status = result.system_status
        sections = status.get("sections", {})
        resource = sections.get("resource", {}) if isinstance(sections, dict) else {}
        cpu = resource.get("cpu-load", "?") if isinstance(resource, dict) else "?"
        version = (
            resource.get("version", result.health.get("rosVersion", "?"))
            if isinstance(resource, dict)
            else result.health.get("rosVersion", "?")
        )
        self.query_one("#backend-status", Static).update(
            tr(
                self.language,
                "discovery.backend_connected",
                router_id=result.router_id,
                version=version,
                cpu=cpu,
                tool_count=result.tool_count,
            )
        )
        device = self._selected_device
        if device is not None and pending.host not in self._device_addresses(device):
            device = None
        profile = ChatProfile(
            router_id=result.router_id,
            address=pending.host,
            identity=result.identity,
            version=str(version),
            board=device.board if device and device.board else "RouterOS",
            mac=device.mac if device and device.mac else "—",
            tool_count=result.tool_count,
            port=pending.port,
        )
        self.push_screen(
            ChatScreen(
                profile,
                result,
                settings_store=self._settings,
                reachability_check=True,
                language=self.language,
            )
        )

    def _show_registration_error(self, error: RegistrationError) -> None:
        self._set_connecting(False)
        self._set_status(f"{error.code}: {error}")

    def _set_connecting(self, connecting: bool) -> None:
        self.query_one("#connect", Button).disabled = connecting

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def _saved_password(self) -> str:
        try:
            return self._credential_store.get("last-routeros") or ""
        except (OSError, RuntimeError, ValueError):
            return ""

    def _remember_connection(self, address: str, password: str) -> None:
        try:
            self._settings.save_last_address(address)
            self._credential_store.set("last-routeros", password)
        except (OSError, RuntimeError, ValueError):
            # Connection must remain usable even if the local credential vault is unavailable.
            pass

    @staticmethod
    def _device_addresses(device: DeviceInfo) -> tuple[str, ...]:
        """Keep every discovered IPv4 target visible, including a source-only reply."""

        addresses = list(device.ipv4_addresses)
        if device.source_ip and device.source_ip not in addresses:
            addresses.append(device.source_ip)
        return tuple(addresses)

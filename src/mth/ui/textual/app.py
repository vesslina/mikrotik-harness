from collections.abc import Callable, Iterable

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from mth.core.discovery import DiscoveryError, discover_devices
from mth.core.discovery.models import DeviceInfo, DiscoveryResult

Discoverer = Callable[..., DiscoveryResult]


class DiscoveryApp(App[None]):
    """Block A terminal UI for discovery and connection target selection."""

    TITLE = "MikroTik Harness"
    SUB_TITLE = "Block A — discover and select a RouterOS device"

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }

    #status {
        height: 3;
        padding: 1 2;
        background: $panel;
        color: $text;
    }

    #devices {
        height: 1fr;
        min-height: 8;
        margin: 0 1;
        border: round $accent;
    }

    #connection {
        height: auto;
        padding: 1 2;
        border-top: solid $accent;
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
        margin-top: 1;
    }
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
    ) -> None:
        super().__init__()
        self._discoverer = discoverer
        self._timeout = timeout
        self._bind_address = bind_address
        self._broadcasts = tuple(broadcasts) if broadcasts is not None else None
        self._port = port
        self._active = active
        self._devices: dict[str, DeviceInfo] = {}
        self._discovery_generation = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Ready to search for MNDP neighbors.", id="status", markup=False)
        yield DataTable(id="devices", zebra_stripes=True)
        with Vertical(id="connection"):
            with Horizontal(id="connection-fields"):
                with Vertical(classes="connection-field"):
                    yield Label("Connect to")
                    yield Input(placeholder="IP address or hostname", id="connect-to")
                with Vertical(classes="connection-field"):
                    yield Label("Login")
                    yield Input(value="admin", placeholder="RouterOS login", id="login")
                with Vertical(classes="connection-field"):
                    yield Label("Password")
                    yield Input(password=True, placeholder="RouterOS password", id="password")
            yield Button("Connect", id="connect", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#devices", DataTable)
        table.cursor_type = "row"
        table.add_columns("MAC", "IP", "Identity", "Version", "Board")
        table.focus()
        self.action_refresh()

    def action_refresh(self) -> None:
        self._discovery_generation += 1
        self._set_status("Searching for MNDP neighbors…")
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

        for device in result.devices:
            table.add_row(
                device.mac or "—",
                self._device_address(device) or "—",
                device.identity or "—",
                device.version or "—",
                device.board or "—",
                key=device.key,
            )

        if result.devices:
            warning_suffix = f" {result.warnings[0]}" if result.warnings else ""
            self._set_status(
                f"Found {len(result.devices)} device(s). Select a row or enter an address."
                f"{warning_suffix}"
            )
            table.focus()
        else:
            self._set_status(
                "No devices found. Press r to retry or enter an address manually."
            )

    def _show_discovery_error(self, message: str, generation: int) -> None:
        if generation != self._discovery_generation:
            return
        self._set_status(f"Discovery error: {message}")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "devices":
            self._select_device(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "devices":
            self._select_device(str(event.row_key.value))

    def _select_device(self, key: str) -> None:
        device = self._devices.get(key)
        if device is None:
            return
        address = self._device_address(device)
        if address:
            self.query_one("#connect-to", Input).value = address
            self._set_status(
                f"Selected {device.identity or device.mac or address} at {address}."
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect":
            self.action_connect()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in {"connect-to", "login", "password"}:
            self.action_connect()

    def action_connect(self) -> None:
        target = self.query_one("#connect-to", Input).value.strip()
        login = self.query_one("#login", Input).value.strip()

        if not target:
            self._set_status("Enter a RouterOS address or select a discovered device.")
            return
        if not login:
            self._set_status("Enter a RouterOS login.")
            return

        self._set_status(
            f"Connection target ready: {target} as {login}. "
            "MikroMCP registration is the next Block A slice."
        )

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    @staticmethod
    def _device_address(device: DeviceInfo) -> str:
        if device.ipv4_addresses:
            return device.ipv4_addresses[0]
        return device.source_ip or ""

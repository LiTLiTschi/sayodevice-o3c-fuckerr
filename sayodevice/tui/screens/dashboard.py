"""Dashboard screen — main landing page with device status."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, RichLog
from textual.containers import Vertical, Horizontal
from textual import work

from ..widgets.device_status import DeviceStatus


class DashboardScreen(Screen):
    """Main dashboard showing device status and quick actions."""

    BINDINGS = [
        Binding("r", "refresh_info", "Refresh", show=True),
        Binding("c", "connect", "Connect", show=True),
    ]

    DEFAULT_CSS = """
    DashboardScreen {
        layout: vertical;
    }
    #dashboard-title {
        text-align: center;
        padding: 1;
        text-style: bold;
        color: $accent;
    }
    #device-panel {
        border: solid $accent;
        margin: 1 2;
        padding: 1;
        height: auto;
    }
    #info-log {
        margin: 1 2;
        border: solid $surface;
        min-height: 10;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield DeviceStatus(id="status")
        yield Static("SayoDevice O3C Dashboard", id="dashboard-title")
        yield Static(
            "Press [bold]C[/bold] to connect to device | "
            "[bold]F1[/bold] Control  [bold]F2[/bold] Capture  "
            "[bold]F3[/bold] History  [bold]Q[/bold] Quit",
            id="device-panel",
        )
        yield RichLog(id="info-log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#info-log", RichLog)
        log.write("[dim]Welcome to SayoDevice O3C TUI[/dim]")
        log.write("[dim]Press C to connect to your device...[/dim]")

    def action_connect(self) -> None:
        self._do_connect()

    @work(thread=True)
    def _do_connect(self) -> None:
        log = self.query_one("#info-log", RichLog)
        status = self.query_one("#status", DeviceStatus)
        try:
            from sayodevice.device import SayoDevice
            self.app.call_from_thread(log.write, "Scanning for device...")
            dev = SayoDevice.open()
            self.app.device = dev
            info = dev.get_info()
            name = dev.get_device_name()
            info_str = (
                f"SAYO O3C | FW: {info} | "
                f"Interface: {dev.usage_page.name}"
            )
            self.app.call_from_thread(setattr, status, "connected", True)
            self.app.call_from_thread(setattr, status, "device_info", info_str)
            self.app.call_from_thread(log.write, f"[green]Connected![/green] {info_str}")
            if name:
                self.app.call_from_thread(log.write, f"Device name: {name}")
        except Exception as e:
            self.app.call_from_thread(log.write, f"[red]Connection failed:[/red] {e}")
            self.app.call_from_thread(setattr, status, "connected", False)

    def action_refresh_info(self) -> None:
        if self.app.device is None:
            log = self.query_one("#info-log", RichLog)
            log.write("[yellow]Not connected. Press C first.[/yellow]")
            return
        self._do_refresh()

    @work(thread=True)
    def _do_refresh(self) -> None:
        log = self.query_one("#info-log", RichLog)
        dev = self.app.device
        try:
            info = dev.get_info()
            self.app.call_from_thread(log.write, f"INFO: {info}")
            si = dev.get_sys_info()
            self.app.call_from_thread(log.write, f"SYS_INFO: {si}")
            setting = dev.get_setting()
            self.app.call_from_thread(log.write, f"SETTING: {setting}")
        except Exception as e:
            self.app.call_from_thread(log.write, f"[red]Error:[/red] {e}")

"""Dashboard screen — main landing page with device status."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, RichLog, DataTable
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
    #help-bar {
        text-align: center;
        padding: 0 1;
        color: $text-muted;
    }
    #quick-status {
        margin: 1 2;
        border: solid $accent;
        height: auto;
        max-height: 14;
        padding: 0;
    }
    #quick-status-title {
        padding: 0 1;
        text-style: bold;
        color: $accent;
    }
    #event-log {
        margin: 0 2 1 2;
        border: solid $surface;
        min-height: 6;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield DeviceStatus(id="status")
        yield Static("SayoDevice O3C Dashboard", id="dashboard-title")
        yield Static(
            "[bold]C[/bold] Connect  [bold]R[/bold] Refresh  |  "
            "[bold]F1[/bold] Control  [bold]F2[/bold] Capture  "
            "[bold]F3[/bold] History  [bold]Q[/bold] Quit",
            id="help-bar",
        )
        with Vertical(id="quick-status"):
            yield Static("Quick Status", id="quick-status-title")
            table = DataTable(id="info-table")
            table.show_header = False
            table.cursor_type = "none"
            table.zebra_stripes = True
            yield table
        yield RichLog(id="event-log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#info-table", DataTable)
        table.add_columns("field", "value")
        self._fill_disconnected(table)
        log = self.query_one("#event-log", RichLog)
        log.write("[dim]Press C to connect to your device...[/dim]")

    def _fill_disconnected(self, table: DataTable) -> None:
        table.clear()
        table.add_row("Device", "[dim]Not connected[/dim]")
        table.add_row("Firmware", "[dim]--[/dim]")
        table.add_row("Display", "[dim]--[/dim]")
        table.add_row("Host", "[dim]--[/dim]")
        table.add_row("Interface", "[dim]--[/dim]")
        table.add_row("HW ID", "[dim]--[/dim]")
        table.add_row("Uptime", "[dim]--[/dim]")

    def action_connect(self) -> None:
        self._do_connect()

    @work(thread=True)
    def _do_connect(self) -> None:
        log = self.query_one("#event-log", RichLog)
        status = self.query_one("#status", DeviceStatus)
        try:
            from sayodevice.device import SayoDevice
            self.app.call_from_thread(log.write, "Scanning for device...")
            dev = SayoDevice.open()
            self.app.device = dev

            # Query everything
            info = dev.get_info()
            name = dev.get_device_name() or "SAYO O3C"
            si = dev.get_sys_info()
            setting = dev.get_setting()

            fw_major = (info.firmware_version >> 8) & 0xFF
            fw_minor = info.firmware_version & 0xFF

            status_str = (
                f"{name} | FW: v{fw_major}.{fw_minor} | "
                f"{dev.usage_page.name}"
            )
            self.app.call_from_thread(setattr, status, "connected", True)
            self.app.call_from_thread(setattr, status, "device_info", status_str)

            # Update info table
            def update_table():
                table = self.query_one("#info-table", DataTable)
                table.clear()
                table.add_row("Device", f"[bold]{name}[/bold]")
                table.add_row("Firmware", f"v{fw_major}.{fw_minor}")
                table.add_row(
                    "Display",
                    f"{si.display_width}x{si.display_height} px"
                )
                table.add_row(
                    "Host",
                    f"{setting.host_width}x{setting.host_height}"
                )
                table.add_row(
                    "Interface",
                    f"{dev.usage_page.name} (0x{dev.usage_page.value:04X})"
                )
                table.add_row(
                    "HW ID / VID:PID",
                    f"{si.hw_id} / {si.vid:04X}:{si.pid:04X}"
                )
                table.add_row("Uptime", f"{si.uptime_s}s")
                table.add_row("FN", f"{info.fn}")
                if si.config_crc:
                    table.add_row("Config CRC", f"0x{si.config_crc:04X}")

            self.app.call_from_thread(update_table)
            self.app.call_from_thread(
                log.write, f"[green]Connected![/green] {status_str}"
            )

        except Exception as e:
            self.app.call_from_thread(
                log.write, f"[red]Connection failed:[/red] {e}"
            )
            self.app.call_from_thread(setattr, status, "connected", False)

    def action_refresh_info(self) -> None:
        if self.app.device is None:
            log = self.query_one("#event-log", RichLog)
            log.write("[yellow]Not connected. Press C first.[/yellow]")
            return
        self._do_refresh()

    @work(thread=True)
    def _do_refresh(self) -> None:
        log = self.query_one("#event-log", RichLog)
        dev = self.app.device
        try:
            info = dev.get_info()
            name = dev.get_device_name() or "SAYO O3C"
            si = dev.get_sys_info()
            setting = dev.get_setting()

            fw_major = (info.firmware_version >> 8) & 0xFF
            fw_minor = info.firmware_version & 0xFF

            def update_table():
                table = self.query_one("#info-table", DataTable)
                table.clear()
                table.add_row("Device", f"[bold]{name}[/bold]")
                table.add_row("Firmware", f"v{fw_major}.{fw_minor}")
                table.add_row(
                    "Display",
                    f"{si.display_width}x{si.display_height} px"
                )
                table.add_row(
                    "Host",
                    f"{setting.host_width}x{setting.host_height}"
                )
                table.add_row(
                    "Interface",
                    f"{dev.usage_page.name} (0x{dev.usage_page.value:04X})"
                )
                table.add_row(
                    "HW ID / VID:PID",
                    f"{si.hw_id} / {si.vid:04X}:{si.pid:04X}"
                )
                table.add_row("Uptime", f"{si.uptime_s}s")
                table.add_row("FN", f"{info.fn}")
                if si.config_crc:
                    table.add_row("Config CRC", f"0x{si.config_crc:04X}")

            self.app.call_from_thread(update_table)
            self.app.call_from_thread(
                log.write, "[green]Refreshed.[/green]"
            )

        except Exception as e:
            self.app.call_from_thread(
                log.write, f"[red]Error:[/red] {e}"
            )

"""Device connection status widget."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class DeviceStatus(Widget):
    """Shows device connection status in a single line."""

    connected: reactive[bool] = reactive(False)
    device_info: reactive[str] = reactive("No device")

    DEFAULT_CSS = """
    DeviceStatus {
        height: 1;
        dock: top;
        padding: 0 1;
    }
    DeviceStatus .connected {
        color: $success;
    }
    DeviceStatus .disconnected {
        color: $error;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(self._render_text(), id="device-status-text")

    def _render_text(self) -> str:
        if self.connected:
            return f"[green]Connected[/green] | {self.device_info}"
        return "[red]Disconnected[/red] | No device found"

    def watch_connected(self) -> None:
        self._update()

    def watch_device_info(self) -> None:
        self._update()

    def _update(self) -> None:
        try:
            self.query_one("#device-status-text", Static).update(self._render_text())
        except Exception:
            pass

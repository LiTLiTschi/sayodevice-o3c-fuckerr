"""SayoDevice TUI — Main application."""

from __future__ import annotations

import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from .screens.dashboard import DashboardScreen
from .screens.capture import CaptureScreen
from .screens.control import ControlScreen
from .screens.history import HistoryScreen


class SayoApp(App):
    """SayoDevice O3C — Textual TUI."""

    TITLE = "SayoDevice O3C"
    CSS = """
    Screen {
        layout: vertical;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("f1", "switch_screen('control')", "Control", show=True),
        Binding("f2", "switch_screen('capture')", "Capture", show=True),
        Binding("f3", "switch_screen('history')", "History", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    SCREENS = {
        "dashboard": DashboardScreen,
        "control": ControlScreen,
        "capture": CaptureScreen,
        "history": HistoryScreen,
    }

    def __init__(self):
        super().__init__()
        self._device = None  # SayoDevice, set after connect

    @property
    def device(self):
        return self._device

    @device.setter
    def device(self, dev):
        self._device = dev

    def on_mount(self) -> None:
        self.push_screen("dashboard")

    def action_switch_screen(self, screen_name: str) -> None:
        # Pop back to base then push the requested screen
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(screen_name)


def main() -> None:
    """Entry point for the TUI."""
    app = SayoApp()
    app.run()


def run() -> None:
    """Top-level entry point — dispatches to TUI or classic CLI."""
    if "--classic" in sys.argv:
        sys.argv.remove("--classic")
        from sayodevice.cli import main as cli_main
        cli_main()
    else:
        main()


if __name__ == "__main__":
    run()

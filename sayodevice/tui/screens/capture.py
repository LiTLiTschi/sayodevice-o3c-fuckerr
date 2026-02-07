"""Capture screen — live capture & diff mode (Phase 4 placeholder)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, RichLog
from textual import work


class CaptureScreen(Screen):
    """Live capture & diff mode — baseline, snapshot, compare."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
        Binding("b", "take_baseline", "Baseline", show=True),
        Binding("n", "take_snapshot", "Snapshot", show=True),
    ]

    DEFAULT_CSS = """
    CaptureScreen {
        layout: vertical;
    }
    #capture-title {
        text-align: center;
        padding: 1;
        text-style: bold;
        color: $accent;
    }
    #capture-help {
        margin: 0 2;
        padding: 1;
        border: solid $surface;
    }
    #capture-log {
        margin: 1 2;
        border: solid $accent;
        min-height: 12;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Live Capture & Diff", id="capture-title")
        yield Static(
            "[bold]B[/bold] Take Baseline  |  "
            "[bold]N[/bold] New Snapshot  |  "
            "[bold]Esc[/bold] Back\n\n"
            "[dim]Take a baseline, make a change on the device, "
            "then take a new snapshot to see what changed.[/dim]",
            id="capture-help",
        )
        yield RichLog(id="capture-log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#capture-log", RichLog)
        log.write("[dim]Capture mode ready. Press B to take a baseline snapshot.[/dim]")

    def action_take_baseline(self) -> None:
        if self.app.device is None:
            log = self.query_one("#capture-log", RichLog)
            log.write("[yellow]Not connected. Go to Dashboard and press C.[/yellow]")
            return
        self._do_baseline()

    @work(thread=True)
    def _do_baseline(self) -> None:
        log = self.query_one("#capture-log", RichLog)
        dev = self.app.device
        from sayodevice.protocol import CmdId
        from sayodevice.analyzer import decode_raw_response
        try:
            self.app.call_from_thread(log.write, "[bold]--- BASELINE ---[/bold]")
            for cmd_id in [CmdId.INFO, CmdId.SYS_INFO, CmdId.SETTING, CmdId.DEVICE_NAME]:
                resp = dev.send_single(cmd_id)
                name = cmd_id.name
                if resp:
                    decoded = decode_raw_response(resp)
                    self.app.call_from_thread(log.write, f"[cyan]{name}:[/cyan]\n{decoded}")
                else:
                    self.app.call_from_thread(log.write, f"[cyan]{name}:[/cyan] (no response)")
            self.app.call_from_thread(log.write, "[green]Baseline captured.[/green]")
        except Exception as e:
            self.app.call_from_thread(log.write, f"[red]Error:[/red] {e}")

    def action_take_snapshot(self) -> None:
        if self.app.device is None:
            log = self.query_one("#capture-log", RichLog)
            log.write("[yellow]Not connected. Go to Dashboard and press C.[/yellow]")
            return
        self._do_snapshot()

    @work(thread=True)
    def _do_snapshot(self) -> None:
        log = self.query_one("#capture-log", RichLog)
        dev = self.app.device
        from sayodevice.protocol import CmdId
        from sayodevice.analyzer import decode_raw_response
        try:
            self.app.call_from_thread(log.write, "[bold]--- NEW SNAPSHOT ---[/bold]")
            for cmd_id in [CmdId.INFO, CmdId.SYS_INFO, CmdId.SETTING, CmdId.DEVICE_NAME]:
                resp = dev.send_single(cmd_id)
                name = cmd_id.name
                if resp:
                    decoded = decode_raw_response(resp)
                    self.app.call_from_thread(log.write, f"[cyan]{name}:[/cyan]\n{decoded}")
                else:
                    self.app.call_from_thread(log.write, f"[cyan]{name}:[/cyan] (no response)")
            self.app.call_from_thread(log.write, "[green]Snapshot captured.[/green]")
        except Exception as e:
            self.app.call_from_thread(log.write, f"[red]Error:[/red] {e}")

"""Control screen — device control (set pos, color, arg0, etc.)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, Input, Button, RichLog
from textual.containers import Vertical, Horizontal
from textual import work


class ControlScreen(Screen):
    """Device control panel for setting position, color, arg0."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
    ]

    DEFAULT_CSS = """
    ControlScreen {
        layout: vertical;
    }
    #control-title {
        text-align: center;
        padding: 1;
        text-style: bold;
        color: $accent;
    }
    .field-row {
        layout: horizontal;
        height: 3;
        margin: 0 2;
    }
    .field-label {
        width: 16;
        padding: 1;
    }
    .field-input {
        width: 20;
    }
    #control-buttons {
        layout: horizontal;
        height: 3;
        margin: 1 2;
    }
    #control-log {
        margin: 1 2;
        border: solid $surface;
        min-height: 6;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Device Control", id="control-title")

        with Horizontal(classes="field-row"):
            yield Static("X Position:", classes="field-label")
            yield Input(placeholder="0", id="input-x", classes="field-input")
        with Horizontal(classes="field-row"):
            yield Static("Y Position:", classes="field-label")
            yield Input(placeholder="0", id="input-y", classes="field-input")
        with Horizontal(classes="field-row"):
            yield Static("Width:", classes="field-label")
            yield Input(placeholder="40", id="input-w", classes="field-input", value="40")
        with Horizontal(classes="field-row"):
            yield Static("Height:", classes="field-label")
            yield Input(placeholder="40", id="input-h", classes="field-input", value="40")
        with Horizontal(classes="field-row"):
            yield Static("Color:", classes="field-label")
            yield Input(placeholder="#FFFFFF or 0xFFFF", id="input-color", classes="field-input", value="#FFFFFF")
        with Horizontal(classes="field-row"):
            yield Static("Layer:", classes="field-label")
            yield Input(placeholder="15", id="input-layer", classes="field-input", value="15")
        with Horizontal(classes="field-row"):
            yield Static("Arg0:", classes="field-label")
            yield Input(placeholder="0-255", id="input-arg0", classes="field-input")

        with Horizontal(id="control-buttons"):
            yield Button("Apply Screen", id="btn-apply-screen", variant="primary")
            yield Button("Apply + Refresh", id="btn-apply-refresh", variant="success")
            yield Button("Set Arg0", id="btn-set-arg0")
            yield Button("Save", id="btn-save", variant="warning")

        yield RichLog(id="control-log", highlight=True, markup=True)
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.app.device is None:
            log = self.query_one("#control-log", RichLog)
            log.write("[yellow]Not connected. Go to Dashboard and press C.[/yellow]")
            return

        if event.button.id == "btn-apply-screen":
            self._apply_screen(refresh=False)
        elif event.button.id == "btn-apply-refresh":
            self._apply_screen(refresh=True)
        elif event.button.id == "btn-set-arg0":
            self._set_arg0()
        elif event.button.id == "btn-save":
            self._save()

    @work(thread=True)
    def _apply_screen(self, refresh: bool = False) -> None:
        log = self.query_one("#control-log", RichLog)
        dev = self.app.device
        try:
            x = int(self.query_one("#input-x", Input).value or "0")
            y = int(self.query_one("#input-y", Input).value or "0")
            w = int(self.query_one("#input-w", Input).value or "40")
            h = int(self.query_one("#input-h", Input).value or "40")
            layer = int(self.query_one("#input-layer", Input).value or "15")

            color_str = self.query_one("#input-color", Input).value or "#FFFFFF"
            if color_str.startswith("#"):
                from sayodevice.protocol import hex_color_to_565
                color = hex_color_to_565(color_str)
            else:
                color = int(color_str, 0)

            dev.set_screen_element(
                x=x, y=y, width=w, height=h,
                color=color, element_index=layer,
                refresh=refresh,
            )
            msg = f"Set x={x} y={y} {w}x{h} color=0x{color:04X} layer={layer}"
            if refresh:
                msg += " + refresh"
            self.app.call_from_thread(log.write, f"[green]{msg}[/green]")
        except Exception as e:
            self.app.call_from_thread(log.write, f"[red]Error:[/red] {e}")

    @work(thread=True)
    def _set_arg0(self) -> None:
        log = self.query_one("#control-log", RichLog)
        dev = self.app.device
        try:
            val = int(self.query_one("#input-arg0", Input).value or "0")
            dev.set_key_arg0(val, save=False)
            self.app.call_from_thread(log.write, f"[green]Arg0 = {val}[/green]")
        except Exception as e:
            self.app.call_from_thread(log.write, f"[red]Error:[/red] {e}")

    @work(thread=True)
    def _save(self) -> None:
        log = self.query_one("#control-log", RichLog)
        dev = self.app.device
        try:
            dev.save()
            self.app.call_from_thread(log.write, "[green]Saved![/green]")
        except Exception as e:
            self.app.call_from_thread(log.write, f"[red]Error:[/red] {e}")

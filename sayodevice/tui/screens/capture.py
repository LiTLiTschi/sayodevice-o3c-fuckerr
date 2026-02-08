"""Capture screen — live capture & diff mode."""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, RichLog, Input
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual import work

from ..widgets.hex_view import HexView
from ..widgets.diff_view import DiffView
from ..snapshots import (
    Snapshot, Discovery, FieldChange,
    capture_snapshot, diff_snapshots, get_changed_byte_offsets,
    save_discovery, _cmd_name,
)


class CaptureScreen(Screen):
    """Live capture & diff mode — baseline, snapshot, compare, label, save."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
        Binding("b", "take_baseline", "Baseline", show=True),
        Binding("n", "take_snapshot", "Snapshot", show=True),
        Binding("l", "label_save", "Label+Save", show=True),
    ]

    DEFAULT_CSS = """
    CaptureScreen {
        layout: vertical;
    }
    #capture-status {
        padding: 0 2;
        height: 1;
        color: $text;
        background: $surface;
    }
    #capture-body {
        margin: 0 1;
    }
    .panel-title {
        text-style: bold;
        color: $accent;
        padding: 0 1;
    }
    #hex-panel {
        border: solid $surface;
        margin: 0 1;
        height: auto;
        max-height: 14;
    }
    #diff-panel {
        border: solid $accent;
        margin: 0 1;
        height: auto;
        max-height: 8;
    }
    #capture-log {
        margin: 0 1;
        border: solid $surface;
        min-height: 4;
        max-height: 8;
    }
    #label-row {
        layout: horizontal;
        height: 3;
        margin: 0 1;
        display: none;
    }
    #label-prompt {
        width: 22;
        padding: 1;
    }
    #label-input {
        width: 1fr;
    }
    """

    def __init__(self):
        super().__init__()
        self._baseline: Snapshot | None = None
        self._latest: Snapshot | None = None
        self._changes: list[FieldChange] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("No baseline captured", id="capture-status")
        with VerticalScroll(id="capture-body"):
            yield Static(
                "[bold]B[/bold] Baseline  [bold]N[/bold] Snapshot  "
                "[bold]L[/bold] Label+Save  [bold]Esc[/bold] Back",
            )
            yield Static("Hex View", classes="panel-title")
            with Vertical(id="hex-panel"):
                yield HexView(id="hex-view")
            yield Static("Field Changes", classes="panel-title")
            with Vertical(id="diff-panel"):
                yield DiffView(id="diff-view")
            with Horizontal(id="label-row"):
                yield Static("What did you change?", id="label-prompt")
                yield Input(placeholder="e.g. Set X position to 120", id="label-input")
            yield RichLog(id="capture-log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#capture-log", RichLog)
        log.write("[dim]Press B to take a baseline snapshot.[/dim]")

    def _update_status(self, text: str) -> None:
        self.query_one("#capture-status", Static).update(text)

    # ---- Baseline ----

    def action_take_baseline(self) -> None:
        if self.app.device is None:
            self.query_one("#capture-log", RichLog).write(
                "[yellow]Not connected. Go to Dashboard (Esc) and press C.[/yellow]"
            )
            return
        self._do_baseline()

    @work(thread=True)
    def _do_baseline(self) -> None:
        log = self.query_one("#capture-log", RichLog)
        self.app.call_from_thread(log.write, "Capturing baseline...")
        try:
            snap = capture_snapshot(self.app.device, label="baseline")
            self._baseline = snap
            self._latest = None
            self._changes = []

            # Show hex of first non-trivial response
            cmd_id, raw = self._pick_display_cmd(snap)
            cmd_name = _cmd_name(cmd_id) if cmd_id is not None else "?"

            def update_ui():
                self._update_status(
                    f"Baseline captured at "
                    f"{time.strftime('%H:%M:%S', time.localtime(snap.timestamp))} "
                    f"| {len(snap.responses)} responses | Status: waiting for snapshot..."
                )
                hex_view = self.query_one("#hex-view", HexView)
                if raw:
                    hex_view.update_data(raw, title=f"{cmd_name} response")
                else:
                    hex_view.update_data(b"", title="No data")
                self.query_one("#diff-view", DiffView).update_changes([])
                self.query_one("#label-row").styles.display = "none"

            self.app.call_from_thread(update_ui)
            self.app.call_from_thread(log.write, "[green]Baseline captured.[/green] Now make a change and press N.")

            # Log decoded fields
            for cid, decoded in snap.decoded.items():
                if decoded:
                    fields_str = ", ".join(f"{k}={v}" for k, v in decoded.items())
                    self.app.call_from_thread(
                        log.write, f"[dim]{_cmd_name(cid)}: {fields_str}[/dim]"
                    )

        except Exception as e:
            self.app.call_from_thread(log.write, f"[red]Error:[/red] {e}")

    # ---- New Snapshot ----

    def action_take_snapshot(self) -> None:
        if self.app.device is None:
            self.query_one("#capture-log", RichLog).write(
                "[yellow]Not connected.[/yellow]"
            )
            return
        if self._baseline is None:
            self.query_one("#capture-log", RichLog).write(
                "[yellow]Take a baseline first (B).[/yellow]"
            )
            return
        self._do_snapshot()

    @work(thread=True)
    def _do_snapshot(self) -> None:
        log = self.query_one("#capture-log", RichLog)
        self.app.call_from_thread(log.write, "Capturing new snapshot...")
        try:
            snap = capture_snapshot(self.app.device, label="snapshot")
            self._latest = snap

            # Diff against baseline
            changes = diff_snapshots(self._baseline, snap)
            self._changes = changes

            # Find which command has the most interesting changes
            cmd_id, raw = self._pick_display_cmd(snap)
            cmd_name = _cmd_name(cmd_id) if cmd_id is not None else "?"

            # Get changed byte offsets for hex highlighting
            changed_offsets = set()
            if cmd_id is not None:
                changed_offsets = get_changed_byte_offsets(
                    self._baseline, snap, cmd_id
                )

            n_changes = len(changes)

            def update_ui():
                self._update_status(
                    f"Baseline: {time.strftime('%H:%M:%S', time.localtime(self._baseline.timestamp))} | "
                    f"Snapshot: {time.strftime('%H:%M:%S', time.localtime(snap.timestamp))} | "
                    f"{n_changes} change(s) detected"
                )
                hex_view = self.query_one("#hex-view", HexView)
                if raw:
                    hex_view.update_data(
                        raw, changed_offsets=changed_offsets,
                        title=f"{cmd_name} response (changed bytes in red)",
                    )
                diff_view = self.query_one("#diff-view", DiffView)
                diff_view.update_changes(changes)
                if changes:
                    self.query_one("#label-row").styles.display = "block"

            self.app.call_from_thread(update_ui)

            if changes:
                self.app.call_from_thread(
                    log.write,
                    f"[green]{n_changes} change(s) detected![/green] "
                    f"Press L to label and save this discovery."
                )
                for fc in changes:
                    self.app.call_from_thread(log.write, f"  {fc}")
            else:
                self.app.call_from_thread(
                    log.write, "[dim]No changes detected between baseline and snapshot.[/dim]"
                )

        except Exception as e:
            self.app.call_from_thread(log.write, f"[red]Error:[/red] {e}")

    # ---- Label & Save ----

    def action_label_save(self) -> None:
        if not self._changes:
            self.query_one("#capture-log", RichLog).write(
                "[yellow]No changes to save. Take baseline (B) then snapshot (N) first.[/yellow]"
            )
            return
        self._do_save()

    @work(thread=True)
    def _do_save(self) -> None:
        log = self.query_one("#capture-log", RichLog)
        label = self.query_one("#label-input", Input).value.strip()
        if not label:
            label = f"Discovery at {time.strftime('%Y-%m-%d %H:%M:%S')}"

        try:
            discovery = Discovery(
                description=label,
                before=self._baseline,
                after=self._latest,
                changed_fields=self._changes,
            )
            path = save_discovery(discovery)
            self.app.call_from_thread(
                log.write, f"[green]Saved![/green] {path}"
            )
            # Reset label input
            def hide_label():
                self.query_one("#label-row").styles.display = "none"
                self.query_one("#label-input", Input).value = ""
            self.app.call_from_thread(hide_label)

        except Exception as e:
            self.app.call_from_thread(log.write, f"[red]Save error:[/red] {e}")

    # ---- Helpers ----

    def _pick_display_cmd(self, snap: Snapshot) -> tuple[int | None, bytes]:
        """Pick the most interesting command to display in hex view."""
        from sayodevice.protocol import CmdId
        priority = [CmdId.SCREEN_MAIN, CmdId.KEY, CmdId.SYS_INFO, CmdId.SETTING, CmdId.INFO]
        for cmd_id in priority:
            if cmd_id in snap.responses:
                return cmd_id, snap.responses[cmd_id]
        if snap.responses:
            cmd_id = next(iter(snap.responses))
            return cmd_id, snap.responses[cmd_id]
        return None, b""

"""History screen — browse saved discoveries & diffs."""

from __future__ import annotations

import time
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, DataTable, RichLog
from textual.containers import Vertical, VerticalScroll

from ..widgets.hex_view import HexView
from ..widgets.diff_view import DiffView
from ..snapshots import list_discoveries, Discovery, _cmd_name


class HistoryScreen(Screen):
    """Browse saved discoveries and re-view diffs."""

    BINDINGS = [
        Binding("escape", "app.switch_screen('dashboard')", "Back", show=True),
        Binding("r", "reload", "Reload", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("enter", "view_detail", "View", show=True),
    ]

    DEFAULT_CSS = """
    HistoryScreen {
        layout: vertical;
    }
    #history-title {
        text-align: center;
        padding: 1 0 0 0;
        text-style: bold;
        color: $accent;
    }
    #help-bar {
        text-align: center;
        padding: 0 1;
        color: $text-muted;
    }
    #discovery-table {
        margin: 0 2;
        height: 1fr;
        min-height: 6;
    }
    #detail-panel {
        margin: 0 2;
        border: solid $accent;
        height: auto;
        max-height: 16;
        display: none;
    }
    #detail-title {
        padding: 0 1;
        text-style: bold;
        color: $accent;
    }
    #history-log {
        margin: 0 2;
        border: solid $surface;
        height: 4;
    }
    """

    def __init__(self):
        super().__init__()
        self._discoveries: list[tuple[Path, Discovery]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Snapshot History", id="history-title")
        yield Static(
            "[bold]Enter[/bold] View  [bold]D[/bold] Delete  "
            "[bold]R[/bold] Reload  [bold]Esc[/bold] Back",
            id="help-bar",
        )
        table = DataTable(id="discovery-table")
        table.cursor_type = "row"
        table.zebra_stripes = True
        yield table
        with Vertical(id="detail-panel"):
            yield Static("", id="detail-title")
            yield HexView(id="detail-hex")
            yield DiffView(id="detail-diff")
        yield RichLog(id="history-log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#discovery-table", DataTable)
        table.add_columns("Date", "Description", "Changes", "AI")
        self._load_discoveries()

    def _load_discoveries(self) -> None:
        self._discoveries = list_discoveries()
        table = self.query_one("#discovery-table", DataTable)
        table.clear()
        log = self.query_one("#history-log", RichLog)

        if not self._discoveries:
            log.write("[dim]No discoveries saved yet. Use Capture (F2) to create some.[/dim]")
            return

        for path, disc in self._discoveries:
            ts = time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime(disc.after.timestamp or 0),
            )
            n_changes = len(disc.changed_fields)
            has_ai = "Yes" if disc.ai_analysis else ""
            table.add_row(ts, disc.description[:40], str(n_changes), has_ai)

        log.write(f"[dim]Loaded {len(self._discoveries)} discovery(ies) from ~/.sayodevice/snapshots/[/dim]")

    def action_reload(self) -> None:
        self._load_discoveries()
        self.query_one("#detail-panel").styles.display = "none"
        self.query_one("#history-log", RichLog).write("[green]Reloaded.[/green]")

    def action_view_detail(self) -> None:
        table = self.query_one("#discovery-table", DataTable)
        if not self._discoveries:
            return
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._discoveries):
            return

        path, disc = self._discoveries[row_idx]
        self._show_detail(disc)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_idx = event.cursor_row
        if row_idx < 0 or row_idx >= len(self._discoveries):
            return
        path, disc = self._discoveries[row_idx]
        self._show_detail(disc)

    def _show_detail(self, disc: Discovery) -> None:
        panel = self.query_one("#detail-panel")
        panel.styles.display = "block"

        title = self.query_one("#detail-title", Static)
        title.update(f"[bold]{disc.description}[/bold]")

        # Show hex of the "after" snapshot for the most interesting command
        from sayodevice.protocol import CmdId
        from ..snapshots import get_changed_byte_offsets
        priority = [CmdId.SCREEN_MAIN, CmdId.KEY, CmdId.SYS_INFO, CmdId.SETTING, CmdId.INFO]
        cmd_id = None
        raw = b""
        for cid in priority:
            if cid in disc.after.responses:
                cmd_id = cid
                raw = disc.after.responses[cid]
                break
        if not raw and disc.after.responses:
            cmd_id = next(iter(disc.after.responses))
            raw = disc.after.responses[cmd_id]

        changed_offsets = set()
        if cmd_id is not None:
            changed_offsets = get_changed_byte_offsets(disc.before, disc.after, cmd_id)

        hex_view = self.query_one("#detail-hex", HexView)
        if raw:
            cmd_name = _cmd_name(cmd_id) if cmd_id is not None else "?"
            hex_view.update_data(raw, changed_offsets=changed_offsets, title=f"{cmd_name} (after)")
        else:
            hex_view.update_data(b"", title="No data")

        diff_view = self.query_one("#detail-diff", DiffView)
        diff_view.update_changes(disc.changed_fields)

        # Show AI analysis if present
        if disc.ai_analysis:
            log = self.query_one("#history-log", RichLog)
            log.write(f"[bold]AI Analysis:[/bold]\n{disc.ai_analysis}")

    def action_delete(self) -> None:
        table = self.query_one("#discovery-table", DataTable)
        if not self._discoveries:
            return
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._discoveries):
            return

        path, disc = self._discoveries[row_idx]
        log = self.query_one("#history-log", RichLog)
        try:
            path.unlink()
            log.write(f"[green]Deleted:[/green] {disc.description}")
            self._load_discoveries()
            self.query_one("#detail-panel").styles.display = "none"
        except Exception as e:
            log.write(f"[red]Delete error:[/red] {e}")

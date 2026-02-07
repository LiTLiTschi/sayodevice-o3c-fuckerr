"""History screen — browse saved snapshots & diffs (Phase 5 placeholder)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class HistoryScreen(Screen):
    """Browse saved snapshots and discoveries."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
    ]

    DEFAULT_CSS = """
    HistoryScreen {
        layout: vertical;
    }
    #history-title {
        text-align: center;
        padding: 1;
        text-style: bold;
        color: $accent;
    }
    #history-placeholder {
        margin: 2 4;
        padding: 2;
        border: dashed $surface;
        text-align: center;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Snapshot History", id="history-title")
        yield Static(
            "[dim]No snapshots saved yet.\n\n"
            "Use the Capture screen (F2) to take snapshots.\n"
            "Saved snapshots and diffs will appear here.[/dim]",
            id="history-placeholder",
        )
        yield Footer()

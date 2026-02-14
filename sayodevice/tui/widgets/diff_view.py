"""Diff view widget — shows field-level changes between snapshots."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static


class DiffView(Widget):
    """Displays field-level changes from a snapshot diff."""

    DEFAULT_CSS = """
    DiffView {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._changes: list = []

    def compose(self) -> ComposeResult:
        yield Static("[dim]No diff yet[/dim]", id="diff-content")

    def update_changes(self, changes: list) -> None:
        """Update with a list of FieldChange objects."""
        self._changes = changes
        try:
            self.query_one("#diff-content", Static).update(self._format_changes())
        except Exception:
            pass

    def _format_changes(self) -> str:
        if not self._changes:
            return "[dim]No changes detected[/dim]"

        lines = ["[bold]Field Changes[/bold]"]
        for fc in self._changes:
            # Import here to avoid circular imports
            from sayodevice.protocol import CmdId
            try:
                cmd_name = CmdId(fc.cmd_id).name
            except ValueError:
                cmd_name = f"CMD_0x{fc.cmd_id:02X}"

            label = fc.field_label or f"byte[{fc.offset}]"

            if fc.size == 1:
                offset_str = f"[{fc.offset}]"
            else:
                offset_str = f"[{fc.offset}-{fc.offset + fc.size - 1}]"

            if fc.field_label:
                # Known field — show with green highlight
                lines.append(
                    f"  [cyan]{cmd_name}[/cyan] {offset_str} "
                    f"[bold]{label}[/bold]: "
                    f"[red]{fc.old_value}[/red] -> [green]{fc.new_value}[/green]"
                )
            else:
                # Unknown field — show as yellow
                lines.append(
                    f"  [cyan]{cmd_name}[/cyan] {offset_str} "
                    f"[yellow]{label}[/yellow]: "
                    f"[red]0x{fc.old_value:X}[/red] -> [green]0x{fc.new_value:X}[/green]"
                )

        return "\n".join(lines)

"""Hex dump widget with change highlighting."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static


class HexView(Widget):
    """Displays a hex dump of raw bytes, highlighting changed offsets."""

    DEFAULT_CSS = """
    HexView {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        data: bytes = b"",
        changed_offsets: set[int] | None = None,
        title: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._data = data
        self._changed = changed_offsets or set()
        self._title = title

    def compose(self) -> ComposeResult:
        yield Static(self._format_hex(), id="hex-content")

    def update_data(
        self,
        data: bytes,
        changed_offsets: set[int] | None = None,
        title: str = "",
    ) -> None:
        self._data = data
        self._changed = changed_offsets or set()
        if title:
            self._title = title
        try:
            self.query_one("#hex-content", Static).update(self._format_hex())
        except Exception:
            pass

    def _format_hex(self) -> str:
        if not self._data:
            return "[dim]No data[/dim]"

        lines = []
        if self._title:
            lines.append(f"[bold]{self._title}[/bold]")

        # Skip 8-byte packet header, show payload only
        payload = self._data[8:] if len(self._data) > 8 else self._data
        bytes_per_line = 16

        for row_start in range(0, len(payload), bytes_per_line):
            row_end = min(row_start + bytes_per_line, len(payload))
            chunk = payload[row_start:row_end]

            # Address
            addr = f"{row_start:04x}: "

            # Hex bytes with highlighting
            hex_parts = []
            for i, b in enumerate(chunk):
                offset = row_start + i
                if offset in self._changed:
                    hex_parts.append(f"[bold red]{b:02x}[/bold red]")
                else:
                    hex_parts.append(f"{b:02x}")
                # Add extra space every 4 bytes for readability
                if (i + 1) % 4 == 0 and i + 1 < len(chunk):
                    hex_parts.append(" ")

            hex_str = " ".join(hex_parts)

            # ASCII representation
            ascii_parts = []
            for b in chunk:
                if 0x20 <= b <= 0x7E:
                    ascii_parts.append(chr(b))
                else:
                    ascii_parts.append(".")
            ascii_str = "".join(ascii_parts)

            lines.append(f"[dim]{addr}[/dim]{hex_str}  [dim]{ascii_str}[/dim]")

        return "\n".join(lines)

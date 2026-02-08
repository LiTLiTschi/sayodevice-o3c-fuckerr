"""PacketTable widget — live-updating DataTable of sniffed USB packets."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import DataTable

from ...analyzer import UsbPacket, DecodedCommand, _KNOWN_FIELDS, _read_field


class PacketTable(Widget):
    """Live-updating DataTable showing sniffed USB packets.

    Columns: Time | Dir | Cmd | Idx | Summary | Size

    Features:
        - ``add_packet()`` appends decoded rows
        - Ring buffer (max 500 rows, trims oldest)
        - Auto-scroll to bottom
    """

    DEFAULT_CSS = """
    PacketTable {
        height: 1fr;
        min-height: 8;
    }
    PacketTable DataTable {
        height: 1fr;
    }
    """

    MAX_ROWS = 500

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._packets: list[tuple[UsbPacket, list[DecodedCommand]]] = []
        self._start_time: float | None = None
        self._row_keys: list[str] = []  # track row keys for removal

    def compose(self) -> ComposeResult:
        table = DataTable(id="pkt-table")
        table.cursor_type = "row"
        table.zebra_stripes = True
        yield table

    def on_mount(self) -> None:
        table = self.query_one("#pkt-table", DataTable)
        table.add_columns("Time", "Dir", "Cmd", "Idx", "Summary", "Size")

    def add_packet(
        self, packet: UsbPacket, commands: list[DecodedCommand]
    ) -> None:
        """Add a new packet to the table."""
        if self._start_time is None:
            self._start_time = packet.timestamp

        self._packets.append((packet, commands))
        table = self.query_one("#pkt-table", DataTable)

        rel_time = packet.timestamp - self._start_time
        time_str = f"+{rel_time:.3f}s"

        dir_str = (
            "[bold cyan]OUT[/]" if packet.direction == "OUT"
            else "[bold green]IN[/]"
        )

        if commands:
            for cmd in commands:
                summary = self._summarize(cmd)
                key = table.add_row(
                    time_str, dir_str, cmd.cmd_name,
                    str(cmd.index), summary, str(len(cmd.data)),
                )
                self._row_keys.append(str(key))
        else:
            # Raw packet, no decoded commands
            rid = f"0x{packet.report_id:02X}" if packet.report_id else "?"
            key = table.add_row(
                time_str, dir_str, f"RAW({rid})",
                "-", packet.payload[:16].hex(" "), str(len(packet.payload)),
            )
            self._row_keys.append(str(key))

        # Ring buffer: trim oldest rows
        while table.row_count > self.MAX_ROWS:
            first_key = table.rows[next(iter(table.rows))].key
            table.remove_row(first_key)
            if self._row_keys:
                self._row_keys.pop(0)
            if self._packets:
                self._packets.pop(0)

        # Auto-scroll
        table.scroll_end(animate=False)

    def clear_packets(self) -> None:
        """Clear all packets from the table."""
        self._packets.clear()
        self._row_keys.clear()
        self._start_time = None
        table = self.query_one("#pkt-table", DataTable)
        table.clear()

    @staticmethod
    def _summarize(cmd: DecodedCommand) -> str:
        """Generate a brief summary of a decoded command using known fields."""
        known = _KNOWN_FIELDS.get(cmd.cmd_id, [])
        if not known:
            return cmd.data[:8].hex(" ") if cmd.data else "(empty)"

        parts: list[str] = []
        for offset, size, _ftype, label in known[:4]:
            if offset + size <= len(cmd.data):
                val = _read_field(cmd.data, offset, size)
                short = label.split("(")[0].strip()
                parts.append(f"{short}={val}")
        return ", ".join(parts) if parts else cmd.data[:8].hex(" ")

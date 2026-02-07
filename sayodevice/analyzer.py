"""
SayoDevice O3C - Packet capture analyzer.

Parse pcapng files (USBPcap on Windows) and auto-analyze SAYO HID protocol
traffic, eliminating the need for manual Wireshark hex analysis.

Usage::

    # CLI
    sayodevice analyze capture.pcapng

    # Python
    from sayodevice.analyzer import analyze_pcapng
    report = analyze_pcapng("capture.pcapng")
    print(report)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import CmdId, calc_checksum

# ============================================================
# pcapng constants
# ============================================================

_BLOCK_SHB = 0x0A0D0D0A
_BLOCK_IDB = 0x00000001
_BLOCK_EPB = 0x00000006
_BYTE_ORDER_MAGIC = 0x1A2B3C4D
_LINKTYPE_USBPCAP = 249

# USBPcap transfer types
_TRANSFER_INTERRUPT = 1

# SAYO report IDs
_REPORT_ID_HIGHSPEED = 0x22
_REPORT_ID_NORMAL = 0x21


# ============================================================
# Data classes
# ============================================================

@dataclass
class UsbPacket:
    """A single USB packet extracted from a pcapng capture."""
    timestamp: float
    device: int
    endpoint: int
    direction: str  # "OUT" or "IN"
    is_submit: bool
    payload: bytes

    @property
    def report_id(self) -> int | None:
        if self.payload:
            return self.payload[0]
        return None


@dataclass
class DecodedCommand:
    """A decoded HID command from a SAYO packet."""
    timestamp: float
    cmd_id: int
    index: int
    data: bytes
    raw_packet: bytes

    @property
    def cmd_name(self) -> str:
        try:
            return CmdId(self.cmd_id).name
        except ValueError:
            return f"UNKNOWN_0x{self.cmd_id:02X}"

    def data_hex(self, max_bytes: int = 0) -> str:
        d = self.data[:max_bytes] if max_bytes else self.data
        return d.hex(" ") if d else "(empty)"


@dataclass
class FieldAnalysis:
    """Analysis of a single field across multiple packets."""
    offset: int
    size: int
    type_name: str  # "uint8", "uint16_le", "uint32_le"
    values: list[int]
    is_constant: bool
    label: str = ""

    @property
    def unique_values(self) -> list[int]:
        return sorted(set(self.values))

    def value_summary(self) -> str:
        uv = self.unique_values
        if self.is_constant:
            v = uv[0]
            if self.size == 1:
                return f"0x{v:02X} (={v})"
            elif self.size == 2:
                return f"0x{v:04X} (={v})"
            else:
                return f"0x{v:08X} (={v})"
        else:
            return ", ".join(str(v) for v in self.values)


@dataclass
class CommandGroupAnalysis:
    """Analysis of all packets sharing the same cmd_id + index."""
    cmd_id: int
    cmd_name: str
    index: int
    count: int
    data_length: int
    constant_fields: list[FieldAnalysis]
    varying_fields: list[FieldAnalysis]
    zero_ranges: list[tuple[int, int]]  # (start, end) of always-zero regions


# ============================================================
# pcapng parser (pure Python, no dependencies)
# ============================================================

def parse_pcapng(filepath: str | Path) -> list[UsbPacket]:
    """
    Parse a pcapng file and extract USB interrupt transfer packets.

    Args:
        filepath: Path to .pcapng file (USBPcap capture).

    Returns:
        List of UsbPacket with raw HID payloads.
    """
    data = Path(filepath).read_bytes()
    packets: list[UsbPacket] = []

    offset = 0
    endian = "<"
    link_types: dict[int, int] = {}
    ts_resolutions: dict[int, float] = {}  # interface_id -> seconds per tick

    while offset + 12 <= len(data):
        block_type = struct.unpack_from("<I", data, offset)[0]
        block_len = struct.unpack_from("<I", data, offset + 4)[0]

        if block_len < 12 or offset + block_len > len(data):
            break

        body = data[offset + 8 : offset + block_len - 4]

        if block_type == _BLOCK_SHB:
            magic = struct.unpack_from("<I", body, 0)[0]
            if magic == _BYTE_ORDER_MAGIC:
                endian = "<"
            elif magic == 0x4D3C2B1A:
                endian = ">"
            block_len = struct.unpack_from(endian + "I", data, offset + 4)[0]
            link_types.clear()
            ts_resolutions.clear()

        elif block_type == _BLOCK_IDB:
            lt = struct.unpack_from(endian + "H", body, 0)[0]
            iface_id = len(link_types)
            link_types[iface_id] = lt
            # Parse options for timestamp resolution
            ts_resolutions[iface_id] = 1e-6  # default: microseconds
            opt_offset = 8  # skip linktype(2) + reserved(2) + snaplen(4)
            while opt_offset + 4 <= len(body):
                opt_code = struct.unpack_from(endian + "H", body, opt_offset)[0]
                opt_len = struct.unpack_from(endian + "H", body, opt_offset + 2)[0]
                if opt_code == 0:  # opt_endofopt
                    break
                if opt_code == 9 and opt_len >= 1:  # if_tsresol
                    tsresol_byte = body[opt_offset + 4]
                    if tsresol_byte & 0x80:
                        ts_resolutions[iface_id] = 2.0 ** -(tsresol_byte & 0x7F)
                    else:
                        ts_resolutions[iface_id] = 10.0 ** -tsresol_byte
                # Advance to next option (padded to 4 bytes)
                opt_offset += 4 + ((opt_len + 3) & ~3)

        elif block_type == _BLOCK_EPB:
            iface_id = struct.unpack_from(endian + "I", body, 0)[0]
            ts_high = struct.unpack_from(endian + "I", body, 4)[0]
            ts_low = struct.unpack_from(endian + "I", body, 8)[0]
            captured_len = struct.unpack_from(endian + "I", body, 12)[0]

            ts_ticks = (ts_high << 32) | ts_low
            ts_sec = ts_ticks * ts_resolutions.get(iface_id, 1e-6)

            pkt = body[20 : 20 + captured_len]

            if link_types.get(iface_id) == _LINKTYPE_USBPCAP and len(pkt) >= 27:
                hdr_len = struct.unpack_from("<H", pkt, 0)[0]
                info = pkt[16]
                device = struct.unpack_from("<H", pkt, 19)[0]
                endpoint = pkt[21]
                transfer = pkt[22]
                data_len = struct.unpack_from("<I", pkt, 23)[0]

                ep_number = endpoint & 0x0F
                is_out = (endpoint & 0x80) == 0
                is_submit = (info & 0x01) == 0
                direction = "OUT" if is_out else "IN"

                if (transfer == _TRANSFER_INTERRUPT
                        and data_len > 0
                        and hdr_len + data_len <= len(pkt)):
                    payload = pkt[hdr_len : hdr_len + data_len]
                    packets.append(UsbPacket(
                        timestamp=ts_sec,
                        device=device,
                        endpoint=ep_number,
                        direction=direction,
                        is_submit=is_submit,
                        payload=bytes(payload),
                    ))

        offset += block_len

    return packets


# ============================================================
# HID packet decoder
# ============================================================

def decode_sayo_packet(packet: UsbPacket) -> list[DecodedCommand]:
    """
    Decode a SAYO HID packet into its TLV commands.

    Args:
        packet: UsbPacket with raw HID payload.

    Returns:
        List of DecodedCommand found in the packet.
    """
    payload = packet.payload
    if len(payload) < 4:
        return []

    # Check for known report IDs
    report_id = payload[0]
    if report_id not in (_REPORT_ID_HIGHSPEED, _REPORT_ID_NORMAL):
        return []

    commands: list[DecodedCommand] = []
    offset = 4  # skip report_id(1) + echo(1) + checksum(2)

    while offset + 4 <= len(payload):
        cmd_len = struct.unpack_from("<H", payload, offset)[0]
        if cmd_len < 4:
            break  # end of commands or padding

        cmd_id = payload[offset + 2]
        index = payload[offset + 3]
        cmd_data = payload[offset + 4 : offset + cmd_len]

        commands.append(DecodedCommand(
            timestamp=packet.timestamp,
            cmd_id=cmd_id,
            index=index,
            data=bytes(cmd_data),
            raw_packet=payload,
        ))

        # Advance with 4-byte alignment
        aligned_len = (cmd_len + 3) & ~3
        offset += aligned_len

    return commands


# ============================================================
# Field analysis engine
# ============================================================

_KNOWN_FIELDS: dict[int, list[tuple[int, int, str, str]]] = {
    # cmd_id -> [(offset, size, type, label), ...]
    CmdId.SCREEN_MAIN: [
        (0, 4, "uint32_le", "element_type"),
        (4, 2, "uint16_le", "width"),
        (6, 2, "uint16_le", "height"),
        (8, 2, "uint16_le", "x_position"),
        (10, 2, "uint16_le", "y_position"),
        (12, 2, "uint16_le", "color"),
    ],
    CmdId.KEY: [
        (0x1C, 1, "uint8", "arg0 (V0)"),
    ],
    CmdId.SYS_INFO: [
        (0, 2, "uint16_le", "display_width"),
        (2, 2, "uint16_le", "display_height"),
        (4, 2, "uint16_le", "unknown_60"),
        (6, 2, "uint16_le", "hw_id"),
        (8, 4, "uint32_le", "uptime_s"),
        (12, 2, "uint16_le", "vid"),
        (14, 2, "uint16_le", "pid"),
        (36, 2, "uint16_le", "config_crc"),
    ],
    CmdId.SETTING: [
        (0, 2, "uint16_le", "host_width"),
        (2, 2, "uint16_le", "host_height"),
        (8, 1, "uint8", "brightness?"),
        (9, 3, "bytes", "color_rgb?"),
    ],
}


def _read_field(data: bytes, offset: int, size: int) -> int:
    if offset + size > len(data):
        return 0
    if size == 1:
        return data[offset]
    elif size == 2:
        return struct.unpack_from("<H", data, offset)[0]
    elif size == 4:
        return struct.unpack_from("<I", data, offset)[0]
    return 0


def _auto_detect_fields(data_samples: list[bytes]) -> list[FieldAnalysis]:
    """
    Auto-detect fields in command data by analyzing value patterns.

    Strategy:
    1. Use known field definitions if available.
    2. For unknown regions, try uint16_le scanning and check for varying values.
    3. Group consecutive zero bytes as "reserved".
    """
    if not data_samples:
        return []

    min_len = min(len(d) for d in data_samples)
    fields: list[FieldAnalysis] = []

    # Scan in uint16_le chunks (most common field size in this protocol)
    offset = 0
    while offset + 2 <= min_len:
        # Try uint32 first (if aligned and values fit)
        if offset + 4 <= min_len:
            vals_32 = [_read_field(d, offset, 4) for d in data_samples]
            vals_16_lo = [_read_field(d, offset, 2) for d in data_samples]
            vals_16_hi = [_read_field(d, offset + 2, 2) for d in data_samples]

            # If high word is always 0 and low word varies, it could be uint32
            # but is more likely uint16 + uint16(zero)
            if any(v != 0 for v in vals_16_hi):
                # High word has values — treat as uint32
                is_const = len(set(vals_32)) == 1
                fields.append(FieldAnalysis(
                    offset=offset, size=4, type_name="uint32_le",
                    values=vals_32, is_constant=is_const,
                ))
                offset += 4
                continue

        # Default to uint16
        vals = [_read_field(d, offset, 2) for d in data_samples]
        is_const = len(set(vals)) == 1
        fields.append(FieldAnalysis(
            offset=offset, size=2, type_name="uint16_le",
            values=vals, is_constant=is_const,
        ))
        offset += 2

    # Handle trailing odd byte
    if offset < min_len:
        vals = [d[offset] for d in data_samples]
        is_const = len(set(vals)) == 1
        fields.append(FieldAnalysis(
            offset=offset, size=1, type_name="uint8",
            values=vals, is_constant=is_const,
        ))

    return fields


def _apply_known_labels(fields: list[FieldAnalysis], cmd_id: int) -> None:
    """Apply known field labels from our protocol knowledge."""
    known = _KNOWN_FIELDS.get(cmd_id, [])
    for koff, ksize, ktype, klabel in known:
        for f in fields:
            if f.offset == koff and f.size == ksize:
                f.label = klabel
                break


def _find_zero_ranges(fields: list[FieldAnalysis]) -> list[tuple[int, int]]:
    """Find contiguous ranges of constant-zero fields."""
    ranges: list[tuple[int, int]] = []
    start = None
    for f in fields:
        if f.is_constant and all(v == 0 for v in f.values):
            if start is None:
                start = f.offset
        else:
            if start is not None:
                ranges.append((start, f.offset))
                start = None
    if start is not None:
        last = fields[-1]
        ranges.append((start, last.offset + last.size))
    return ranges


def analyze_commands(commands: list[DecodedCommand]) -> list[CommandGroupAnalysis]:
    """
    Group commands by (cmd_id, index) and analyze field patterns.

    Returns:
        List of CommandGroupAnalysis, one per unique (cmd_id, index).
    """
    # Group by (cmd_id, index)
    groups: dict[tuple[int, int], list[DecodedCommand]] = {}
    for cmd in commands:
        key = (cmd.cmd_id, cmd.index)
        groups.setdefault(key, []).append(cmd)

    results: list[CommandGroupAnalysis] = []
    for (cmd_id, index), cmds in sorted(groups.items()):
        data_samples = [c.data for c in cmds]
        if not data_samples:
            continue

        fields = _auto_detect_fields(data_samples)
        _apply_known_labels(fields, cmd_id)

        constant = [f for f in fields if f.is_constant]
        varying = [f for f in fields if not f.is_constant]
        zero_ranges = _find_zero_ranges(fields)

        try:
            name = CmdId(cmd_id).name
        except ValueError:
            name = f"UNKNOWN_0x{cmd_id:02X}"

        results.append(CommandGroupAnalysis(
            cmd_id=cmd_id,
            cmd_name=name,
            index=index,
            count=len(cmds),
            data_length=min(len(d) for d in data_samples),
            constant_fields=constant,
            varying_fields=varying,
            zero_ranges=zero_ranges,
        ))

    return results


# ============================================================
# High-level analysis + report formatting
# ============================================================

def analyze_pcapng(
    filepath: str | Path,
    device: int | None = None,
) -> str:
    """
    Full analysis pipeline: parse pcapng -> decode -> analyze -> report.

    Args:
        filepath: Path to .pcapng capture file.
        device: USB device address to filter (None = auto-detect SAYO).

    Returns:
        Human-readable analysis report string.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return f"File not found: {filepath}"

    # Step 1: Parse pcapng
    usb_packets = parse_pcapng(filepath)
    if not usb_packets:
        return (
            f"No USB interrupt transfer packets found in {filepath.name}.\n"
            f"Make sure this is a USBPcap pcapng file."
        )

    # Step 2: Filter for SAYO packets (report_id 0x22 or 0x21)
    sayo_packets = []
    for pkt in usb_packets:
        if device is not None and pkt.device != device:
            continue
        if pkt.report_id in (_REPORT_ID_HIGHSPEED, _REPORT_ID_NORMAL):
            sayo_packets.append(pkt)

    # If no device filter, try to auto-detect by report ID
    if not sayo_packets and device is None:
        # Show what we found
        devices = set(p.device for p in usb_packets)
        lines = [
            f"Found {len(usb_packets)} USB packets but none with SAYO report IDs (0x22/0x21).",
            f"Devices seen: {', '.join(str(d) for d in sorted(devices))}",
            f"Try specifying --device <addr> to filter.",
        ]
        return "\n".join(lines)

    # Separate OUT (host->device) and IN (device->host)
    out_packets = [p for p in sayo_packets if p.direction == "OUT" and p.is_submit]
    in_packets = [p for p in sayo_packets if p.direction == "IN" or not p.is_submit]

    # Step 3: Decode commands
    all_commands: list[DecodedCommand] = []
    for pkt in out_packets:
        all_commands.extend(decode_sayo_packet(pkt))

    in_commands: list[DecodedCommand] = []
    for pkt in in_packets:
        in_commands.extend(decode_sayo_packet(pkt))

    # Step 4: Analyze
    out_analysis = analyze_commands(all_commands)
    in_analysis = analyze_commands(in_commands)

    # Step 5: Format report
    return _format_report(
        filepath.name, usb_packets, sayo_packets,
        out_packets, in_packets,
        all_commands, in_commands,
        out_analysis, in_analysis,
    )


def _format_report(
    filename: str,
    all_usb: list[UsbPacket],
    sayo_packets: list[UsbPacket],
    out_packets: list[UsbPacket],
    in_packets: list[UsbPacket],
    out_commands: list[DecodedCommand],
    in_commands: list[DecodedCommand],
    out_analysis: list[CommandGroupAnalysis],
    in_analysis: list[CommandGroupAnalysis],
) -> str:
    """Format the analysis as a human-readable report."""
    lines: list[str] = []
    w = lines.append

    # Header
    w(f"{'=' * 60}")
    w(f"  SAYO Device Packet Analysis: {filename}")
    w(f"{'=' * 60}")
    w("")

    # Summary
    w(f"Total USB packets:  {len(all_usb)}")
    w(f"SAYO packets:       {len(sayo_packets)}")
    w(f"  OUT (host->dev):  {len(out_packets)} ({len(out_commands)} commands)")
    w(f"  IN  (dev->host):  {len(in_packets)} ({len(in_commands)} commands)")

    if sayo_packets:
        t0 = min(p.timestamp for p in sayo_packets)
        t1 = max(p.timestamp for p in sayo_packets)
        w(f"Time span:          {t1 - t0:.2f}s")

        devices = set(p.device for p in sayo_packets)
        w(f"Device address(es): {', '.join(str(d) for d in sorted(devices))}")

    w("")

    # Timeline
    w(f"{'─' * 60}")
    w("  PACKET TIMELINE")
    w(f"{'─' * 60}")
    t0 = min(p.timestamp for p in sayo_packets) if sayo_packets else 0
    for pkt in out_packets + in_packets:
        cmds = decode_sayo_packet(pkt)
        for cmd in cmds:
            arrow = "→" if pkt.direction == "OUT" else "←"
            w(f"  [{pkt.timestamp - t0:7.2f}s] {arrow} {cmd.cmd_name} (0x{cmd.cmd_id:02X})"
              f" idx={cmd.index} [{len(cmd.data)} bytes]")
    w("")

    # OUT command analysis
    if out_analysis:
        w(f"{'─' * 60}")
        w("  OUT COMMANDS (host -> device)")
        w(f"{'─' * 60}")
        for ga in out_analysis:
            _format_group(ga, lines)

    # IN command analysis
    if in_analysis:
        w(f"{'─' * 60}")
        w("  IN COMMANDS (device -> host)")
        w(f"{'─' * 60}")
        for ga in in_analysis:
            _format_group(ga, lines)

    return "\n".join(lines)


# ============================================================
# Live packet decoder (no pcapng file needed)
# ============================================================

def decode_raw_response(data: bytes) -> str:
    """
    Decode a raw HID response packet and format a human-readable breakdown.

    Takes the raw bytes received from the device (without report_id prefix
    if hidapi already stripped it, or with it) and decodes all TLV commands,
    applying known field labels.

    Args:
        data: Raw HID response bytes from device.

    Returns:
        Formatted multi-line string showing decoded fields.
    """
    if not data or len(data) < 4:
        return "  (empty or too short)"

    lines: list[str] = []
    w = lines.append

    # Determine if first byte is report_id or if hidapi stripped it
    # hidapi on some platforms strips report_id, on others it doesn't
    # We check: if byte 0 is 0x22 or 0x21, treat as report_id present
    if data[0] in (_REPORT_ID_HIGHSPEED, _REPORT_ID_NORMAL):
        echo = data[1]
        checksum = struct.unpack_from("<H", data, 2)[0]
        w(f"  report_id=0x{data[0]:02X}  echo=0x{echo:02X}  checksum=0x{checksum:04X}")
        cmd_offset = 4
    else:
        # Assume hidapi stripped report_id, first byte is echo
        echo = data[0]
        checksum = struct.unpack_from("<H", data, 1)[0] if len(data) >= 3 else 0
        w(f"  echo=0x{echo:02X}  (report_id stripped by hidapi)")
        cmd_offset = 3

    # Decode TLV commands
    cmd_count = 0
    while cmd_offset + 4 <= len(data):
        cmd_len = struct.unpack_from("<H", data, cmd_offset)[0]
        if cmd_len < 4:
            break

        cmd_id = data[cmd_offset + 2]
        index = data[cmd_offset + 3]
        cmd_data = data[cmd_offset + 4 : cmd_offset + cmd_len]

        try:
            cmd_name = CmdId(cmd_id).name
        except ValueError:
            cmd_name = f"UNKNOWN_0x{cmd_id:02X}"

        cmd_count += 1
        w("")
        w(f"  [{cmd_count}] {cmd_name} (0x{cmd_id:02X}) index={index}  [{len(cmd_data)} bytes]")

        if cmd_data:
            # Apply known field labels
            known = _KNOWN_FIELDS.get(cmd_id, [])
            if known:
                for koff, ksize, ktype, klabel in known:
                    if koff + ksize <= len(cmd_data):
                        val = _read_field(cmd_data, koff, ksize)
                        if ksize == 1:
                            val_str = f"0x{val:02X} (={val})"
                        elif ksize == 2:
                            val_str = f"0x{val:04X} (={val})"
                        elif ksize == 4:
                            val_str = f"0x{val:08X} (={val})"
                        else:
                            val_str = cmd_data[koff:koff + ksize].hex(" ")
                        w(f"      [{koff:2d}-{koff + ksize - 1:2d}] {ktype:10s} = {val_str}  ({klabel})")
            else:
                # No known fields — dump first 32 bytes as hex
                w(f"      hex: {cmd_data[:32].hex(' ')}")
                if len(cmd_data) > 32:
                    w(f"      ... ({len(cmd_data)} bytes total)")

        cmd_offset += (cmd_len + 3) & ~3

    if cmd_count == 0:
        w("  (no TLV commands decoded)")
        w(f"  raw hex: {data[:64].hex(' ')}")

    return "\n".join(lines)


def _format_group(ga: CommandGroupAnalysis, lines: list[str]) -> None:
    """Format a single CommandGroupAnalysis into lines."""
    w = lines.append
    w("")
    w(f"  {ga.cmd_name} (0x{ga.cmd_id:02X}) index={ga.index}"
      f" -- {ga.count} packet(s), {ga.data_length} bytes data")
    w("")

    if ga.constant_fields:
        # Filter out zero-range fields for cleaner output
        zero_offsets = set()
        for start, end in ga.zero_ranges:
            for o in range(start, end):
                zero_offsets.add(o)

        non_zero_const = [f for f in ga.constant_fields
                          if f.offset not in zero_offsets]
        if non_zero_const:
            w("    Constant fields:")
            for f in non_zero_const:
                label = f"  ({f.label})" if f.label else ""
                w(f"      [{f.offset:2d}-{f.offset + f.size - 1:2d}]"
                  f"  {f.type_name:10s}  = {f.value_summary()}{label}")

    if ga.varying_fields:
        w("    Varying fields:")
        for f in ga.varying_fields:
            label = f"  ({f.label})" if f.label else ""
            w(f"      [{f.offset:2d}-{f.offset + f.size - 1:2d}]"
              f"  {f.type_name:10s}  values: {f.value_summary()}{label}")

    if ga.zero_ranges:
        ranges_str = ", ".join(f"[{s}-{e - 1}]" for s, e in ga.zero_ranges)
        w(f"    Always zero: {ranges_str}")

    w("")

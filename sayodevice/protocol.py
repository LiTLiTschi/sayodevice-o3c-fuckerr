"""
SayoDevice O3C - Protocol constants and packet construction.

Protocol docs:
    https://gist.github.com/khang06/6186543b560548370ce7cc08cad7f710
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

# ============================================================
# Device identifiers
# ============================================================
SAYO_VID = 0x8089
SAYO_PID = 0x0009

# ============================================================
# Usage pages (API v2)
# ============================================================
class UsagePage(IntEnum):
    HIGHSPEED = 0xFF12  # 8000Hz polling, 1024-byte packets, report_id=0x22
    NORMAL    = 0xFF11  # Other polling rates, 64-byte packets, report_id=0x21
    V1        = 0xFF00  # API v1 (legacy), 64-byte packets, report_id=0x02

    @property
    def report_id(self) -> int:
        return {
            UsagePage.HIGHSPEED: 0x22,
            UsagePage.NORMAL:    0x21,
            UsagePage.V1:        0x02,
        }[self]

    @property
    def packet_size(self) -> int:
        return {
            UsagePage.HIGHSPEED: 1024,
            UsagePage.NORMAL:    64,
            UsagePage.V1:        64,
        }[self]

# Priority order for selecting config interface
USAGE_PAGE_PRIORITY = [UsagePage.HIGHSPEED, UsagePage.NORMAL, UsagePage.V1]

# ============================================================
# Command IDs (API v2)
# ============================================================
class CmdId(IntEnum):
    INFO           = 0x00
    DEVICE_NAME    = 0x01
    SYS_INFO       = 0x02
    SETTING        = 0x03
    BLE            = 0x04
    DEVICE_LOCK    = 0x05
    DEVICE_UNLOCK  = 0x06
    SAVE           = 0x0D
    SYS_CONTROL    = 0x0E
    KEY            = 0x10
    LIGHT          = 0x11
    PALETTE        = 0x12
    SCRIPT_PREVIEW = 0x19
    SCRIPT_STEP    = 0x1A
    KEY_STATUS     = 0x1E
    KEY_DATA       = 0x1F
    IMAGE          = 0x20
    SCREEN_START   = 0x21
    SCREEN_MAIN    = 0x22
    SCREEN_SLEEP   = 0x23
    DISPLAY        = 0x25

# ============================================================
# Default echo byte (observed in captures)
# ============================================================
DEFAULT_ECHO = 0x12

# ============================================================
# Packet construction
# ============================================================

def calc_checksum(packet: bytearray) -> int:
    """
    Calculate API v2 checksum.

    Algorithm:
        1. Zero checksum field (bytes 2-3)
        2. Interpret entire packet as LE 16-bit words
        3. Sum all words
        4. Return lower 16 bits
    """
    buf = bytearray(packet)
    buf[2] = 0
    buf[3] = 0
    total = 0
    for i in range(0, len(buf), 2):
        total += buf[i] | (buf[i + 1] << 8)
    return total & 0xFFFF


@dataclass
class HidCommand:
    """A single command within an API v2 HID packet."""
    cmd_id: int
    index: int = 0
    data: bytes = b""

    def encode(self) -> bytes:
        """Encode to wire format (padded to 4-byte alignment)."""
        total_len = 4 + len(self.data)
        padded_len = (total_len + 3) & ~3
        buf = bytearray(padded_len)
        struct.pack_into("<H", buf, 0, total_len)
        buf[2] = self.cmd_id
        buf[3] = self.index
        buf[4 : 4 + len(self.data)] = self.data
        return bytes(buf)


def build_packet(
    usage_page: UsagePage,
    commands: list[HidCommand],
    echo: int = DEFAULT_ECHO,
) -> bytearray:
    """
    Build a complete HID packet for the given usage page.

    Returns:
        bytearray of the correct size (1024 or 64 bytes),
        with report_id at byte 0 and checksum at bytes 2-3.
    """
    pkt_size = usage_page.packet_size
    report_id = usage_page.report_id

    packet = bytearray(pkt_size)
    packet[0] = report_id
    packet[1] = echo

    offset = 4
    for cmd in commands:
        encoded = cmd.encode()
        if offset + len(encoded) > pkt_size:
            raise ValueError(
                f"Commands exceed packet size ({pkt_size}). "
                f"Need {offset + len(encoded)} bytes."
            )
        packet[offset : offset + len(encoded)] = encoded
        offset += len(encoded)

    checksum = calc_checksum(packet)
    struct.pack_into("<H", packet, 2, checksum)
    return packet


# ============================================================
# Key configuration template
# ============================================================

# Captured from Wireshark when setting Arg0=0 via the web GUI.
# This is the 56-byte cmd_data portion of the Key (0x10) command.
KEY_CONFIG_TEMPLATE = bytearray([
    0x01, 0x00, 0x00, 0x00, 0xE8, 0x03, 0xB8, 0x0B,  # [0x00]
    0x08, 0x07, 0x08, 0x07, 0x64, 0x00, 0x00, 0x00,  # [0x08]
    0x00, 0x00, 0x00, 0x00, 0x00, 0x8C, 0x00, 0x00,  # [0x10]
    0x40, 0x00, 0x00, 0x00, 0x00,                      # [0x18] Arg0 @ 0x1C
    0x01, 0x00, 0x00,
    0x40, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00,  # [0x20]
    0x40, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00,  # [0x28]
    0x43, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  # [0x30]
])

# Offsets within key config data for the 4 script parameters
ARG0_OFFSET = 0x1C  # V0
# ARG1/2/3 offsets not yet confirmed from captures


def build_screen_element(
    x: int = 0,
    y: int = 0,
    width: int = 40,
    height: int = 40,
    color: int = 0xFFFF,
    element_type: int = 1,
) -> bytes:
    """
    Build SCREEN_MAIN (0x22) command data for a screen element.

    The data portion is 56 bytes (command total = 60 bytes with 4-byte header).
    Layout (offsets relative to command data start):
        0-3:   element_type  (uint32_le, 1 = Pure Color)
        4-5:   width         (uint16_le, pixels)
        6-7:   height        (uint16_le, pixels)
        8-11:  x_position    (uint32_le, pixels)
        12-13: color         (uint16_le, e.g. 0xFFFF = white)
        14-55: reserved      (zeros)

    Args:
        x: X-position in pixels.
        y: Y-position in pixels (unconfirmed offset, not yet wired).
        width: Element width in pixels.
        height: Element height in pixels.
        color: Colour value (uint16, 0xFFFF = white).
        element_type: Element type (1 = Pure Color).

    Returns:
        56-byte payload ready for HidCommand.data.
    """
    data = bytearray(56)
    struct.pack_into("<I", data, 0, element_type)
    struct.pack_into("<H", data, 4, width)
    struct.pack_into("<H", data, 6, height)
    struct.pack_into("<I", data, 8, x)
    struct.pack_into("<H", data, 12, color)
    # bytes 14-55 stay zero (reserved / y-position TBD)
    return bytes(data)


def build_key_config(
    arg0: int = 0,
    arg1: int | None = None,
    arg2: int | None = None,
    arg3: int | None = None,
    template: bytearray | None = None,
) -> bytes:
    """
    Build Key command data with specified script arguments.

    Args:
        arg0: V0 parameter (0-255)
        arg1: V1 parameter (not yet confirmed in protocol)
        arg2: V2 parameter (not yet confirmed in protocol)
        arg3: V3 parameter (not yet confirmed in protocol)
        template: Custom template (default: captured from web GUI)
    """
    data = bytearray(template or KEY_CONFIG_TEMPLATE)
    data[ARG0_OFFSET] = arg0 & 0xFF
    # TODO: set arg1/2/3 once offsets are confirmed
    return bytes(data)

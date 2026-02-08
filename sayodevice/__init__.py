"""
SayoDevice - Python library for controlling SayoDevice O3C via USB HID.

Quick start::

    from sayodevice import SayoDevice

    with SayoDevice.open() as dev:
        info = dev.get_info()
        dev.set_key_arg0(128)
        dev.set_screen_element(x=120, y=40, refresh=True)

Capture & diff::

    from sayodevice import capture_snapshot, diff_snapshots, save_discovery

    baseline = capture_snapshot(dev)
    # ... make a change on the device ...
    snapshot = capture_snapshot(dev)
    changes = diff_snapshots(baseline, snapshot)

AI analysis::

    from sayodevice import is_claude_available, analyze_diff

TUI::

    # Launch: sayodevice
    # Classic CLI: sayodevice --classic
"""

__version__ = "1.0.0"

# --- Protocol layer ---
from .protocol import (
    SAYO_VID,
    SAYO_PID,
    UsagePage,
    CmdId,
    HidCommand,
    build_packet,
    build_key_config,
    build_screen_element,
    calc_checksum,
    SysInfo,
    DeviceSetting,
    parse_sys_info,
    parse_setting,
    rgb_to_565,
    rgb565_to_rgb,
    hex_color_to_565,
)

# --- Device layer ---
from .device import SayoDevice, SayoInterface, DeviceInfo

# --- Analyzer (kept: decode engine, removed: pcapng parser) ---
from .analyzer import decode_raw_response, analyze_commands

# --- Snapshot & diff engine ---
from .tui.snapshots import (
    Snapshot,
    Discovery,
    FieldChange,
    capture_snapshot,
    diff_snapshots,
    get_changed_byte_offsets,
    save_discovery,
    list_discoveries,
    SNAPSHOTS_DIR,
    PROBE_CMDS,
)

# --- Claude AI integration ---
from .tui.claude import (
    is_claude_available,
    ask_claude,
    analyze_diff,
    format_discovery_for_claude,
)

__all__ = [
    # Version
    "__version__",
    # Protocol
    "SAYO_VID",
    "SAYO_PID",
    "UsagePage",
    "CmdId",
    "HidCommand",
    "build_packet",
    "build_key_config",
    "build_screen_element",
    "calc_checksum",
    "SysInfo",
    "DeviceSetting",
    "parse_sys_info",
    "parse_setting",
    "rgb_to_565",
    "rgb565_to_rgb",
    "hex_color_to_565",
    # Device
    "SayoDevice",
    "SayoInterface",
    "DeviceInfo",
    # Analyzer
    "decode_raw_response",
    "analyze_commands",
    # Snapshots & diff
    "Snapshot",
    "Discovery",
    "FieldChange",
    "capture_snapshot",
    "diff_snapshots",
    "get_changed_byte_offsets",
    "save_discovery",
    "list_discoveries",
    "SNAPSHOTS_DIR",
    "PROBE_CMDS",
    # Claude AI
    "is_claude_available",
    "ask_claude",
    "analyze_diff",
    "format_discovery_for_claude",
]

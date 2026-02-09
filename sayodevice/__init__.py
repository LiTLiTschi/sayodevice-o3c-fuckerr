"""
SayoDevice - Python library for controlling SayoDevice O3C via USB HID.

Quick start::

    from sayodevice import SayoDevice

    with SayoDevice.open() as dev:
        info = dev.get_info()
        dev.set_key_arg0(128)
        dev.set_screen_element(x=120, color='#FF0000', refresh=True)

Screen helpers::

    with SayoDevice.open() as dev:
        dev.fill_screen('#1295FF')              # full background
        dev.draw_rect(0, 0, 40, 40, '#000000')  # foreground rectangle
        dev.clear_layer(15)                      # remove foreground

Input & events::

    from sayodevice import SayoDevice, DeviceListener

    with SayoDevice.open() as dev:
        listener = DeviceListener(dev, poll_interval_ms=50)
        listener.on_fn_change(lambda e: print(f"FN: {e.old_fn} -> {e.new_fn}"))
        listener.start()
        # ... your app loop ...
        listener.stop()

Named setups::

    from sayodevice import DeviceSetup, ScreenElement, save_setup, load_setup

    setup = DeviceSetup(name="seq-gate", screen_elements=[
        ScreenElement(x=0, y=0, width=160, height=80, color="#1295FF", element_index=14),
        ScreenElement(x=0, y=0, width=40, height=40, color="#000000", element_index=15),
    ])
    save_setup(setup)

    with SayoDevice.open() as dev:
        load_setup("seq-gate").apply(dev)

Capture & diff::

    from sayodevice import capture_snapshot, diff_snapshots

    baseline = capture_snapshot(dev)
    # ... make a change on the device ...
    snapshot = capture_snapshot(dev)
    changes = diff_snapshots(baseline, snapshot)

TUI::

    # Launch: sayodevice
    # Classic CLI: sayodevice --classic
"""

__version__ = "1.3.2"

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
from .device import SayoDevice, SayoInterface, DeviceInfo, ButtonState

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

# --- Named setups ---
from .setup import (
    ScreenElement,
    KeyConfig,
    DeviceSetup,
    save_setup,
    load_setup,
    list_setups,
    delete_setup,
    SETUPS_DIR,
)

# --- Event listener ---
from .listener import (
    DeviceListener,
    DeviceEvent,
    FnChangeEvent,
    InfoUpdateEvent,
    ButtonEvent,
    RawPacketEvent,
)

# --- Live USB sniffer ---
from .tui.sniffer import (
    find_tshark,
    list_usb_interfaces,
    check_sniff_prerequisites,
    TsharkSniffer,
)

# --- ADSR envelopes ---
from .adsr import (
    CurveType,
    ADSREnvelope,
    EnvelopeGenerator,
    EnvelopeStage,
    envelope_to_bars,
    ADSR_COLORS,
    ADSR_LABELS,
    ADSR_PARAM_NAMES,
    ADSR_PARAM_RANGES,
)

# --- MIDI bridge (optional: requires mido + python-rtmidi) ---
try:
    from .midi import (
        MidiBridge,
        MidiMapping,
        KnobMapping,
        list_midi_ports,
    )
    _MIDI_AVAILABLE = True
except ImportError:
    _MIDI_AVAILABLE = False

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
    "ButtonState",
    # Analyzer
    "decode_raw_response",
    "analyze_commands",
    # Named setups
    "ScreenElement",
    "KeyConfig",
    "DeviceSetup",
    "save_setup",
    "load_setup",
    "list_setups",
    "delete_setup",
    "SETUPS_DIR",
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
    # Event listener
    "DeviceListener",
    "DeviceEvent",
    "FnChangeEvent",
    "InfoUpdateEvent",
    "ButtonEvent",
    "RawPacketEvent",
    # Live USB sniffer
    "find_tshark",
    "list_usb_interfaces",
    "check_sniff_prerequisites",
    "TsharkSniffer",
    # ADSR envelopes
    "CurveType",
    "ADSREnvelope",
    "EnvelopeGenerator",
    "EnvelopeStage",
    "envelope_to_bars",
    "ADSR_COLORS",
    "ADSR_LABELS",
    "ADSR_PARAM_NAMES",
    "ADSR_PARAM_RANGES",
]

# Conditionally add MIDI exports
if _MIDI_AVAILABLE:
    __all__ += [
        "MidiBridge",
        "MidiMapping",
        "KnobMapping",
        "list_midi_ports",
    ]

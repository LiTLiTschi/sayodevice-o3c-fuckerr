"""
SayoDevice - Python library for controlling SayoDevice O3C via USB HID.

Quick start::

    from sayodevice import SayoDevice

    with SayoDevice.open() as dev:
        dev.set_key_arg0(128)

    # Or use the CLI:
    #   sayodevice set-arg0 128
    #   sayodevice interactive
"""

__version__ = "0.7.1"

from .protocol import (
    UsagePage,
    CmdId,
    HidCommand,
    build_packet,
    build_key_config,
    build_screen_element,
    calc_checksum,
    SAYO_VID,
    SAYO_PID,
    SysInfo,
    DeviceSetting,
    parse_sys_info,
    parse_setting,
    rgb_to_565,
    rgb565_to_rgb,
    hex_color_to_565,
)
from .device import SayoDevice, SayoInterface, DeviceInfo
from .analyzer import analyze_pcapng, parse_pcapng, decode_sayo_packet, analyze_commands, decode_raw_response

__all__ = [
    "SayoDevice",
    "SayoInterface",
    "DeviceInfo",
    "UsagePage",
    "CmdId",
    "HidCommand",
    "build_packet",
    "build_key_config",
    "build_screen_element",
    "calc_checksum",
    "SAYO_VID",
    "SAYO_PID",
    "SysInfo",
    "DeviceSetting",
    "parse_sys_info",
    "parse_setting",
    "rgb_to_565",
    "rgb565_to_rgb",
    "hex_color_to_565",
    "analyze_pcapng",
    "parse_pcapng",
    "decode_sayo_packet",
    "analyze_commands",
    "decode_raw_response",
]

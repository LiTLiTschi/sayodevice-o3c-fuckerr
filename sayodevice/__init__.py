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

__version__ = "0.4.0"

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
)
from .device import SayoDevice, SayoInterface, DeviceInfo

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
]

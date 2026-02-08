"""
SayoDevice O3C - Live USB sniffer via tshark.

Captures USB traffic in real-time by running tshark as a subprocess.
Replaces the user's manual Wireshark workflow::

    _ws.col.info == "URB_INTERRUPT out" && usb.src=="host"

Usage::

    from sayodevice.tui.sniffer import TsharkSniffer, check_sniff_prerequisites

    ok, msg = check_sniff_prerequisites()
    if not ok:
        print(msg)
    else:
        sniffer = TsharkSniffer(on_packet=lambda pkt, cmds: print(pkt))
        sniffer.start()
        sniffer.run_blocking()  # blocks until stop()
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from ..analyzer import UsbPacket, DecodedCommand, decode_sayo_packet

# Report IDs we care about
_REPORT_ID_HIGHSPEED = 0x22
_REPORT_ID_NORMAL = 0x21


# ============================================================
# tshark discovery
# ============================================================

def find_tshark() -> str | None:
    """Find tshark binary. Checks PATH, then common Windows install locations."""
    path = shutil.which("tshark")
    if path:
        return path
    # Windows-specific fallback locations
    for candidate in [
        r"C:\Program Files\Wireshark\tshark.exe",
        r"C:\Program Files (x86)\Wireshark\tshark.exe",
    ]:
        if Path(candidate).exists():
            return candidate
    return None


def list_usb_interfaces(tshark_path: str | None = None) -> list[tuple[str, str]]:
    """
    List available USB capture interfaces via ``tshark -D``.

    Returns list of ``(interface_id, display_name)`` for USBPcap (Windows)
    or usbmon (Linux) interfaces.
    """
    tshark = tshark_path or find_tshark()
    if not tshark:
        return []

    try:
        result = subprocess.run(
            [tshark, "-D"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    interfaces: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        low = line.lower()
        # Windows: "1. \\.\USBPcap1 (USBPcap1)"
        # Linux:   "3. usbmon0"
        if "usbpcap" in low or "usbmon" in low:
            parts = line.split(".", 1)
            if len(parts) >= 2:
                iface_str = parts[1].strip()
                # Extract just the interface name (before any parenthetical)
                iface_id = iface_str.split("(")[0].strip()
                interfaces.append((iface_id, iface_str))
    return interfaces


def check_sniff_prerequisites() -> tuple[bool, str]:
    """
    Check if live USB sniffing is possible.

    Returns:
        ``(ok, human_readable_message)``
    """
    tshark = find_tshark()
    if not tshark:
        return False, (
            "tshark not found. Install Wireshark from https://www.wireshark.org/\n"
            "Make sure 'Add Wireshark to PATH' is checked during installation."
        )

    interfaces = list_usb_interfaces(tshark)
    if not interfaces:
        return False, (
            "No USBPcap/usbmon interfaces found.\n"
            "Windows: Reinstall Wireshark with 'Install USBPcap' checked.\n"
            "Linux: Load the usbmon kernel module: sudo modprobe usbmon"
        )

    names = ", ".join(name for _, name in interfaces)
    return True, f"Ready. Found {len(interfaces)} USB capture interface(s): {names}"


# ============================================================
# TsharkSniffer
# ============================================================

class TsharkSniffer:
    """
    Manages a tshark subprocess for live USB packet capture.

    Args:
        interface: USB capture interface name (e.g. ``USBPcap1``, ``usbmon0``).
            Auto-detected if None.
        on_packet: Callback ``(UsbPacket, list[DecodedCommand]) -> None``
            called for each SAYO packet.
        on_error: Callback ``(str) -> None`` called on errors.
    """

    def __init__(
        self,
        interface: str | None = None,
        on_packet: Callable[[UsbPacket, list[DecodedCommand]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self._tshark_path = find_tshark()
        self._interface = interface
        self._on_packet = on_packet
        self._on_error = on_error

        self._process: subprocess.Popen | None = None
        self._stop_event = threading.Event()
        self._packet_count = 0
        self._sayo_count = 0

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def packet_count(self) -> int:
        """Total USB packets seen (all devices)."""
        return self._packet_count

    @property
    def sayo_count(self) -> int:
        """Packets that decoded as SAYO protocol."""
        return self._sayo_count

    def start(self) -> None:
        """Launch the tshark subprocess."""
        if not self._tshark_path:
            raise RuntimeError("tshark not found")

        # Auto-detect interface if not specified
        if self._interface is None:
            interfaces = list_usb_interfaces(self._tshark_path)
            if not interfaces:
                raise RuntimeError("No USBPcap/usbmon interfaces found")
            self._interface = interfaces[0][0]

        cmd = self._build_cmd()
        kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "bufsize": 1,  # line-buffered
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self._stop_event.clear()
        self._packet_count = 0
        self._sayo_count = 0
        self._process = subprocess.Popen(cmd, **kwargs)

    def stop(self) -> None:
        """Stop the tshark capture."""
        self._stop_event.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def run_blocking(self) -> None:
        """
        Blocking read loop — reads tshark stdout line-by-line until ``stop()`` is called.

        Call this from a background thread (e.g. Textual ``@work(thread=True)``).
        """
        if not self._process or not self._process.stdout:
            return

        try:
            for line in iter(self._process.stdout.readline, ""):
                if self._stop_event.is_set():
                    break
                if not line.strip():
                    continue

                self._packet_count += 1
                packet = self._parse_line(line)
                if packet is None:
                    continue

                # Decode through existing SAYO TLV decoder
                commands = decode_sayo_packet(packet)

                # Filter: only packets with SAYO report IDs
                if not commands and (
                    packet.report_id not in (_REPORT_ID_HIGHSPEED, _REPORT_ID_NORMAL)
                ):
                    continue

                self._sayo_count += 1
                if self._on_packet:
                    self._on_packet(packet, commands)

        except Exception as e:
            if self._on_error and not self._stop_event.is_set():
                self._on_error(str(e))

        # Check if tshark exited with an error
        if self._process and self._process.poll() is not None:
            rc = self._process.returncode
            if rc != 0 and not self._stop_event.is_set():
                stderr = ""
                if self._process.stderr:
                    stderr = self._process.stderr.read()
                if self._on_error:
                    self._on_error(f"tshark exited with code {rc}: {stderr.strip()}")

    def _build_cmd(self) -> list[str]:
        """Build the tshark command line."""
        assert self._tshark_path and self._interface
        return [
            self._tshark_path,
            "-i", self._interface,
            "-Y", "usb.transfer_type == 0x01 && usb.data_len > 0",
            "-T", "fields",
            "-E", "separator=\t",
            "-E", "quote=n",
            "-e", "frame.time_epoch",
            "-e", "usb.device_address",
            "-e", "usb.endpoint_address",
            "-e", "usb.capdata",
            "-e", "usb.data_len",
            "-l",  # line-buffered
        ]

    @staticmethod
    def _parse_line(line: str) -> UsbPacket | None:
        """Parse one line of tshark ``-T fields`` output into a UsbPacket."""
        parts = line.strip().split("\t")
        if len(parts) < 5:
            return None

        try:
            timestamp = float(parts[0]) if parts[0] else 0.0
            device = int(parts[1]) if parts[1] else 0
            ep_raw_str = parts[2].strip()
            capdata_hex = parts[3].strip()
            data_len = int(parts[4]) if parts[4] else 0
        except (ValueError, IndexError):
            return None

        if not capdata_hex or data_len == 0:
            return None

        # Parse endpoint address (may be decimal or 0x hex)
        try:
            if ep_raw_str.startswith("0x") or ep_raw_str.startswith("0X"):
                ep_raw = int(ep_raw_str, 16)
            else:
                ep_raw = int(ep_raw_str)
        except ValueError:
            return None

        # Endpoint bit 7: 0=OUT (host->device), 1=IN (device->host)
        is_out = (ep_raw & 0x80) == 0
        direction = "OUT" if is_out else "IN"
        ep_number = ep_raw & 0x0F

        # capdata uses colon separators: "22:12:ab:cd:..."
        try:
            payload = bytes.fromhex(capdata_hex.replace(":", ""))
        except ValueError:
            return None

        return UsbPacket(
            timestamp=timestamp,
            device=device,
            endpoint=ep_number,
            direction=direction,
            is_submit=is_out,
            payload=payload,
        )

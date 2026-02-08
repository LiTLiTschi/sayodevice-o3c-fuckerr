"""
SayoDevice O3C - Device communication layer.

Usage::

    from sayodevice import SayoDevice

    with SayoDevice.open() as dev:
        info = dev.get_info()
        print(info)
        dev.set_key_arg0(5)
        dev.save()
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Self

try:
    import hid
except ImportError:
    raise ImportError(
        "hidapi is required. Install with: pip install hidapi"
    )

from .protocol import (
    SAYO_VID,
    SAYO_PID,
    UsagePage,
    USAGE_PAGE_PRIORITY,
    CmdId,
    DEFAULT_ECHO,
    HidCommand,
    build_packet,
    build_key_config,
    build_screen_element,
    calc_checksum,
    SysInfo,
    DeviceSetting,
    parse_sys_info,
    parse_setting,
    hex_color_to_565,
)

# ============================================================
# Data classes for parsed responses
# ============================================================

@dataclass
class ButtonState:
    """Current state of all device buttons and knob.

    Decoded from KEY_STATUS (CMD 0x1E, index=0) byte [8].
    Active-low bitmask: bit=0 means pressed, bit=1 means released.

    Bit layout::

        Bit 0 (0x01): button1    (left button)
        Bit 1 (0x02): button2    (middle button)
        Bit 2 (0x04): button3    (right button)
        Bit 3 (0x08): knob_click (knob press)
        Bit 4 (0x10): knob_left  (knob rotate left)
        Bit 5 (0x20): knob_right (knob rotate right)
    """
    button1: bool = False
    button2: bool = False
    button3: bool = False
    knob_click: bool = False
    knob_left: bool = False
    knob_right: bool = False
    raw: int = 0x3F

    @classmethod
    def from_byte(cls, b: int) -> ButtonState:
        """Decode active-low bitmask byte into ButtonState."""
        return cls(
            button1=not bool(b & 0x01),
            button2=not bool(b & 0x02),
            button3=not bool(b & 0x04),
            knob_click=not bool(b & 0x08),
            knob_left=not bool(b & 0x10),
            knob_right=not bool(b & 0x20),
            raw=b,
        )

    @property
    def any_pressed(self) -> bool:
        """True if any button or knob input is active."""
        return (self.button1 or self.button2 or self.button3
                or self.knob_click or self.knob_left or self.knob_right)

    def __str__(self) -> str:
        pressed = []
        if self.button1: pressed.append("btn1")
        if self.button2: pressed.append("btn2")
        if self.button3: pressed.append("btn3")
        if self.knob_click: pressed.append("knob_click")
        if self.knob_left: pressed.append("knob_left")
        if self.knob_right: pressed.append("knob_right")
        return f"ButtonState({', '.join(pressed) or 'none'})"


@dataclass
class DeviceInfo:
    """Parsed response from Info command (0x00)."""
    model_code: int = 0
    firmware_version: int = 0
    battery: int = 0
    fn: int = 0
    cpu_s: int = 0
    cpu_ms: int = 0
    raw: bytes = b""

    def __str__(self) -> str:
        fw_major = (self.firmware_version >> 8) & 0xFF
        fw_minor = self.firmware_version & 0xFF
        return (
            f"DeviceInfo(model=0x{self.model_code:04X}, "
            f"firmware=v{fw_major}.{fw_minor}, "
            f"fn={self.fn})"
        )


@dataclass
class SayoInterface:
    """Discovered HID interface for a SayoDevice."""
    path: bytes
    usage_page: int
    usage: int
    interface_number: int
    product_string: str

    @property
    def is_config(self) -> bool:
        return self.usage_page in (up.value for up in USAGE_PAGE_PRIORITY)

    @property
    def mode(self) -> UsagePage | None:
        try:
            return UsagePage(self.usage_page)
        except ValueError:
            return None

    def __str__(self) -> str:
        mode_str = ""
        if self.mode:
            mode_str = f" [{self.mode.name}]"
        return (
            f"Interface {self.interface_number}: "
            f"UP=0x{self.usage_page:04X}, Usage=0x{self.usage:04X}"
            f"{mode_str} - {self.product_string}"
        )


# ============================================================
# Main device class
# ============================================================

class SayoDevice:
    """
    High-level interface to a SayoDevice O3C.

    Usage::

        # Auto-detect and open
        with SayoDevice.open() as dev:
            dev.set_key_arg0(128)

        # Or manual control
        dev = SayoDevice.open()
        dev.send_info()
        dev.close()

        # Specify interface explicitly
        dev = SayoDevice.open(usage_page=UsagePage.HIGHSPEED)
    """

    def __init__(
        self,
        path: bytes,
        usage_page: UsagePage,
        echo: int = DEFAULT_ECHO,
    ):
        self._path = path
        self._usage_page = usage_page
        self._echo = echo
        self._dev: hid.device | None = None

    @property
    def usage_page(self) -> UsagePage:
        return self._usage_page

    @property
    def packet_size(self) -> int:
        return self._usage_page.packet_size

    @property
    def report_id(self) -> int:
        return self._usage_page.report_id

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    # ---- Context manager ----

    def __enter__(self) -> Self:
        self._open()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ---- Connection management ----

    def _open(self) -> None:
        if self._dev is not None:
            return
        self._dev = hid.device()
        self._dev.open_path(self._path)
        self._dev.set_nonblocking(1)

    def _ensure_open(self) -> hid.device:
        if self._dev is None:
            self._open()
        assert self._dev is not None
        return self._dev

    def close(self) -> None:
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    # ---- Low-level send/receive ----

    def send(self, packet: bytearray) -> int:
        """
        Send a raw HID packet. Returns number of bytes sent.
        On hidapi, first byte of the buffer is the report ID.
        """
        dev = self._ensure_open()
        return dev.write(bytes(packet))

    def receive(self, timeout_ms: int = 200) -> bytes | None:
        """
        Try to read a response. Returns bytes or None if no data.
        """
        dev = self._ensure_open()
        data = dev.read(self.packet_size, timeout_ms=timeout_ms)
        if data:
            return bytes(data)
        return None

    def send_command(
        self,
        commands: list[HidCommand],
        wait_response: bool = True,
        timeout_ms: int = 200,
    ) -> bytes | None:
        """
        Build and send an HID packet with the given commands.
        Optionally waits for a response.
        """
        packet = build_packet(self._usage_page, commands, self._echo)
        result = self.send(packet)
        if result < 0:
            raise IOError(f"HID write failed (result={result})")

        if wait_response:
            time.sleep(0.02)
            return self.receive(timeout_ms)
        return None

    def send_single(
        self,
        cmd_id: int,
        data: bytes = b"",
        index: int = 0,
        wait_response: bool = True,
    ) -> bytes | None:
        """Send a single command and optionally wait for response."""
        return self.send_command(
            [HidCommand(cmd_id, index, data)],
            wait_response=wait_response,
        )

    # ---- High-level commands ----

    def get_info(self) -> DeviceInfo:
        """
        Liest Geräteinformationen (CMD 0x00).

        Returns:
            DeviceInfo: Objekt mit Modell, Firmware, Batterie usw.

        Beispiel:
            info = dev.get_info()
            print(info.model_code, info.firmware_version)
        """
        resp = self.send_single(CmdId.INFO)
        info = DeviceInfo()
        if resp and len(resp) >= 12:
            # Response contains hid_packet_v2_t header + info_res_t
            # Skip packet header (4 bytes) + cmd header (4 bytes)
            payload = resp[8:] if len(resp) > 8 else resp
            info.raw = bytes(resp)
            if len(payload) >= 4:
                info.model_code = payload[0] | (payload[1] << 8)
                info.firmware_version = payload[2] | (payload[3] << 8)
            if len(payload) >= 12:
                info.battery = payload[8]
                info.fn = payload[9]
                info.cpu_s = payload[10]
                info.cpu_ms = payload[11]
        elif resp:
            info.raw = bytes(resp)
        return info

    def set_key_config(
        self,
        arg0: int = 0,
        arg1: int | None = None,
        arg2: int | None = None,
        arg3: int | None = None,
        key_index: int = 0,
        save: bool = False,
    ) -> bytes | None:
        """
        Send Key configuration command (CMD 0x10).

        Args:
            arg0: V0 script parameter (0-255)
            arg1-arg3: V1-V3 parameters (not yet confirmed)
            key_index: Which key to configure (cmd index field)
            save: Also send Save command after
        """
        config_data = build_key_config(arg0, arg1, arg2, arg3)
        resp = self.send_single(CmdId.KEY, config_data, index=key_index)

        if save:
            self.save()

        return resp

    def set_key_arg0(self, value: int, key_index: int = 0, save: bool = True):
        """
        Setzt den Arg0-Wert für einen Key und speichert optional.

        Args:
            value (int): Neuer Wert für Arg0 (0-255)
            key_index (int): Index des Keys (Standard: 0)
            save (bool): Wenn True, wird nach dem Setzen gespeichert

        Beispiel:
            dev.set_key_arg0(42, save=True)
        """
        return self.set_key_config(arg0=value, key_index=key_index, save=save)

    def save(self) -> bytes | None:
        """
        Sendet den Save-Befehl (CMD 0x0D), um Änderungen zu speichern.

        Returns:
            Antwort-Bytes oder None.

        Beispiel:
            dev.save()
        """
        return self.send_single(CmdId.SAVE, wait_response=True)

    @staticmethod
    def _resolve_color(color: int | str) -> int:
        """Convert color to RGB565. Accepts int or '#RRGGBB' string."""
        if isinstance(color, str):
            return hex_color_to_565(color)
        return color

    def set_screen_element(
        self,
        x: int = 0,
        y: int = 0,
        width: int = 40,
        height: int = 40,
        color: int | str = 0xFFFF,
        element_type: int = 1,
        element_index: int = 0x0F,
        refresh: bool = False,
    ) -> bytes | None:
        """
        Set screen element properties via SCREEN_MAIN (CMD 0x22).

        Args:
            x: X-Position in Pixeln (0-65535).
            y: Y-Position in Pixeln (0-65535).
            width: Breite des Elements in Pixeln.
            height: Höhe des Elements in Pixeln.
            color: RGB565 int oder '#RRGGBB' String (z.B. '#FF0000' für Rot).
            element_type: Elementtyp (1 = Pure Color).
            element_index: Index des Elements/Fn (z.B. 0x0F).
            refresh: Wenn True, wird nach dem Setzen ein Display-Refresh ausgeführt.

        Returns:
            Response bytes oder None.

        Beispiel::

            dev.set_screen_element(x=120, color='#FF0000', refresh=True)
        """
        data = build_screen_element(
            x=x, y=y, width=width, height=height,
            color=self._resolve_color(color), element_type=element_type,
        )
        resp = self.send_single(
            CmdId.SCREEN_MAIN, data, index=element_index,
        )
        if refresh:
            self.refresh_display()
        return resp

    def refresh_display(self) -> bytes | None:
        """
        Sendet den DISPLAY-Refresh-Befehl (CMD 0x25).

        Returns:
            Antwort-Bytes oder None.

        Beispiel:
            dev.refresh_display()
        """
        return self.send_single(CmdId.DISPLAY)

    # ---- Convenience screen helpers ----

    def fill_screen(
        self,
        color: int | str = 0xFFFF,
        layer: int = 14,
        refresh: bool = True,
    ) -> bytes | None:
        """
        Fill the entire display with a solid color.

        Args:
            color: RGB565 int oder '#RRGGBB' String.
            layer: Screen-Element-Index (Standard: 14 = Hintergrund).
            refresh: Display nach dem Setzen aktualisieren.

        Beispiel::

            dev.fill_screen('#1295FF')
        """
        return self.set_screen_element(
            x=0, y=0, width=160, height=80,
            color=color, element_index=layer, refresh=refresh,
        )

    def draw_rect(
        self,
        x: int,
        y: int,
        width: int = 40,
        height: int = 40,
        color: int | str = 0xFFFF,
        layer: int = 15,
        refresh: bool = True,
    ) -> bytes | None:
        """
        Draw a colored rectangle on the display.

        Args:
            x: X-Position in Pixeln.
            y: Y-Position in Pixeln.
            width: Breite in Pixeln (Standard: 40).
            height: Höhe in Pixeln (Standard: 40).
            color: RGB565 int oder '#RRGGBB' String.
            layer: Screen-Element-Index (Standard: 15 = Vordergrund).
            refresh: Display nach dem Setzen aktualisieren.

        Beispiel::

            dev.draw_rect(40, 0, 40, 40, '#FF0000')
        """
        return self.set_screen_element(
            x=x, y=y, width=width, height=height,
            color=color, element_index=layer, refresh=refresh,
        )

    def clear_layer(self, layer: int = 15, refresh: bool = True) -> bytes | None:
        """
        Clear a screen layer (set element_type=0).

        Args:
            layer: Screen-Element-Index zum Löschen.
            refresh: Display nach dem Löschen aktualisieren.

        Beispiel::

            dev.clear_layer(15)
        """
        return self.set_screen_element(
            element_type=0, element_index=layer, refresh=refresh,
        )

    # ---- Input / status queries ----

    def get_fn(self) -> int:
        """
        Get current FN layer number.

        Returns:
            int: Aktuelle FN-Nummer (z.B. 0, 1, 2...).

        Beispiel::

            fn = dev.get_fn()  # -> 0
        """
        return self.get_info().fn

    def get_key_status(self, key_index: int = 0) -> bytes | None:
        """
        Query key status (CMD 0x1E).

        Returns raw response — protocol for this command is not yet
        fully decoded. Useful for experimentation.

        Args:
            key_index: Which key to query.

        Returns:
            Raw response bytes or None.
        """
        return self.send_single(CmdId.KEY_STATUS, index=key_index)

    def get_buttons(self) -> ButtonState:
        """
        Liest den aktuellen Tastenstatus aller Buttons und des Drehreglers.

        Sendet KEY_STATUS (CMD 0x1E, index=0) und dekodiert Byte [8]
        als aktiv-niedrige Bitmaske (0 = gedrückt, 1 = losgelassen).

        Returns:
            ButtonState: Objekt mit bool-Feldern für jeden Button/Knob.

        Beispiel::

            btns = dev.get_buttons()
            if btns.button1:
                print("Button 1 is pressed!")
            if btns.knob_right:
                print("Knob turned right")
        """
        resp = self.send_single(CmdId.KEY_STATUS, index=0)
        if resp and len(resp) > 8:
            return ButtonState.from_byte(resp[8])
        return ButtonState()

    def get_sys_info(self) -> SysInfo:
        """
        Sendet SYS_INFO (CMD 0x02) und versucht die Antwort zu parsen.

        Returns:
            SysInfo: Geparstes Objekt mit Display-Abmessungen, HW-ID, Uptime, VID/PID.

        Beispiel:
            si = dev.get_sys_info()
            print(si.display_width, si.display_height)
        """
        resp = self.send_single(CmdId.SYS_INFO)
        if resp and len(resp) > 8:
            payload = resp[8:]
            return parse_sys_info(payload)
        return SysInfo(raw=bytes(resp) if resp else b"")

    def get_display_size(self) -> tuple[int, int]:
        """
        Gibt die Display-Abmessungen als (width, height) Tuple zurück.

        Returns:
            Tuple[int, int]: (display_width, display_height), z.B. (160, 80).

        Beispiel:
            w, h = dev.get_display_size()
        """
        si = self.get_sys_info()
        return (si.display_width, si.display_height)

    def get_setting(self) -> DeviceSetting:
        """
        Sendet SETTING (CMD 0x03) und versucht die Antwort zu parsen.

        Returns:
            DeviceSetting: Geparstes Objekt mit Host-Auflösung.

        Beispiel:
            s = dev.get_setting()
            print(s.host_width, s.host_height)
        """
        resp = self.send_single(CmdId.SETTING)
        if resp and len(resp) > 8:
            payload = resp[8:]
            return parse_setting(payload)
        return DeviceSetting(raw=bytes(resp) if resp else b"")

    def send_heartbeat(self) -> None:
        """
        Sendet das SYS_INFO + SETTING Paar, das die offizielle GUI alle ~2s sendet.

        Dies hält das Gerät möglicherweise wach und empfangsbereit.

        Beispiel:
            dev.send_heartbeat()
        """
        # SYS_INFO: 44 bytes with observed constant values
        sys_data = bytearray(44)
        struct.pack_into("<H", sys_data, 0, 160)    # display_width
        struct.pack_into("<H", sys_data, 2, 80)     # display_height
        struct.pack_into("<H", sys_data, 4, 60)     # unknown (refresh rate?)
        struct.pack_into("<H", sys_data, 6, 902)    # hw_id
        struct.pack_into("<I", sys_data, 8, int(time.time()) & 0xFFFFFFFF)  # uptime approx
        struct.pack_into("<H", sys_data, 12, SAYO_VID)
        struct.pack_into("<H", sys_data, 14, SAYO_PID)

        # SETTING: 38 bytes with observed constant values
        set_data = bytearray(38)
        struct.pack_into("<H", set_data, 0, 1920)   # host_width
        struct.pack_into("<H", set_data, 2, 1080)   # host_height

        self.send_command([
            HidCommand(CmdId.SYS_INFO, data=bytes(sys_data)),
            HidCommand(CmdId.SETTING, data=bytes(set_data)),
        ], wait_response=False)

    def get_device_name(self) -> str:
        """
        Liest den Gerätenamen (CMD 0x01).

        Returns:
            str: Gerätename als String.

        Beispiel:
            name = dev.get_device_name()
            print(name)
        """
        resp = self.send_single(CmdId.DEVICE_NAME)
        if resp and len(resp) > 8:
            # Name is UTF-16LE or UTF-32LE after cmd header
            payload = resp[8:]
            try:
                # Try to find null terminator
                end = payload.find(b"\x00\x00")
                if end > 0:
                    payload = payload[: end + 1]
                return payload.decode("utf-16-le", errors="replace").strip("\x00")
            except Exception:
                return payload.hex()
        return ""

    def send_raw_packet(self, data: bytearray | bytes) -> int:
        """Send a completely raw packet (for advanced use / debugging)."""
        return self.send(bytearray(data))

    # ---- Discovery / factory ----

    @staticmethod
    def enumerate() -> list[SayoInterface]:
        """Find all SayoDevice HID interfaces."""
        interfaces = []
        for dev in hid.enumerate(SAYO_VID, SAYO_PID):
            interfaces.append(
                SayoInterface(
                    path=dev.get("path", b""),
                    usage_page=dev.get("usage_page", 0),
                    usage=dev.get("usage", 0),
                    interface_number=dev.get("interface_number", -1),
                    product_string=dev.get("product_string", ""),
                )
            )
        return interfaces

    @classmethod
    def open(
        cls,
        usage_page: UsagePage | None = None,
        echo: int = DEFAULT_ECHO,
    ) -> SayoDevice:
        """
        Find and open a SayoDevice.

        Args:
            usage_page: Force a specific usage page, or None to auto-detect.
                        Auto-detect prefers HIGHSPEED > NORMAL > V1.
            echo: Echo byte value.

        Raises:
            RuntimeError: If no suitable device is found.
        """
        interfaces = cls.enumerate()
        config_interfaces = [i for i in interfaces if i.is_config]

        if not config_interfaces:
            all_ups = [f"0x{i.usage_page:04X}" for i in interfaces]
            raise RuntimeError(
                f"No SayoDevice config interface found. "
                f"Found {len(interfaces)} interfaces with usage pages: "
                f"{', '.join(all_ups) or 'none'}. "
                f"Is the device connected?"
            )

        if usage_page is not None:
            # User wants a specific usage page
            matching = [i for i in config_interfaces if i.usage_page == usage_page.value]
            if not matching:
                available = [f"0x{i.usage_page:04X}" for i in config_interfaces]
                raise RuntimeError(
                    f"No interface with usage page 0x{usage_page.value:04X}. "
                    f"Available config interfaces: {', '.join(available)}"
                )
            chosen = matching[0]
        else:
            # Auto-detect: prefer HIGHSPEED > NORMAL > V1
            chosen = None
            for up in USAGE_PAGE_PRIORITY:
                for iface in config_interfaces:
                    if iface.usage_page == up.value:
                        chosen = iface
                        break
                if chosen:
                    break
            if chosen is None:
                chosen = config_interfaces[0]

        mode = chosen.mode
        if mode is None:
            raise RuntimeError(f"Unknown usage page 0x{chosen.usage_page:04X}")

        dev = cls(chosen.path, mode, echo)
        dev._open()
        return dev

    def __repr__(self) -> str:
        status = "open" if self.is_open else "closed"
        return (
            f"SayoDevice(usage_page={self._usage_page.name}, "
            f"pkt_size={self.packet_size}, status={status})"
        )

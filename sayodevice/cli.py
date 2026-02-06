"""
SayoDevice CLI - Command-line interface and interactive REPL.

Usage::

    sayodevice scan
    sayodevice info
    sayodevice set-arg0 128
    sayodevice interactive
"""

from __future__ import annotations

import argparse
import cmd
import sys
import time
import traceback

from .protocol import (
    UsagePage,
    CmdId,
    HidCommand,
    build_packet,
    build_key_config,
    calc_checksum,
    DEFAULT_ECHO,
)
from .device import SayoDevice, SayoInterface


# ============================================================
# Interactive REPL
# ============================================================

class SayoREPL(cmd.Cmd):
    """Interactive SayoDevice debugging console."""

    intro = (
        "\n"
        "╔══════════════════════════════════════════╗\n"
        "║   SayoDevice Interactive Console         ║\n"
        "║   Type 'help' for commands, 'quit' to    ║\n"
        "║   exit.                                   ║\n"
        "╚══════════════════════════════════════════╝\n"
    )
    prompt = "sayo> "

    def __init__(self, device: SayoDevice):
        super().__init__()
        self.dev = device
        print(f"Connected: {device!r}")

    # ---- Commands ----

    def do_info(self, _arg: str):
        """Query device info (CMD 0x00)."""
        try:
            info = self.dev.get_info()
            print(f"  {info}")
            if info.raw:
                print(f"  Raw ({len(info.raw)} bytes): {info.raw[:32].hex(' ')}")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_name(self, _arg: str):
        """Query device name (CMD 0x01)."""
        try:
            name = self.dev.get_device_name()
            print(f"  Device name: {name!r}")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_arg0(self, arg: str):
        """Set Arg0 value. Usage: arg0 <0-255> [--nosave]"""
        parts = arg.split()
        if not parts:
            print("  Usage: arg0 <0-255> [--nosave]")
            return
        try:
            value = int(parts[0])
            assert 0 <= value <= 255, "Value must be 0-255"
            do_save = "--nosave" not in parts
            self.dev.set_key_arg0(value, save=do_save)
            print(f"  ✅ Arg0 set to {value}" + (" (saved)" if do_save else " (not saved)"))
        except (ValueError, AssertionError) as e:
            print(f"  ERROR: {e}")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_save(self, _arg: str):
        """Send Save command (CMD 0x0D)."""
        try:
            resp = self.dev.save()
            print("  ✅ Save sent")
            if resp:
                print(f"  Response: {bytes(resp)[:32].hex(' ')}")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_send(self, arg: str):
        """Send raw command. Usage: send <cmd_id_hex> [data_hex]
        Example: send 0x00
        Example: send 0x10 01000000e803..."""
        parts = arg.split(maxsplit=1)
        if not parts:
            print("  Usage: send <cmd_id_hex> [data_hex]")
            return
        try:
            cmd_id = int(parts[0], 0)
            data = bytes.fromhex(parts[1]) if len(parts) > 1 else b""
            print(f"  Sending CMD 0x{cmd_id:02X} with {len(data)} bytes data...")
            resp = self.dev.send_single(cmd_id, data)
            print("  ✅ Sent")
            if resp:
                print(f"  Response ({len(resp)} bytes): {bytes(resp)[:64].hex(' ')}")
            else:
                print("  No response received")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_raw(self, arg: str):
        """Send raw hex packet. Usage: raw <hex_bytes>
        The packet will be padded to the correct size.
        Example: raw 2212000004000000"""
        if not arg.strip():
            print("  Usage: raw <hex_bytes>")
            return
        try:
            data = bytes.fromhex(arg.replace(" ", ""))
            packet = bytearray(self.dev.packet_size)
            packet[: len(data)] = data
            # Recalculate checksum
            checksum = calc_checksum(packet)
            packet[2] = checksum & 0xFF
            packet[3] = (checksum >> 8) & 0xFF
            print(f"  Sending {len(packet)} bytes (checksum=0x{checksum:04X})...")
            print(f"  First 32 bytes: {packet[:32].hex(' ')}")
            result = self.dev.send_raw_packet(packet)
            print(f"  ✅ Write result: {result}")
            time.sleep(0.05)
            resp = self.dev.receive(timeout_ms=200)
            if resp:
                print(f"  Response ({len(resp)} bytes): {bytes(resp)[:64].hex(' ')}")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_read(self, arg: str):
        """Read from device. Usage: read [timeout_ms]"""
        timeout = int(arg) if arg.strip() else 500
        try:
            resp = self.dev.receive(timeout_ms=timeout)
            if resp:
                print(f"  Got {len(resp)} bytes: {bytes(resp)[:64].hex(' ')}")
            else:
                print("  No data received")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_sweep(self, arg: str):
        """Sweep Arg0 from 0 to 255 with delay. Usage: sweep [delay_ms] [step]
        Example: sweep 100 5"""
        parts = arg.split()
        delay = int(parts[0]) / 1000.0 if len(parts) > 0 and parts[0] else 0.1
        step = int(parts[1]) if len(parts) > 1 else 1
        print(f"  Sweeping Arg0 0..255 (step={step}, delay={delay*1000:.0f}ms)")
        print("  Press Ctrl+C to stop")
        try:
            for v in range(0, 256, step):
                self.dev.set_key_arg0(v, save=False)
                sys.stdout.write(f"\r  Arg0 = {v:3d}")
                sys.stdout.flush()
                time.sleep(delay)
            print("\n  ✅ Sweep complete")
        except KeyboardInterrupt:
            print("\n  Stopped")

    def do_status(self, _arg: str):
        """Show current connection status."""
        print(f"  Device: {self.dev!r}")
        print(f"  Usage Page: {self.dev.usage_page.name} (0x{self.dev.usage_page.value:04X})")
        print(f"  Packet Size: {self.dev.packet_size}")
        print(f"  Report ID: 0x{self.dev.report_id:02X}")

    def do_interfaces(self, _arg: str):
        """List all HID interfaces."""
        interfaces = SayoDevice.enumerate()
        for i, iface in enumerate(interfaces):
            marker = " ◄" if iface.is_config else ""
            print(f"  [{i}] {iface}{marker}")

    def do_quit(self, _arg: str):
        """Exit the interactive console."""
        print("  Goodbye!")
        return True

    def do_exit(self, arg: str):
        """Exit the interactive console."""
        return self.do_quit(arg)

    do_q = do_quit
    do_EOF = do_quit

    def default(self, line: str):
        """Handle unknown commands."""
        print(f"  Unknown command: {line!r}. Type 'help' for available commands.")


# ============================================================
# CLI entry point
# ============================================================

def print_interfaces(interfaces: list[SayoInterface]):
    """Pretty-print discovered interfaces."""
    print(f"\nFound {len(interfaces)} HID interface(s):")
    print("-" * 60)
    for i, iface in enumerate(interfaces):
        marker = " ◄ CONFIG" if iface.is_config else ""
        mode = f" [{iface.mode.name}]" if iface.mode else ""
        print(f"  [{i}] {iface}{marker}")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="sayodevice",
        description="SayoDevice O3C USB HID controller",
    )
    parser.add_argument(
        "--interface",
        choices=["highspeed", "normal", "v1", "auto"],
        default="auto",
        help="Which HID interface to use (default: auto-detect, prefers highspeed)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output",
    )

    sub = parser.add_subparsers(dest="command")

    # scan
    sub.add_parser("scan", help="Scan for SayoDevice interfaces")

    # info
    sub.add_parser("info", help="Query device info")

    # name
    sub.add_parser("name", help="Query device name")

    # set-arg0
    p_arg0 = sub.add_parser("set-arg0", help="Set Arg0 (V0 parameter)")
    p_arg0.add_argument("value", type=int, help="Value 0-255")
    p_arg0.add_argument("--nosave", action="store_true", help="Don't save after setting")

    # save
    sub.add_parser("save", help="Send Save command")

    # send-raw
    p_raw = sub.add_parser("send-raw", help="Send raw command by ID")
    p_raw.add_argument("cmd_id", help="Command ID (hex or decimal)")
    p_raw.add_argument("data", nargs="?", default="", help="Data as hex string")

    # interactive
    sub.add_parser("interactive", aliases=["i", "repl"], help="Interactive debugging console")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return

    # ---- scan (no device needed) ----
    if args.command == "scan":
        interfaces = SayoDevice.enumerate()
        print_interfaces(interfaces)
        config = [i for i in interfaces if i.is_config]
        if config:
            print(f"\n✅ {len(config)} config interface(s) available")
            for i in config:
                print(f"   {i.mode.name if i.mode else '?'}: {i.path.decode(errors='replace')[:80]}")
        else:
            print("\n❌ No config interfaces found. Is the device connected?")
        return

    # ---- All other commands need a device ----
    usage_page_map = {
        "highspeed": UsagePage.HIGHSPEED,
        "normal": UsagePage.NORMAL,
        "v1": UsagePage.V1,
        "auto": None,
    }
    up = usage_page_map[args.interface]

    try:
        dev = SayoDevice.open(usage_page=up)
    except RuntimeError as e:
        print(f"❌ {e}")
        return

    if args.verbose:
        print(f"✅ Connected: {dev!r}")

    try:
        if args.command == "info":
            info = dev.get_info()
            print(f"Device info: {info}")
            if args.verbose and info.raw:
                print(f"Raw: {info.raw[:64].hex(' ')}")

        elif args.command == "name":
            name = dev.get_device_name()
            print(f"Device name: {name!r}")

        elif args.command == "set-arg0":
            if not 0 <= args.value <= 255:
                print("❌ Value must be 0-255")
                return
            do_save = not args.nosave
            dev.set_key_arg0(args.value, save=do_save)
            print(f"✅ Arg0 = {args.value}" + (" (saved)" if do_save else ""))

        elif args.command == "save":
            dev.save()
            print("✅ Saved")

        elif args.command == "send-raw":
            cmd_id = int(args.cmd_id, 0)
            data = bytes.fromhex(args.data) if args.data else b""
            resp = dev.send_single(cmd_id, data)
            print(f"✅ Sent CMD 0x{cmd_id:02X}")
            if resp:
                print(f"Response: {bytes(resp)[:64].hex(' ')}")

        elif args.command in ("interactive", "i", "repl"):
            repl = SayoREPL(dev)
            try:
                repl.cmdloop()
            except KeyboardInterrupt:
                print("\nExiting...")
    except Exception as e:
        print(f"❌ Error: {e}")
        if args.verbose:
            traceback.print_exc()
    finally:
        dev.close()


if __name__ == "__main__":
    main()

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
import runpy
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
    hex_color_to_565,
    rgb565_to_rgb,
)
from .device import SayoDevice, SayoInterface
from .analyzer import analyze_pcapng, decode_raw_response
from .setup import (
    DeviceSetup,
    ScreenElement,
    KeyConfig,
    save_setup,
    load_setup,
    list_setups,
    delete_setup,
)


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

    def do_screen_pos(self, arg: str):
        """Set screen element position. Usage: screen_pos <x> <y> [--index 0x0F] [--refresh]
        Example: screen_pos 120 40
        Example: screen_pos 50 50 --index 0x0F --refresh"""
        parts = arg.split()
        if len(parts) < 2:
            print("  Usage: screen_pos <x> <y> [--index 0x0F] [--refresh]")
            return
        try:
            x = int(parts[0])
            y = int(parts[1])
            element_index = 0x0F
            do_refresh = "--refresh" in parts
            if "--index" in parts:
                idx = parts.index("--index")
                if idx + 1 < len(parts):
                    element_index = int(parts[idx + 1], 0)
            resp = self.dev.set_screen_element(x=x, y=y, element_index=element_index)
            print(f"  Set X={x}, Y={y} (element_index=0x{element_index:02X})")
            if resp:
                print(f"  Response: {bytes(resp)[:32].hex(' ')}")
            if do_refresh:
                self.dev.refresh_display()
                print("  Display refreshed")
        except (ValueError, AssertionError) as e:
            print(f"  ERROR: {e}")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_display_refresh(self, _arg: str):
        """Send DISPLAY refresh command (CMD 0x25)."""
        try:
            resp = self.dev.refresh_display()
            print("  Display refresh sent")
            if resp:
                print(f"  Response: {bytes(resp)[:32].hex(' ')}")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_sys_info(self, _arg: str):
        """Query SYS_INFO from device (CMD 0x02)."""
        try:
            si = self.dev.get_sys_info()
            print(f"  {si}")
            if si.raw:
                print(f"  Raw ({len(si.raw)} bytes): {si.raw[:44].hex(' ')}")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_setting(self, _arg: str):
        """Query SETTING from device (CMD 0x03)."""
        try:
            s = self.dev.get_setting()
            print(f"  {s}")
            if s.raw:
                print(f"  Raw ({len(s.raw)} bytes): {s.raw[:38].hex(' ')}")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_display_size(self, _arg: str):
        """Show display dimensions from SYS_INFO."""
        try:
            w, h = self.dev.get_display_size()
            print(f"  Display: {w}x{h} pixels")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_color(self, arg: str):
        """Set screen element color. Usage: color <#RRGGBB | 0xNNNN>
        Example: color #FF0000
        Example: color 0xF800"""
        if not arg.strip():
            print("  Usage: color <#RRGGBB | 0xNNNN>")
            return
        try:
            val = arg.strip()
            if val.startswith("#"):
                color = hex_color_to_565(val)
                r, g, b = rgb565_to_rgb(color)
                print(f"  #{val.lstrip('#')} -> RGB565 0x{color:04X} (R={r} G={g} B={b})")
            else:
                color = int(val, 0)
                r, g, b = rgb565_to_rgb(color)
                print(f"  0x{color:04X} = R={r} G={g} B={b}")
            self.dev.set_screen_element(color=color)
            print(f"  Color set to 0x{color:04X}")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_heartbeat(self, _arg: str):
        """Send SYS_INFO + SETTING heartbeat pair."""
        try:
            self.dev.send_heartbeat()
            print("  Heartbeat sent (SYS_INFO + SETTING)")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_capture(self, arg: str):
        """Probe all known commands and decode responses live.
        Usage: capture [cmd_ids...]
        Example: capture              (probes INFO, SYS_INFO, SETTING, DEVICE_NAME)
        Example: capture 0x02 0x03    (probes SYS_INFO and SETTING only)"""
        parts = arg.split()
        if parts:
            cmd_ids = [int(p, 0) for p in parts]
        else:
            cmd_ids = [
                CmdId.INFO,
                CmdId.SYS_INFO,
                CmdId.SETTING,
                CmdId.DEVICE_NAME,
            ]
        try:
            print(f"  {'=' * 50}")
            print(f"  Live Capture — probing {len(cmd_ids)} command(s)")
            print(f"  {'=' * 50}")
            for cid in cmd_ids:
                try:
                    name = CmdId(cid).name
                except ValueError:
                    name = f"0x{cid:02X}"
                print(f"\n  ── {name} (0x{cid:02X}) ──")
                resp = self.dev.send_single(cid)
                if resp:
                    print(decode_raw_response(resp))
                else:
                    print("  (no response)")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_sniff(self, arg: str):
        """Listen for incoming packets and decode them live.
        Usage: sniff [seconds]
        Example: sniff 10
        Press Ctrl+C to stop."""
        duration = float(arg) if arg.strip() else 10.0
        print(f"  Sniffing for {duration:.0f}s (Ctrl+C to stop)...")
        try:
            start = time.time()
            count = 0
            while time.time() - start < duration:
                resp = self.dev.receive(timeout_ms=500)
                if resp:
                    count += 1
                    elapsed = time.time() - start
                    print(f"\n  ── Packet #{count} at {elapsed:.2f}s ──")
                    print(decode_raw_response(resp))
            print(f"\n  Done. {count} packet(s) received in {duration:.0f}s")
        except KeyboardInterrupt:
            print(f"\n  Stopped. {count} packet(s) received")
        except Exception as e:
            print(f"  ERROR: {e}")

    def do_probe(self, arg: str):
        """Probe screen element fields interactively. Usage: probe [field] [start] [end] [step]
        Fields: x, y, width, height, color, type
        Example: probe x 0 160 20
        Example: probe y 0 80 10
        Example: probe color 0x0000 0xFFFF 0x1000"""
        parts = arg.split()
        if not parts:
            print("  Usage: probe <field> [start] [end] [step]")
            print("  Fields: x, y, width, height, color, type")
            print("  Example: probe x 0 160 20")
            return

        field_name = parts[0].lower()
        field_map = {
            "x": "x", "y": "y", "width": "width", "height": "height",
            "color": "color", "type": "element_type",
        }
        if field_name not in field_map:
            print(f"  Unknown field: {field_name}")
            print(f"  Available: {', '.join(field_map.keys())}")
            return

        param = field_map[field_name]
        start = int(parts[1], 0) if len(parts) > 1 else 0
        end = int(parts[2], 0) if len(parts) > 2 else 160
        step = int(parts[3], 0) if len(parts) > 3 else 20

        print(f"  Probing {field_name} from {start} to {end} (step={step})")
        print("  Watch the device screen. Press Ctrl+C to stop.")
        try:
            for val in range(start, end + 1, step):
                kwargs = {param: val}
                self.dev.set_screen_element(**kwargs)
                sys.stdout.write(f"\r  {field_name} = {val:<6d}")
                sys.stdout.flush()
                time.sleep(0.3)
            print(f"\n  Probe complete. {field_name} swept {start} -> {end}")
        except KeyboardInterrupt:
            print("\n  Stopped")
        except Exception as e:
            print(f"\n  ERROR: {e}")

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

    def do_button_probe(self, arg: str):
        """Probe for button press detection. Press buttons on device while running.
        Usage: button_probe [seconds] [max_key_index]
        Example: button_probe 30
        Example: button_probe 60 8
        Press Ctrl+C to stop early."""
        parts = arg.split()
        duration = float(parts[0]) if parts else 30.0
        max_key = int(parts[1]) if len(parts) > 1 else 4

        print(f"  === Button Probe ({duration:.0f}s) ===")
        print(f"  Polling KEY_STATUS (0x1E) for key indices 0..{max_key-1}")
        print(f"  + INFO (0x00) for FN/status fields")
        print(f"  + receive() for unsolicited packets")
        print(f"  Press buttons on the device NOW! Ctrl+C to stop.\n")

        # Collect baselines
        baselines = {}
        info_baseline = None

        print("  Capturing baselines (don't press anything)...")
        time.sleep(0.5)

        # Drain any pending data
        for _ in range(5):
            self.dev.receive(timeout_ms=50)

        # Baseline INFO
        try:
            info_resp = self.dev.send_single(CmdId.INFO)
            if info_resp:
                info_baseline = bytes(info_resp)
                print(f"  INFO baseline: {info_baseline[:32].hex(' ')}")
        except Exception as e:
            print(f"  INFO baseline failed: {e}")

        # Baseline KEY_STATUS for each index
        for ki in range(max_key):
            try:
                resp = self.dev.send_single(CmdId.KEY_STATUS, index=ki)
                if resp:
                    baselines[ki] = bytes(resp)
                    # Show first 32 bytes of each
                    print(f"  KEY_STATUS[{ki}] baseline: {baselines[ki][:32].hex(' ')}")
                else:
                    print(f"  KEY_STATUS[{ki}] baseline: (no response)")
            except Exception as e:
                print(f"  KEY_STATUS[{ki}] baseline failed: {e}")

        print(f"\n  --- Now press buttons! Monitoring for {duration:.0f}s ---\n")

        # Noisy bytes to ignore in diffs:
        # [2-3] = packet checksum (always changes when any field changes)
        # [18]  = cpu_s uptime seconds counter
        # [19]  = cpu_ms uptime sub-second counter
        info_ignore = {2, 3, 18, 19}
        # KEY_STATUS also has checksum at [2-3]
        key_ignore = {2, 3}

        start = time.time()
        event_count = 0
        try:
            while time.time() - start < duration:
                elapsed = time.time() - start

                # 1. Check for unsolicited packets first
                try:
                    unsolicited = self.dev.receive(timeout_ms=20)
                    if unsolicited:
                        event_count += 1
                        print(f"  [{elapsed:6.2f}s] UNSOLICITED ({len(unsolicited)}B): "
                              f"{bytes(unsolicited)[:64].hex(' ')}")
                except Exception:
                    pass

                # 2. Poll INFO and diff (ignoring checksum + uptime noise)
                try:
                    info_resp = self.dev.send_single(CmdId.INFO)
                    if info_resp and info_baseline:
                        info_bytes = bytes(info_resp)
                        diffs = []
                        for i in range(min(len(info_bytes), len(info_baseline))):
                            if i not in info_ignore and info_bytes[i] != info_baseline[i]:
                                diffs.append(f"[{i}]: 0x{info_baseline[i]:02X}->0x{info_bytes[i]:02X}")
                        if diffs:
                            event_count += 1
                            print(f"  [{elapsed:6.2f}s] INFO CHANGED: {', '.join(diffs[:10])}")
                        info_baseline = info_bytes
                except Exception:
                    pass

                # 3. Poll KEY_STATUS for each index and diff (ignoring checksum)
                for ki in range(max_key):
                    try:
                        resp = self.dev.send_single(CmdId.KEY_STATUS, index=ki)
                        if resp:
                            resp_bytes = bytes(resp)
                            bl = baselines.get(ki)
                            if bl:
                                diffs = []
                                for i in range(min(len(resp_bytes), len(bl))):
                                    if i not in key_ignore and resp_bytes[i] != bl[i]:
                                        diffs.append(f"[{i}]: 0x{bl[i]:02X}->0x{resp_bytes[i]:02X}")
                                if diffs:
                                    event_count += 1
                                    print(f"  [{elapsed:6.2f}s] KEY_STATUS[{ki}] CHANGED: {', '.join(diffs[:10])}")
                                    print(f"    full: {resp_bytes[:48].hex(' ')}")
                            baselines[ki] = resp_bytes
                    except Exception:
                        pass

                time.sleep(0.02)  # ~50Hz poll rate

        except KeyboardInterrupt:
            pass

        elapsed = time.time() - start
        print(f"\n  === Done. {event_count} change(s) detected in {elapsed:.1f}s ===")
        if event_count == 0:
            print("  No changes detected. Buttons may report on a different HID interface.")
            print("  Try: interfaces  (to see all HID interfaces)")
            print("  The standard keyboard/consumer interfaces may carry button events.")

    def do_setup(self, arg: str):
        """Manage named device setups. Usage: setup <list|show|apply> [name]
        Examples:
            setup list
            setup show seq-gate
            setup apply seq-gate"""
        parts = arg.split(maxsplit=1)
        if not parts:
            print("  Usage: setup <list|show|apply> [name]")
            return
        action = parts[0].lower()
        name = parts[1] if len(parts) > 1 else None

        if action == "list":
            names = list_setups()
            if not names:
                print("  No saved setups. Use the Python API to create one.")
            else:
                print(f"  {len(names)} setup(s):")
                for n in names:
                    try:
                        s = load_setup(n)
                        desc = f" — {s.description}" if s.description else ""
                        elems = len(s.screen_elements)
                        keys = len(s.key_configs)
                        print(f"    {n}{desc}  ({elems} elements, {keys} keys)")
                    except Exception:
                        print(f"    {n}  (error loading)")

        elif action == "show":
            if not name:
                print("  Usage: setup show <name>")
                return
            try:
                s = load_setup(name)
                print(f"  Name: {s.name}")
                if s.description:
                    print(f"  Description: {s.description}")
                print(f"  Save to flash: {s.save_to_flash}")
                if s.screen_elements:
                    print(f"  Screen elements ({len(s.screen_elements)}):")
                    for i, e in enumerate(s.screen_elements):
                        print(f"    [{i}] idx=0x{e.element_index:02X} "
                              f"{e.width}x{e.height} @ ({e.x},{e.y}) "
                              f"color={e.color} type={e.element_type}")
                if s.key_configs:
                    print(f"  Key configs ({len(s.key_configs)}):")
                    for i, k in enumerate(s.key_configs):
                        args = [f"arg0={k.arg0}"]
                        if k.arg1 is not None: args.append(f"arg1={k.arg1}")
                        if k.arg2 is not None: args.append(f"arg2={k.arg2}")
                        if k.arg3 is not None: args.append(f"arg3={k.arg3}")
                        print(f"    [{i}] key={k.key_index} {', '.join(args)}")
            except FileNotFoundError:
                print(f"  Setup '{name}' not found")
            except Exception as e:
                print(f"  ERROR: {e}")

        elif action == "apply":
            if not name:
                print("  Usage: setup apply <name>")
                return
            try:
                s = load_setup(name)
                s.apply(self.dev)
                elems = len(s.screen_elements)
                keys = len(s.key_configs)
                print(f"  ✅ Applied '{name}' ({elems} elements, {keys} keys)")
            except FileNotFoundError:
                print(f"  Setup '{name}' not found")
            except Exception as e:
                print(f"  ERROR: {e}")

        else:
            print(f"  Unknown action: {action}")
            print("  Available: list, show, apply")

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
# Script runner
# ============================================================

def run_script(script_path: str, script_args: list[str] | None = None) -> None:
    """Run a Python script with sayodevice available, as if it were __main__."""
    import os
    if not os.path.isfile(script_path):
        print(f"File not found: {script_path}")
        sys.exit(1)
    # Set sys.argv so the script sees its own args
    old_argv = sys.argv
    sys.argv = [script_path] + (script_args or [])
    try:
        runpy.run_path(script_path, run_name="__main__")
    except SystemExit:
        pass  # Script called sys.exit(), that's fine
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception:
        traceback.print_exc()
    finally:
        sys.argv = old_argv


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

    # set-pos
    p_pos = sub.add_parser("set-pos", aliases=["set-x"], help="Set screen element X/Y position")
    p_pos.add_argument("x", type=int, help="X-position in pixels")
    p_pos.add_argument("--y", type=int, default=0, help="Y-position in pixels (default: 0)")
    p_pos.add_argument("--index", type=lambda v: int(v, 0), default=0x0F,
                        help="Element index (default: 0x0F)")
    p_pos.add_argument("--refresh", action="store_true",
                        help="Send display refresh after setting")

    # sys-info
    sub.add_parser("sys-info", help="Query SYS_INFO from device")

    # display-size
    sub.add_parser("display-size", help="Show display dimensions")

    # color
    p_color = sub.add_parser("color", help="Set screen element color")
    p_color.add_argument("value", help="Color as #RRGGBB or 0xNNNN")
    p_color.add_argument("--index", type=lambda v: int(v, 0), default=0x0F,
                          help="Element index (default: 0x0F)")

    # heartbeat
    sub.add_parser("heartbeat", help="Send SYS_INFO + SETTING heartbeat")

    # capture
    p_capture = sub.add_parser("capture", help="Probe device and decode all responses live")
    p_capture.add_argument("cmd_ids", nargs="*", default=[],
                            help="Command IDs to probe (hex or decimal, default: all known)")

    # analyze
    p_analyze = sub.add_parser("analyze", help="Analyze a pcapng capture file")
    p_analyze.add_argument("file", help="Path to .pcapng file")
    p_analyze.add_argument("--device", type=int, default=None,
                           help="USB device address to filter")

    # save
    sub.add_parser("save", help="Send Save command")

    # send-raw
    p_raw = sub.add_parser("send-raw", help="Send raw command by ID")
    p_raw.add_argument("cmd_id", help="Command ID (hex or decimal)")
    p_raw.add_argument("data", nargs="?", default="", help="Data as hex string")

    # run
    p_run = sub.add_parser("run", help="Run a Python script")
    p_run.add_argument("script", help="Path to Python script")
    p_run.add_argument("script_args", nargs="*", help="Arguments for the script")

    # interactive
    sub.add_parser("interactive", aliases=["i", "repl"], help="Interactive debugging console")

    # midi
    p_midi = sub.add_parser("midi", help="MIDI tools (requires mido + python-rtmidi)")
    midi_sub = p_midi.add_subparsers(dest="midi_action")
    midi_sub.add_parser("ports", help="List available MIDI input/output ports")
    p_midi_bridge = midi_sub.add_parser("bridge", help="Bridge device buttons to MIDI")
    p_midi_bridge.add_argument("--output", default="", help="MIDI output port name")
    p_midi_bridge.add_argument("--input", default="", help="MIDI input port name")
    p_midi_bridge.add_argument("--through", action="store_true", help="Enable MIDI through")
    p_midi_bridge.add_argument("--note", action="append", default=[],
                                help="Map button to note, e.g. button1:60 or button2:C4")
    p_midi_bridge.add_argument("--cc", action="append", default=[],
                                help="Map button to CC, e.g. button3:1:127")
    p_midi_bridge.add_argument("--knob-cc", type=int, default=1,
                                help="CC number for knob rotation (default: 1)")

    # setup
    p_setup = sub.add_parser("setup", help="Manage named device setups")
    setup_sub = p_setup.add_subparsers(dest="setup_action")
    setup_sub.add_parser("list", help="List saved setups")
    p_setup_show = setup_sub.add_parser("show", help="Show setup details")
    p_setup_show.add_argument("name", help="Setup name")
    p_setup_apply = setup_sub.add_parser("apply", help="Apply setup to device")
    p_setup_apply.add_argument("name", help="Setup name")
    p_setup_delete = setup_sub.add_parser("delete", help="Delete a saved setup")
    p_setup_delete.add_argument("name", help="Setup name")
    p_setup_save = setup_sub.add_parser("save", help="Import setup from JSON file")
    p_setup_save.add_argument("file", help="Path to JSON file")

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

    # ---- run (no device needed) ----
    if args.command == "run":
        run_script(args.script, args.script_args)
        return

    # ---- midi ports (no device needed for 'ports') ----
    if args.command == "midi":
        action = args.midi_action
        if not action:
            p_midi.print_help()
            return
        if action == "ports":
            try:
                from .midi import list_midi_ports
                ports = list_midi_ports()
                print("MIDI Output ports:")
                for p in ports['outputs']:
                    print(f"  {p}")
                if not ports['outputs']:
                    print("  (none)")
                print("\nMIDI Input ports:")
                for p in ports['inputs']:
                    print(f"  {p}")
                if not ports['inputs']:
                    print("  (none)")
            except ImportError:
                print("MIDI support not installed. Install with: pip install sayodevice[midi]")
            return
        if action == "bridge":
            # Needs a device — fall through
            pass
        else:
            p_midi.print_help()
            return

    # ---- analyze (no device needed) ----
    if args.command == "analyze":
        report = analyze_pcapng(args.file, device=args.device)
        print(report)
        return

    # ---- setup (mostly no device needed) ----
    if args.command == "setup":
        action = args.setup_action
        if not action:
            p_setup.print_help()
            return

        if action == "list":
            names = list_setups()
            if not names:
                print("No saved setups.")
            else:
                print(f"{len(names)} setup(s):")
                for n in names:
                    try:
                        s = load_setup(n)
                        desc = f" — {s.description}" if s.description else ""
                        print(f"  {n}{desc}")
                    except Exception:
                        print(f"  {n}  (error loading)")
            return

        if action == "show":
            try:
                s = load_setup(args.name)
                import json
                print(json.dumps(s.to_dict(), indent=2))
            except FileNotFoundError:
                print(f"Setup '{args.name}' not found")
            return

        if action == "delete":
            if delete_setup(args.name):
                print(f"Deleted '{args.name}'")
            else:
                print(f"Setup '{args.name}' not found")
            return

        if action == "save":
            import json
            try:
                with open(args.file) as f:
                    data = json.load(f)
                setup = DeviceSetup.from_dict(data)
                path = save_setup(setup)
                print(f"Saved '{setup.name}' -> {path}")
            except FileNotFoundError:
                print(f"File not found: {args.file}")
            except Exception as e:
                print(f"Error: {e}")
            return

        if action == "apply":
            # This one needs a device — fall through to device opening below
            pass
        else:
            p_setup.print_help()
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

        elif args.command in ("set-pos", "set-x"):
            dev.set_screen_element(x=args.x, y=args.y, element_index=args.index)
            print(f"✅ X={args.x}, Y={args.y} (element_index=0x{args.index:02X})")
            if args.refresh:
                dev.refresh_display()
                print("✅ Display refreshed")

        elif args.command == "sys-info":
            si = dev.get_sys_info()
            print(f"SYS_INFO: {si}")
            if args.verbose and si.raw:
                print(f"Raw: {si.raw[:44].hex(' ')}")

        elif args.command == "display-size":
            w, h = dev.get_display_size()
            print(f"Display: {w}x{h} pixels")

        elif args.command == "color":
            val = args.value
            if val.startswith("#"):
                color = hex_color_to_565(val)
                r, g, b = rgb565_to_rgb(color)
                print(f"#{val.lstrip('#')} -> RGB565 0x{color:04X} (R={r} G={g} B={b})")
            else:
                color = int(val, 0)
                r, g, b = rgb565_to_rgb(color)
                print(f"0x{color:04X} = R={r} G={g} B={b}")
            dev.set_screen_element(color=color, element_index=args.index)
            print(f"✅ Color set to 0x{color:04X}")

        elif args.command == "heartbeat":
            dev.send_heartbeat()
            print("✅ Heartbeat sent (SYS_INFO + SETTING)")

        elif args.command == "capture":
            if args.cmd_ids:
                cmd_ids = [int(c, 0) for c in args.cmd_ids]
            else:
                cmd_ids = [CmdId.INFO, CmdId.SYS_INFO, CmdId.SETTING, CmdId.DEVICE_NAME]
            print(f"{'=' * 50}")
            print(f"Live Capture — probing {len(cmd_ids)} command(s)")
            print(f"{'=' * 50}")
            for cid in cmd_ids:
                try:
                    name = CmdId(cid).name
                except ValueError:
                    name = f"0x{cid:02X}"
                print(f"\n── {name} (0x{cid:02X}) ──")
                resp = dev.send_single(cid)
                if resp:
                    print(decode_raw_response(resp))
                else:
                    print("(no response)")

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

        elif args.command == "setup" and args.setup_action == "apply":
            try:
                s = load_setup(args.name)
                s.apply(dev)
                elems = len(s.screen_elements)
                keys = len(s.key_configs)
                print(f"✅ Applied '{args.name}' ({elems} elements, {keys} keys)")
            except FileNotFoundError:
                print(f"❌ Setup '{args.name}' not found")

        elif args.command == "midi" and args.midi_action == "bridge":
            try:
                from .midi import MidiBridge
                from .listener import DeviceListener
                listener = DeviceListener(dev, poll_interval_ms=20)
                bridge = MidiBridge(
                    listener=listener,
                    output_port=args.output,
                    input_port=args.input if args.input else None,
                )

                # Parse note mappings: button1:60 or button2:C4
                _note_names = {
                    'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11,
                }
                for mapping in args.note:
                    parts = mapping.split(':')
                    if len(parts) >= 2:
                        btn = parts[0]
                        note_str = parts[1]
                        try:
                            note_val = int(note_str)
                        except ValueError:
                            # Parse note name like C4, D#5
                            name = note_str[0].upper()
                            sharp = '#' in note_str or 's' in note_str.lower()
                            octave = int(note_str[-1])
                            note_val = _note_names.get(name, 0) + (1 if sharp else 0) + (octave + 1) * 12
                        vel = int(parts[2]) if len(parts) > 2 else 127
                        bridge.map_button(btn, note=note_val, velocity=vel)
                        print(f"  Map {btn} -> note {note_val} (vel={vel})")

                # Parse CC mappings: button3:1:127
                for mapping in args.cc:
                    parts = mapping.split(':')
                    if len(parts) >= 2:
                        btn = parts[0]
                        cc = int(parts[1])
                        val = int(parts[2]) if len(parts) > 2 else 127
                        bridge.map_button_cc(btn, cc=cc, value=val)
                        print(f"  Map {btn} -> CC {cc} (val={val})")

                # Knob mapping
                bridge.map_knob(cc=args.knob_cc)
                print(f"  Knob -> CC {args.knob_cc}")

                bridge.through_enabled = args.through
                if args.through:
                    print("  MIDI through enabled")

                bridge.start()
                print("\n  MIDI bridge running. Press Ctrl+C to stop.\n")
                try:
                    while True:
                        time.sleep(0.1)
                except KeyboardInterrupt:
                    print("\n  Stopping...")
                finally:
                    bridge.stop()
                    listener.stop()
            except ImportError:
                print("MIDI support not installed. Install with: pip install sayodevice[midi]")

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

"""Claude Code CLI integration — AI-powered protocol analysis."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .snapshots import Discovery


SYSTEM_PROMPT = (
    "You are analyzing USB HID protocol diffs for a SAYO Device O3C keyboard "
    "(VID=0x8089 PID=0x0009). The device uses 1024-byte HID packets with "
    "report_id=0x22, little-endian byte order, and a 16-bit checksum. "
    "Known commands: INFO(0x00), SYS_INFO(0x02), SETTING(0x03), KEY(0x10), "
    "SCREEN_MAIN(0x22), DISPLAY(0x25). "
    "The user captures before/after snapshots to reverse-engineer the protocol. "
    "Analyze byte-level changes, infer field types and names, and generate "
    "a concrete implementation plan for adding new features to the sayodevice "
    "Python library (protocol.py for constants, device.py for high-level methods)."
)


def is_claude_available() -> bool:
    """Check if the claude CLI is installed and accessible."""
    return shutil.which("claude") is not None


def ask_claude(prompt: str, system_prompt: str = "", model: str = "sonnet") -> str:
    """Send a one-shot prompt to Claude via the claude CLI subprocess."""
    cmd = ["claude", "-p", "--model", model, "--no-session-persistence"]
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])
    cmd.append(prompt)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI error: {result.stderr[:200]}")
    return result.stdout.strip()


def format_discovery_for_claude(discovery: Discovery) -> str:
    """Format a discovery as a human-readable protocol diff report."""
    from .snapshots import _cmd_name, _KNOWN_FIELDS, _read_field

    lines = ["## Protocol Diff Report\n"]
    lines.append(f'User action: "{discovery.description}"\n')

    # Group changes by command
    cmd_changes: dict[int, list] = {}
    for fc in discovery.changed_fields:
        cmd_changes.setdefault(fc.cmd_id, []).append(fc)

    # For each command with changes, show before/after hex
    for cmd_id, changes in sorted(cmd_changes.items()):
        cmd_name = _cmd_name(cmd_id)
        lines.append(f"### {cmd_name} (0x{cmd_id:02X})")

        raw_before = discovery.before.responses.get(cmd_id, b"")
        raw_after = discovery.after.responses.get(cmd_id, b"")
        pay_before = raw_before[8:] if len(raw_before) > 8 else raw_before
        pay_after = raw_after[8:] if len(raw_after) > 8 else raw_after

        # Show hex dump (first 64 bytes)
        if pay_before:
            lines.append(f"Before: {pay_before[:64].hex(' ')}")
        if pay_after:
            lines.append(f"After:  {pay_after[:64].hex(' ')}")

        # Show changed bytes
        lines.append("\nChanged bytes:")
        for fc in changes:
            label = fc.field_label or f"unknown"
            if fc.size == 1:
                lines.append(
                    f"  [{fc.offset}]: 0x{fc.old_value:02X} -> 0x{fc.new_value:02X} "
                    f"({fc.old_value} -> {fc.new_value}, uint8) [{label}]"
                )
            elif fc.size == 2:
                lines.append(
                    f"  [{fc.offset}-{fc.offset+1}]: 0x{fc.old_value:04X} -> 0x{fc.new_value:04X} "
                    f"({fc.old_value} -> {fc.new_value}, uint16_le) [{label}]"
                )
            else:
                lines.append(
                    f"  [{fc.offset}-{fc.offset+fc.size-1}]: 0x{fc.old_value:X} -> 0x{fc.new_value:X} "
                    f"(size={fc.size}) [{label}]"
                )

        # Show all known fields for context
        known = _KNOWN_FIELDS.get(cmd_id, [])
        if known and pay_after:
            lines.append("\nKnown fields in this command:")
            for offset, size, ftype, flabel in known:
                val = _read_field(pay_after, offset, size)
                # Mark if this field changed
                changed_mark = ""
                for fc in changes:
                    if fc.offset == offset:
                        old = _read_field(pay_before, offset, size)
                        changed_mark = f" <- CHANGED (was {old})"
                        break
                lines.append(f"  [{offset}-{offset+size-1}] {flabel} = {val} ({ftype}){changed_mark}")

        lines.append("")

    # Note commands with no changes
    all_cmds = set(discovery.before.responses.keys()) | set(discovery.after.responses.keys())
    unchanged = all_cmds - set(cmd_changes.keys())
    if unchanged:
        names = ", ".join(_cmd_name(c) for c in sorted(unchanged))
        lines.append(f"### No changes detected in: {names}")

    return "\n".join(lines)


def analyze_diff(discovery: Discovery) -> str:
    """Ask Claude to analyze a protocol diff and suggest implementation."""
    context = format_discovery_for_claude(discovery)
    prompt = (
        f"{context}\n\n"
        "Based on this diff, please:\n"
        "1. Confirm or infer the field type and purpose\n"
        "2. Suggest a descriptive field name if unknown\n"
        "3. Provide a concrete implementation plan: what to add to protocol.py "
        "(constants, _KNOWN_FIELDS entry) and device.py (high-level method)\n"
        "4. Include example code snippets\n"
    )
    return ask_claude(prompt, system_prompt=SYSTEM_PROMPT)

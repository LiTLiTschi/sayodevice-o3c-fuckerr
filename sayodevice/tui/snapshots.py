"""Snapshot capture, diff, and persistence logic."""

from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sayodevice.protocol import CmdId

# Known field definitions reused from analyzer
_KNOWN_FIELDS: dict[int, list[tuple[int, int, str, str]]] = {
    CmdId.SCREEN_MAIN: [
        (0, 4, "uint32_le", "element_type"),
        (4, 2, "uint16_le", "width"),
        (6, 2, "uint16_le", "height"),
        (8, 2, "uint16_le", "x_position"),
        (10, 2, "uint16_le", "y_position"),
        (12, 2, "uint16_le", "color"),
    ],
    CmdId.KEY: [
        (0x1C, 1, "uint8", "arg0 (V0)"),
    ],
    CmdId.SYS_INFO: [
        (0, 2, "uint16_le", "display_width"),
        (2, 2, "uint16_le", "display_height"),
        (4, 2, "uint16_le", "unknown_60"),
        (6, 2, "uint16_le", "hw_id"),
        (8, 4, "uint32_le", "uptime_s"),
        (12, 2, "uint16_le", "vid"),
        (14, 2, "uint16_le", "pid"),
        (36, 2, "uint16_le", "config_crc"),
    ],
    CmdId.SETTING: [
        (0, 2, "uint16_le", "host_width"),
        (2, 2, "uint16_le", "host_height"),
    ],
}

# Commands to probe when taking a snapshot
PROBE_CMDS = [
    CmdId.INFO,
    CmdId.SYS_INFO,
    CmdId.SETTING,
    CmdId.DEVICE_NAME,
]

SNAPSHOTS_DIR = Path.home() / ".sayodevice" / "snapshots"


# ============================================================
# Data classes
# ============================================================

@dataclass
class FieldChange:
    """A single field-level change between two snapshots."""
    cmd_id: int
    offset: int
    old_value: int
    new_value: int
    size: int  # 1, 2, or 4 bytes
    field_label: str = ""

    def __str__(self) -> str:
        cmd_name = _cmd_name(self.cmd_id)
        label = self.field_label or f"byte[{self.offset}]"
        if self.size == 1:
            return f"{cmd_name} [{self.offset}] {label}: {self.old_value} -> {self.new_value}"
        elif self.size == 2:
            return f"{cmd_name} [{self.offset}-{self.offset+1}] {label}: {self.old_value} -> {self.new_value}"
        else:
            return f"{cmd_name} [{self.offset}-{self.offset+self.size-1}] {label}: {self.old_value} -> {self.new_value}"


@dataclass
class Snapshot:
    """A point-in-time capture of all device responses."""
    timestamp: float = 0.0
    responses: dict[int, bytes] = field(default_factory=dict)
    decoded: dict[int, dict] = field(default_factory=dict)
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "label": self.label,
            "responses": {str(k): v.hex() for k, v in self.responses.items()},
            "decoded": {str(k): v for k, v in self.decoded.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> Snapshot:
        return cls(
            timestamp=d.get("timestamp", 0.0),
            label=d.get("label", ""),
            responses={int(k): bytes.fromhex(v) for k, v in d.get("responses", {}).items()},
            decoded={int(k): v for k, v in d.get("decoded", {}).items()},
        )


@dataclass
class Discovery:
    """A user-labeled protocol discovery from comparing two snapshots."""
    description: str = ""
    before: Snapshot = field(default_factory=Snapshot)
    after: Snapshot = field(default_factory=Snapshot)
    changed_fields: list[FieldChange] = field(default_factory=list)
    ai_analysis: str = ""

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "changed_fields": [
                {
                    "cmd_id": fc.cmd_id,
                    "offset": fc.offset,
                    "old_value": fc.old_value,
                    "new_value": fc.new_value,
                    "size": fc.size,
                    "field_label": fc.field_label,
                }
                for fc in self.changed_fields
            ],
            "ai_analysis": self.ai_analysis,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Discovery:
        return cls(
            description=d.get("description", ""),
            before=Snapshot.from_dict(d.get("before", {})),
            after=Snapshot.from_dict(d.get("after", {})),
            changed_fields=[
                FieldChange(**fc) for fc in d.get("changed_fields", [])
            ],
            ai_analysis=d.get("ai_analysis", ""),
        )


# ============================================================
# Capture logic
# ============================================================

def capture_snapshot(device, label: str = "") -> Snapshot:
    """Probe all known commands and record responses."""
    snap = Snapshot(timestamp=time.time(), label=label)
    for cmd_id in PROBE_CMDS:
        resp = device.send_single(cmd_id)
        if resp:
            snap.responses[cmd_id] = bytes(resp)
            snap.decoded[cmd_id] = _decode_response(cmd_id, resp)
    return snap


def _decode_response(cmd_id: int, raw: bytes) -> dict:
    """Decode a raw response into a field dict using known fields."""
    # Skip the 8-byte packet+cmd header to get payload
    payload = raw[8:] if len(raw) > 8 else raw
    result: dict[str, Any] = {}
    fields = _KNOWN_FIELDS.get(cmd_id, [])
    for offset, size, _ftype, label in fields:
        result[label] = _read_field(payload, offset, size)
    return result


# ============================================================
# Diff logic
# ============================================================

def diff_snapshots(before: Snapshot, after: Snapshot) -> list[FieldChange]:
    """Compare two snapshots and return all field-level changes."""
    changes: list[FieldChange] = []
    # Compare all commands present in either snapshot
    all_cmd_ids = set(before.responses.keys()) | set(after.responses.keys())

    for cmd_id in sorted(all_cmd_ids):
        raw_before = before.responses.get(cmd_id, b"")
        raw_after = after.responses.get(cmd_id, b"")
        # Skip packet header (8 bytes) to compare payload only
        pay_before = raw_before[8:] if len(raw_before) > 8 else raw_before
        pay_after = raw_after[8:] if len(raw_after) > 8 else raw_after

        if not pay_before or not pay_after:
            continue

        # Check known fields first
        known = _KNOWN_FIELDS.get(cmd_id, [])
        known_offsets = set()
        for offset, size, _ftype, label in known:
            known_offsets.update(range(offset, offset + size))
            old_val = _read_field(pay_before, offset, size)
            new_val = _read_field(pay_after, offset, size)
            if old_val != new_val:
                changes.append(FieldChange(
                    cmd_id=cmd_id, offset=offset,
                    old_value=old_val, new_value=new_val,
                    size=size, field_label=label,
                ))

        # Check remaining bytes for unknown changes
        min_len = min(len(pay_before), len(pay_after))
        i = 0
        while i < min_len:
            if i in known_offsets:
                i += 1
                continue
            if pay_before[i] != pay_after[i]:
                # Try to detect 2-byte or 4-byte aligned changes
                size = _detect_change_size(pay_before, pay_after, i, min_len, known_offsets)
                old_val = _read_field(pay_before, i, size)
                new_val = _read_field(pay_after, i, size)
                changes.append(FieldChange(
                    cmd_id=cmd_id, offset=i,
                    old_value=old_val, new_value=new_val,
                    size=size, field_label="",
                ))
                i += size
            else:
                i += 1

    return changes


def get_changed_byte_offsets(before: Snapshot, after: Snapshot, cmd_id: int) -> set[int]:
    """Return the set of payload byte offsets that differ between snapshots."""
    raw_before = before.responses.get(cmd_id, b"")
    raw_after = after.responses.get(cmd_id, b"")
    pay_before = raw_before[8:] if len(raw_before) > 8 else raw_before
    pay_after = raw_after[8:] if len(raw_after) > 8 else raw_after
    changed = set()
    for i in range(min(len(pay_before), len(pay_after))):
        if pay_before[i] != pay_after[i]:
            changed.add(i)
    return changed


# ============================================================
# Persistence
# ============================================================

def save_discovery(discovery: Discovery) -> Path:
    """Save a discovery to ~/.sayodevice/snapshots/ as JSON."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(discovery.after.timestamp or time.time())
    filename = f"discovery_{ts}.json"
    path = SNAPSHOTS_DIR / filename
    path.write_text(json.dumps(discovery.to_dict(), indent=2))
    return path


def list_discoveries() -> list[tuple[Path, Discovery]]:
    """Load all saved discoveries, sorted newest first."""
    if not SNAPSHOTS_DIR.exists():
        return []
    results = []
    for f in sorted(SNAPSHOTS_DIR.glob("discovery_*.json"), reverse=True):
        try:
            d = Discovery.from_dict(json.loads(f.read_text()))
            results.append((f, d))
        except Exception:
            continue
    return results


# ============================================================
# Helpers
# ============================================================

def _cmd_name(cmd_id: int) -> str:
    try:
        return CmdId(cmd_id).name
    except ValueError:
        return f"CMD_0x{cmd_id:02X}"


def _read_field(data: bytes, offset: int, size: int) -> int:
    if offset + size > len(data):
        return 0
    if size == 1:
        return data[offset]
    elif size == 2:
        return struct.unpack_from("<H", data, offset)[0]
    elif size == 4:
        return struct.unpack_from("<I", data, offset)[0]
    return int.from_bytes(data[offset:offset + size], "little")


def _detect_change_size(
    before: bytes, after: bytes, offset: int, length: int,
    known_offsets: set[int],
) -> int:
    """Detect likely field size for an unknown changed byte."""
    # If 2-byte aligned and next byte also changed, treat as uint16
    if offset % 2 == 0 and offset + 1 < length:
        if (offset + 1) not in known_offsets:
            if before[offset + 1] != after[offset + 1]:
                # Check for 4-byte field
                if (offset % 4 == 0 and offset + 3 < length
                        and (offset + 2) not in known_offsets
                        and (offset + 3) not in known_offsets):
                    if (before[offset + 2] != after[offset + 2]
                            or before[offset + 3] != after[offset + 3]):
                        return 4
                return 2
    return 1

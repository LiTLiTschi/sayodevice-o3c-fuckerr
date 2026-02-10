# CLAUDE.md — Project Instructions for Claude Code

## What This Is

`sayo-fuckerr-apps` — Applications built on top of the `sayodevice` library for controlling a SayoDevice O3C keyboard via USB HID.

The library repo: https://github.com/LiTLiTschi/sayodevice-o3c-fuckerr

## Project Structure

```
sayo-fuckerr-apps/
├── pyproject.toml              # project config, depends on sayodevice
├── sequence_gate/              # 4x2 step sequencer + ADSR + StemFX
│   ├── __init__.py
│   ├── __main__.py             # python -m sequence_gate
│   └── app.py                  # all app code
└── (future apps go here as sibling packages)
```

## How to Run

```bash
# Install (editable, with MIDI support):
pip install -e ".[midi]"

# Run sequence gate:
python -m sequence_gate --midi --setup     # interactive config + MIDI
python -m sequence_gate --midi             # direct run with MIDI
python -m sequence_gate --debug            # keyboard debug mode (no device)
sequence-gate --midi                       # console script shortcut
```

## Dependencies

- `sayodevice` — the USB HID library (install from git):
  ```
  uv pip install --upgrade --no-cache git+https://github.com/LiTLiTschi/sayodevice-o3c-fuckerr.git
  ```
- `mido` + `python-rtmidi` — optional, for MIDI features

## Device & Protocol Quick Reference

- SAYO O3C: VID=0x8089, PID=0x0009, 1024-byte HID packets
- 160x80 pixel screen, 16 compositable layers (element_index 0-15)
- 3 buttons + rotary encoder (left/right/click)
- Button LEDs: NOT controllable (LIGHT 0x11 / PALETTE 0x12 exist but undocumented)
- Screen elements: element_type=1 visible, 0=hidden. Colors as #RRGGBB strings.

## Sequence Gate Architecture

- **Screen cycling**: knob click cycles sequencer → stemfx → adsr
- **Beat tracking**: always runs across all screens (MIDI stays in sync)
- **Thread-safe HID**: `_LockedDev` proxy serializes USB writes; dedicated poller thread for buttons
- **Config**: JSON at `~/.sayodevice/sequence_gate.json` — screens, MIDI ports, CC channel, StemFX stems
- **MIDI CC output**: CC 102-119 for Bome MIDI Translator Pro, CC 20-22 for StemFX

### Key Classes

| Class | Purpose |
|-------|---------|
| `SequenceGate` | Main app: sequencer logic, screen dispatch, beat tracking |
| `ADSREditor` | ADSR envelope curve editor (12-bar chart visualization) |
| `StemFXScreen` | 3-stem toggle for Rekordbox (drums/inst/voc) |
| `CCOutput` | MIDI CC broadcaster with change detection + verbose logging |
| `SequencerMidi` | Note output, learn mode, through routing |
| `DeviceInput` | Threaded USB poller with knob accumulator (25ms debounce) |
| `DebugKeyboardInput` | Console keyboard for testing without device |

## User Preferences

- Vibes coding, fast iteration, no unnecessary safety gates
- User is on Windows (PowerShell), device is physical USB
- No `gh` CLI available

## Version Bumping

When releasing: bump version in BOTH `pyproject.toml` AND `sequence_gate/__init__.py`.

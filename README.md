# sayodevice

Python library and CLI for controlling **SayoDevice O3C** keyboards via USB HID (API v2).

Reverse-engineered from Wireshark captures and [khang06's protocol docs](https://gist.github.com/khang06/6186543b560548370ce7cc08cad7f710). And of course my favourite clanker, claude.

## Installation

```powershell
# From GitHub (latest)
pip install git+https://github.com/LiTLiTschi/sayodevice-o3c-fuckerr.git

# With MIDI support
pip install "sayodevice[midi] @ git+https://github.com/LiTLiTschi/sayodevice-o3c-fuckerr.git"

# From source
pip install .

# Editable/dev install
pip install -e ".[midi]"
```

## Quick Start

```python
from sayodevice import SayoDevice

with SayoDevice.open() as dev:
    # Query device
    info = dev.get_info()
    print(info)

    # Read buttons
    btns = dev.get_buttons()
    if btns.button1:
        print("Button 1 pressed!")

    # Screen control
    dev.fill_screen('#1295FF')
    dev.draw_rect(40, 0, 40, 40, '#FF0000', layer=15)

    # Key config
    dev.set_key_arg0(128)
```

## Button Detection

The device has 3 buttons and a rotary knob (click + rotate). Read them with `get_buttons()`:

```python
from sayodevice import SayoDevice

with SayoDevice.open() as dev:
    btns = dev.get_buttons()
    print(btns)  # ButtonState(btn1, knob_right)
    print(btns.button1)     # True/False
    print(btns.button2)     # True/False
    print(btns.button3)     # True/False
    print(btns.knob_click)  # True/False
    print(btns.knob_left)   # True/False
    print(btns.knob_right)  # True/False
    print(btns.any_pressed) # True if anything is active
```

### Event Listener (background thread)

```python
from sayodevice import SayoDevice, DeviceListener

with SayoDevice.open() as dev:
    listener = DeviceListener(dev, poll_interval_ms=20)
    listener.on_button(lambda e: print(f"{e.button} {'pressed' if e.pressed else 'released'}"))
    listener.on_fn_change(lambda e: print(f"FN: {e.old_fn} -> {e.new_fn}"))
    listener.start()

    # ... your app logic ...

    listener.stop()
```

## Screen Control

```python
with SayoDevice.open() as dev:
    # Fill entire screen (160x80)
    dev.fill_screen('#1295FF')

    # Draw rectangles on layers (0-15, higher = on top)
    dev.draw_rect(0, 0, 40, 40, '#FF0000', layer=15)

    # Low-level: full control over element properties
    dev.set_screen_element(
        x=120, y=0, width=40, height=40,
        color='#00FF00', element_type=1, element_index=14,
    )

    # Clear a layer
    dev.clear_layer(15)

    # Manual refresh (set_screen_element with refresh=False, then batch)
    dev.refresh_display()
```

## ADSR Envelopes

Configurable Attack-Decay-Sustain-Release envelopes with linear, exponential, and logarithmic curves:

```python
from sayodevice import ADSREnvelope, EnvelopeGenerator, CurveType

env = ADSREnvelope(
    attack_ms=50, decay_ms=100, sustain=0.8, release_ms=200,
    attack_curve=CurveType.LINEAR,
    decay_curve=CurveType.EXPONENTIAL,
    release_curve=CurveType.EXPONENTIAL,
)

gen = EnvelopeGenerator(env)
gen.gate_on()      # Start attack
val = gen.get_value()  # 0.0 to 1.0
gen.gate_off()     # Start release
```

## MIDI Bridge (optional)

Requires `pip install sayodevice[midi]` (mido + python-rtmidi):

```python
from sayodevice import SayoDevice, DeviceListener
from sayodevice.midi import MidiBridge, list_midi_ports

# List available ports
print(list_midi_ports())

# Bridge device buttons to MIDI
with SayoDevice.open() as dev:
    listener = DeviceListener(dev, poll_interval_ms=20)
    bridge = MidiBridge(listener)
    bridge.map_button('button1', note=60)  # C4
    bridge.map_button('button2', note=62)  # D4
    bridge.map_knob(cc=1)                  # Mod wheel
    bridge.start()

    # ... your app loop ...

    bridge.stop()
    listener.stop()
```

## Named Setups (Presets)

Save and load device configurations as JSON files:

```python
from sayodevice import DeviceSetup, ScreenElement, KeyConfig, save_setup, load_setup

# Define a setup
setup = DeviceSetup(name="seq-gate", description="Sequence gate layout", screen_elements=[
    ScreenElement(x=0, y=0, width=160, height=80, color="#1295FF", element_index=14),
    ScreenElement(x=0, y=0, width=40, height=40, color="#000000", element_index=15),
], key_configs=[
    KeyConfig(arg0=128, key_index=0),
])

# Save to ~/.sayodevice/setups/seq-gate.json
save_setup(setup)

# Load and apply
with SayoDevice.open() as dev:
    load_setup("seq-gate").apply(dev)
```

CLI setup management:

```
sayodevice --classic setup list
sayodevice --classic setup show seq-gate
sayodevice --classic setup apply seq-gate
sayodevice --classic setup delete seq-gate
```

## CLI Usage

The TUI launches by default. Use `--classic` for the argparse CLI:

```powershell
# TUI (Textual app with live capture, snapshots, AI analysis)
sayodevice

# Classic CLI
sayodevice --classic scan
sayodevice --classic info
sayodevice --classic set-arg0 128
sayodevice --classic interactive

# Run a script
sayodevice run examples/sequence_gate.py --midi --adsr

# MIDI tools
sayodevice --classic midi ports
sayodevice --classic midi bridge --note button1:60 --note button2:62 --knob-cc 1
```

### Interactive Console (REPL)

```
sayodevice --classic interactive
```

| Command | Description |
|---------|-------------|
| `info` | Query device info |
| `name` | Query device name |
| `arg0 <0-255> [--nosave]` | Set Arg0 value |
| `screen_pos <x> <y>` | Set screen element position |
| `color <#RRGGBB>` | Set screen element color |
| `button_probe [secs]` | Detect button presses (hex diff) |
| `setup list\|show\|apply` | Manage named setups |
| `probe <field> [range]` | Sweep screen element fields |
| `capture [cmd_ids]` | Probe and decode responses |
| `sniff [seconds]` | Listen for incoming packets |
| `save` | Send Save command |
| `send <cmd_hex> [data]` | Send raw command |
| `sweep [delay] [step]` | Sweep Arg0 0-255 |
| `status` | Show connection info |
| `quit` | Exit |

## Examples

See the `examples/` directory:

- **`sequence_gate.py`** — 4x2 step sequencer with beat visualization, knob subdivision control, MIDI output, ADSR envelope editor (knob click), and MIDI learn mode.

```powershell
# Run with real device buttons
python examples/sequence_gate.py

# Debug mode (keyboard input)
python examples/sequence_gate.py --debug

# With MIDI output (sends notes on each active beat)
python examples/sequence_gate.py --midi

# With MIDI to a specific port
python examples/sequence_gate.py --midi --output "loopMIDI Port"

# All features
python examples/sequence_gate.py --midi --output "loopMIDI Port" --input "MIDI Controller"
```

### Sequence Gate Controls

**Sequencer mode:**
| Input | Action |
|-------|--------|
| Button 1 | Cursor left |
| Button 2 | Toggle square on/off |
| Button 3 | Cursor right |
| Knob left/right | Subdivision down/up (quarter/eighth/16th/32nd) |
| Knob click | Enter ADSR editor |

**ADSR editor mode (4 colored bars on screen):**
| Input | Action |
|-------|--------|
| Button 1/3 | Select parameter (A/D/S/R/curves) |
| Button 2 | Exit editor |
| Knob left/right | Adjust selected value |

**MIDI learn mode:**
| Input | Action |
|-------|--------|
| Knob click | Toggle learn mode |
| Button 1/3 | Move cursor to target position |
| Play MIDI note | Assign note to cursor position |

**Default MIDI note map (4x2 grid):**
```
C4  D4  E4  F4
G4  A4  B4  C5
```

## Protocol Overview

The SayoDevice O3C exposes multiple HID interfaces. Config commands go through vendor-specific usage pages:

| Usage Page | Mode | Packet Size | Report ID |
|------------|------|-------------|-----------|
| `0xFF12` | High-speed (8kHz) | 1024 bytes | `0x22` |
| `0xFF11` | Normal | 64 bytes | `0x21` |
| `0xFF00` | API v1 (legacy) | 64 bytes | `0x02` |

**Packet structure** (API v2):
```
[0] report_id
[1] echo
[2-3] checksum (LE16)
[4+] commands (length-prefixed TLV)
```

**Checksum**: sum of all 16-bit LE words in the packet (with checksum field zeroed), masked to 16 bits.

**Button state**: `KEY_STATUS` (CMD 0x1E, index=0) byte [8] is an active-low bitmask:
```
bit 0: button1     bit 3: knob_click
bit 1: button2     bit 4: knob_left
bit 2: button3     bit 5: knob_right
```
0x3F = all released. Bit cleared = pressed.

## References

- [khang06/O3C Internals](https://gist.github.com/khang06/6186543b560548370ce7cc08cad7f710)
- [Sayobot/SayoDevice Web HID](https://github.com/Sayobot/sayo-device-web-hid)
- [Sayobot/Sayo_CLI](https://github.com/Sayobot/Sayo_CLI)

## License

MIT

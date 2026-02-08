# sayodevice

Python library and CLI for controlling **SayoDevice O3C** keyboards via USB HID (API v2).

Reverse-engineered from Wireshark captures and [khang06's protocol docs](https://gist.github.com/khang06/6186543b560548370ce7cc08cad7f710). And of course my favourite clanker, claude.

## Installation

```powershell
# From GitHub (latest)
pip install git+https://github.com/LiTLiTschi/sayodevice-o3c-fuckerr.git

# From source
pip install .

# Editable/dev install
pip install -e .
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

- **`sequence_gate.py`** — 4x2 step sequencer with beat visualization, cursor navigation, and BPM control via knob. Demonstrates `get_buttons()`, screen element tracking, and the input abstraction pattern.

```powershell
# Run with real device buttons
python examples/sequence_gate.py

# Debug mode (keyboard input)
python examples/sequence_gate.py --debug
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

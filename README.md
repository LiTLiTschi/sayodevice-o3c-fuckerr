# sayodevice

Python library and CLI for controlling **SayoDevice O3C** keyboards via USB HID (API v2).

Reverse-engineered from Wireshark captures and [khang06's protocol docs](https://gist.github.com/khang06/6186543b560548370ce7cc08cad7f710). And of course my favourite clanker, claude.

## Installation

```powershell
# From source (in the package directory)
pip install .

# Or editable/dev install
pip install -e .
```

## Library Usage

```python
from sayodevice import SayoDevice

# Auto-detect and open device
with SayoDevice.open() as dev:
    # Set Arg0 (V0 script parameter) and save
    dev.set_key_arg0(128)

    # Query device info
    info = dev.get_info()
    print(info)

    # Send arbitrary commands
    dev.send_single(0x00)  # Info request

    # Low-level: set without saving
    dev.set_key_arg0(64, save=False)
    # ... do more stuff ...
    dev.save()  # save manually
```

### Force a specific interface

```python
from sayodevice import SayoDevice, UsagePage

# Use high-speed (0xFF12, 1024-byte packets)
with SayoDevice.open(usage_page=UsagePage.HIGHSPEED) as dev:
    dev.set_key_arg0(200)

# Use normal (0xFF11, 64-byte packets)
with SayoDevice.open(usage_page=UsagePage.NORMAL) as dev:
    dev.set_key_arg0(200)
```

### Enumerate interfaces

```python
from sayodevice import SayoDevice

for iface in SayoDevice.enumerate():
    print(f"{iface}  {'◄ CONFIG' if iface.is_config else ''}")
```

## CLI Usage

After `pip install`, the `sayodevice` command is available:

```powershell
# Scan for devices
sayodevice scan

# Query device info
sayodevice info

# Set Arg0 to 128 (with auto-save)
sayodevice set-arg0 128

# Set without saving
sayodevice set-arg0 64 --nosave

# Force specific interface
sayodevice --interface normal set-arg0 128

# Interactive debugging console
sayodevice interactive
```

### Interactive Console

The REPL (`sayodevice interactive`) provides these commands:

| Command | Description |
|---------|-------------|
| `info` | Query device info |
| `name` | Query device name |
| `arg0 <0-255> [--nosave]` | Set Arg0 value |
| `save` | Send Save command |
| `send <cmd_hex> [data_hex]` | Send raw command |
| `raw <hex_bytes>` | Send raw packet (auto-pads + checksum) |
| `read [timeout_ms]` | Read from device |
| `sweep [delay_ms] [step]` | Sweep Arg0 0→255 |
| `status` | Show connection info |
| `interfaces` | List HID interfaces |
| `quit` | Exit |

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

## References

- [khang06/O3C Internals](https://gist.github.com/khang06/6186543b560548370ce7cc08cad7f710)
- [Sayobot/SayoDevice Web HID](https://github.com/Sayobot/sayo-device-web-hid)
- [Sayobot/Sayo_CLI](https://github.com/Sayobot/Sayo_CLI)

## License

MIT

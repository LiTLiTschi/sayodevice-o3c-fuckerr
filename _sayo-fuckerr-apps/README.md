# sayo-fuckerr-apps

Applications for the [SayoDevice O3C](https://github.com/LiTLiTschi/sayodevice-o3c-fuckerr) keyboard.

## Apps

### Sequence Gate

4x2 step sequencer with MIDI output, ADSR envelope editor, and Rekordbox StemFX control.

**Features:**
- 4x2 grid sequencer with per-step activation
- Screen cycling: sequencer → StemFX → ADSR editor (knob click)
- ADSR envelope editor with curve visualization
- StemFX screen for Rekordbox 3-stem toggle (drums/inst/voc)
- MIDI CC output for Bome MIDI Translator Pro
- Configurable MIDI ports, CC channels, stems
- Interactive `--setup` terminal config menu
- Persistent JSON config at `~/.sayodevice/sequence_gate.json`

## Install

```bash
# Install the library first:
uv pip install --upgrade --no-cache git+https://github.com/LiTLiTschi/sayodevice-o3c-fuckerr.git

# Install apps (with MIDI support):
pip install -e ".[midi]"
```

## Run

```bash
sequence-gate --midi --setup     # interactive config + run
sequence-gate --midi             # direct run
sequence-gate --debug            # keyboard test mode (no device)
python -m sequence_gate --midi   # alternative
```

## License

MIT

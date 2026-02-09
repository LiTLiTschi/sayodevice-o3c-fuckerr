"""
SayoDevice O3C - MIDI bridge for button-to-MIDI mapping.

Bridges device button/knob events to MIDI output, and optionally
receives MIDI input for learn/listen/through modes.

Requires optional dependencies: mido + python-rtmidi
    pip install sayodevice[midi]

Usage::

    from sayodevice import SayoDevice, DeviceListener
    from sayodevice.midi import MidiBridge, list_midi_ports

    # List available MIDI ports
    ports = list_midi_ports()
    print(ports)

    # Bridge device buttons to MIDI
    with SayoDevice.open() as dev:
        listener = DeviceListener(dev, poll_interval_ms=20)
        bridge = MidiBridge(listener)
        bridge.map_button('button1', note=60)
        bridge.map_button('button2', note=62)
        bridge.map_knob(cc=1, channel=0, step=4)
        bridge.start()

        # ... your app loop ...

        bridge.stop()
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

try:
    import mido
except ImportError:
    raise ImportError(
        "mido is required for MIDI support. "
        "Install with: pip install sayodevice[midi]  "
        "or: pip install mido python-rtmidi"
    )

from .listener import DeviceListener, ButtonEvent


# ============================================================
# Data types
# ============================================================

@dataclass
class MidiMapping:
    """Maps a device button to a MIDI message."""
    button: str              # 'button1', 'button2', 'button3', 'knob_click'
    message_type: str        # 'note' | 'cc'
    channel: int = 0
    note_or_cc: int = 60
    velocity: int = 127
    cc_value: int = 127

    def to_dict(self) -> dict:
        return {
            'button': self.button,
            'message_type': self.message_type,
            'channel': self.channel,
            'note_or_cc': self.note_or_cc,
            'velocity': self.velocity,
            'cc_value': self.cc_value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> MidiMapping:
        return cls(**d)


@dataclass
class KnobMapping:
    """Maps knob rotation to a MIDI CC."""
    cc: int = 1
    channel: int = 0
    step: int = 4
    value: int = 64       # current accumulator
    min_val: int = 0
    max_val: int = 127


# ============================================================
# Port discovery
# ============================================================

def list_midi_ports() -> dict[str, list[str]]:
    """List available MIDI input and output port names.

    Returns:
        Dict with 'inputs' and 'outputs' keys, each a list of port names.

    Example::

        ports = list_midi_ports()
        print(ports['outputs'])  # ['loopMIDI Port 1', 'Microsoft GS Wavetable Synth']
    """
    return {
        'inputs': mido.get_input_names(),
        'outputs': mido.get_output_names(),
    }


# ============================================================
# MIDI Bridge
# ============================================================

class MidiBridge:
    """
    Bridges SayoDevice button events to MIDI output.

    Optionally receives MIDI input for learn/listen/through modes.

    Args:
        listener: A DeviceListener instance (will be started if not running).
        output_port: MIDI output port name. Empty string = default port.
        input_port: MIDI input port name. Empty string = no input.
            Use None to auto-open the default input port.

    Example::

        bridge = MidiBridge(listener)
        bridge.map_button('button1', note=60)
        bridge.start()
    """

    def __init__(
        self,
        listener: DeviceListener | None = None,
        output_port: str = "",
        input_port: str | None = None,
    ):
        self._listener = listener
        self._output_port_name = output_port
        self._input_port_name = input_port

        self._output: mido.ports.BaseOutput | None = None
        self._input: mido.ports.BaseInput | None = None

        # Mappings
        self._button_mappings: dict[str, MidiMapping] = {}
        self._knob_mapping: KnobMapping | None = None

        # Active notes (for note_off on release)
        self._active_notes: dict[str, tuple[int, int]] = {}  # button -> (note, channel)

        # MIDI input callbacks
        self._midi_in_callbacks: list[Callable[[mido.Message], None]] = []

        # Through mode
        self.through_enabled: bool = False

        self._running = False

    # ---- Mapping configuration ----

    def map_button(
        self,
        button: str,
        note: int = 60,
        channel: int = 0,
        velocity: int = 127,
    ) -> None:
        """Map a button to a MIDI note (note_on on press, note_off on release)."""
        self._button_mappings[button] = MidiMapping(
            button=button,
            message_type='note',
            channel=channel,
            note_or_cc=note,
            velocity=velocity,
        )

    def map_button_cc(
        self,
        button: str,
        cc: int,
        value: int = 127,
        channel: int = 0,
    ) -> None:
        """Map a button to a MIDI CC (send value on press, 0 on release)."""
        self._button_mappings[button] = MidiMapping(
            button=button,
            message_type='cc',
            channel=channel,
            note_or_cc=cc,
            cc_value=value,
        )

    def map_knob(
        self,
        cc: int = 1,
        channel: int = 0,
        step: int = 4,
    ) -> None:
        """Map knob rotation to a MIDI CC with accumulator."""
        self._knob_mapping = KnobMapping(cc=cc, channel=channel, step=step)

    def on_midi_in(self, callback: Callable[[mido.Message], None]) -> None:
        """Register a callback for incoming MIDI messages."""
        self._midi_in_callbacks.append(callback)

    # ---- Port management ----

    def _open_output(self) -> None:
        if self._output is not None:
            return
        if self._output_port_name:
            self._output = mido.open_output(self._output_port_name)
        else:
            outputs = mido.get_output_names()
            if outputs:
                self._output = mido.open_output(outputs[0])
            else:
                print("[MidiBridge] Warning: no MIDI output ports available")

    def _open_input(self) -> None:
        if self._input is not None:
            return
        if self._input_port_name is None:
            return  # No input requested
        if self._input_port_name:
            self._input = mido.open_input(self._input_port_name, callback=self._on_midi_message)
        else:
            inputs = mido.get_input_names()
            if inputs:
                self._input = mido.open_input(inputs[0], callback=self._on_midi_message)

    def _on_midi_message(self, msg: mido.Message) -> None:
        """Handle incoming MIDI message."""
        # Through mode: forward to output
        if self.through_enabled and self._output:
            self._output.send(msg)

        # Fire callbacks
        for cb in self._midi_in_callbacks:
            try:
                cb(msg)
            except Exception:
                pass

    # ---- Send helpers ----

    def send_note_on(self, note: int, velocity: int = 127, channel: int = 0) -> None:
        """Send a MIDI note_on message."""
        if self._output:
            self._output.send(mido.Message('note_on', note=note, velocity=velocity, channel=channel))

    def send_note_off(self, note: int, channel: int = 0) -> None:
        """Send a MIDI note_off message."""
        if self._output:
            self._output.send(mido.Message('note_off', note=note, velocity=0, channel=channel))

    def send_cc(self, cc: int, value: int, channel: int = 0) -> None:
        """Send a MIDI control_change message."""
        if self._output:
            self._output.send(mido.Message('control_change', control=cc, value=value, channel=channel))

    def send(self, msg: mido.Message) -> None:
        """Send an arbitrary MIDI message."""
        if self._output:
            self._output.send(msg)

    # ---- Button event handler ----

    def _handle_button_event(self, event: ButtonEvent) -> None:
        """Handle a button press/release from DeviceListener."""
        mapping = self._button_mappings.get(event.button)
        if mapping:
            if mapping.message_type == 'note':
                if event.pressed:
                    self.send_note_on(mapping.note_or_cc, mapping.velocity, mapping.channel)
                    self._active_notes[event.button] = (mapping.note_or_cc, mapping.channel)
                else:
                    if event.button in self._active_notes:
                        note, ch = self._active_notes.pop(event.button)
                        self.send_note_off(note, ch)
            elif mapping.message_type == 'cc':
                if event.pressed:
                    self.send_cc(mapping.note_or_cc, mapping.cc_value, mapping.channel)
                else:
                    self.send_cc(mapping.note_or_cc, 0, mapping.channel)

        # Knob handling
        if self._knob_mapping:
            km = self._knob_mapping
            if event.button == 'knob_right' and event.pressed:
                km.value = min(km.max_val, km.value + km.step)
                self.send_cc(km.cc, km.value, km.channel)
            elif event.button == 'knob_left' and event.pressed:
                km.value = max(km.min_val, km.value - km.step)
                self.send_cc(km.cc, km.value, km.channel)

    # ---- Lifecycle ----

    def start(self) -> None:
        """Open MIDI ports and start listening for device events."""
        if self._running:
            return
        self._open_output()
        self._open_input()
        if self._listener:
            self._listener.on_button(self._handle_button_event)
            if not self._listener.is_running:
                self._listener.start()
        self._running = True

    def stop(self) -> None:
        """Release all active notes and close MIDI ports."""
        self._running = False
        # Release any held notes
        for button, (note, ch) in list(self._active_notes.items()):
            self.send_note_off(note, ch)
        self._active_notes.clear()

        if self._output:
            self._output.close()
            self._output = None
        if self._input:
            self._input.close()
            self._input = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def output_port_name(self) -> str | None:
        if self._output:
            return self._output.name
        return None

    @property
    def input_port_name(self) -> str | None:
        if self._input:
            return self._input.name
        return None

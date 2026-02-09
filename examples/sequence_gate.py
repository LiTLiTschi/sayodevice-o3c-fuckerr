"""
Sequence Gate — 4x2 step sequencer on SayoDevice O3C with MIDI output + ADSR.

Usage:
    python sequence_gate.py                        # device mode
    python sequence_gate.py --debug                # keyboard debug input
    python sequence_gate.py --midi                 # with MIDI output (default port)
    python sequence_gate.py --midi --output "port" # specific MIDI output port

Controls (device — sequencer mode):
    Button 1:    cursor left
    Button 2:    toggle square (hold 500ms for ready indicator)
    Button 3:    cursor right
    Knob left:   subdivision down (quarter → eighth → sixteenth → 32nd)
    Knob right:  subdivision up
    Knob click:  enter ADSR editor

Controls (device — ADSR editor mode):
    Button 1:    select previous parameter (A/D/S/R/curves)
    Button 2:    exit ADSR editor (return to sequencer)
    Button 3:    select next parameter
    Knob left:   decrease selected value
    Knob right:  increase selected value

Controls (device — MIDI learn mode):
    Knob click (while in sequencer): toggle MIDI learn mode
    In learn mode:
        Button 1/3: move cursor to grid position
        Play MIDI note: assign note to cursor position
        Knob click: exit learn mode

Controls (debug):
    1/2/3:       momentary button press
    1p/2p/3p:    hold button
    1r/2r/3r:    release button
    k1/k2/k3:    knob left/click/right
    +/-:         BPM +/-10
    m:           toggle MIDI learn mode
    e:           toggle ADSR editor
    q:           quit

MIDI note map (4x2 grid, default):
    (0,0)=C4  (1,0)=D4  (2,0)=E4  (3,0)=F4
    (0,1)=G4  (1,1)=A4  (2,1)=B4  (3,1)=C5
"""

import sys
import threading
from abc import ABC, abstractmethod
from time import sleep, time

import sayodevice


# ============================================================
# THEME CONFIGURATION
# ============================================================
#
# Color Format:
#   6 chars: #RRGGBB (fully opaque)
#   8 chars: #RRGGBBAA (with alpha — blended against background)

class Theme:
    BACKGROUND_COLOR = "#000000"

    GRID_COLOR = "#FFFFFF89"
    GRID_WIDTH = 8

    CURSOR_COLOR_NORMAL = "#FF0000E6"
    CURSOR_COLOR_READY = "#00FF00E6"
    CURSOR_COLOR_LEARN = "#FF00FFE6"  # purple for MIDI learn mode
    CURSOR_SIZE = 15
    CURSOR_READY_TIME = 500  # ms hold for green cursor

    SQUARE_ACTIVATED_COLOR = "#AFAFAFB3"
    SQUARE_ACTIVATED_WITH_BEAT_COLOR = "#7B9296D9"

    BEAT_INDICATOR_COLOR = "#6BDAFFFF"
    BEAT_INDICATOR_FIRST_COLOR = "#16FFEFFF"

    NOTE_SUBDIVISION = "SIXTEENTH"

    QUARTER_NOTE_MARKER_COLOR = "#FFFFFF99"
    QUARTER_NOTE_MARKER_WIDTH = 4

    DEFAULT_BPM = 120
    MIN_BPM = 30
    MAX_BPM = 300

    SCREEN_WIDTH = 160
    SCREEN_HEIGHT = 80
    SQUARE_SIZE = 40
    GRID_COLS = 4
    GRID_ROWS = 2

    # ADSR editor colors
    ADSR_BAR_A = "#00FF00"
    ADSR_BAR_D = "#FFFF00"
    ADSR_BAR_S = "#3399FF"
    ADSR_BAR_R = "#FF3300"
    ADSR_BAR_A_DIM = "#004400"
    ADSR_BAR_D_DIM = "#444400"
    ADSR_BAR_S_DIM = "#112244"
    ADSR_BAR_R_DIM = "#441100"
    ADSR_SELECTED_BORDER = "#FFFFFF"
    ADSR_CURVE_INDICATOR = "#FFFFFFCC"

    @staticmethod
    def blend_colors(foreground: str, background: str, alpha: float | None = None) -> str:
        """Blend foreground over background. Reads alpha from #RRGGBBAA if present."""
        if len(foreground) == 9:
            fg_r, fg_g, fg_b = int(foreground[1:3], 16), int(foreground[3:5], 16), int(foreground[5:7], 16)
            if alpha is None:
                alpha = int(foreground[7:9], 16) / 255.0
        else:
            fg_r, fg_g, fg_b = int(foreground[1:3], 16), int(foreground[3:5], 16), int(foreground[5:7], 16)
            if alpha is None:
                alpha = 1.0

        bg_r, bg_g, bg_b = int(background[1:3], 16), int(background[3:5], 16), int(background[5:7], 16)

        r = max(0, min(255, int(alpha * fg_r + (1 - alpha) * bg_r)))
        g = max(0, min(255, int(alpha * fg_g + (1 - alpha) * bg_g)))
        b = max(0, min(255, int(alpha * fg_b + (1 - alpha) * bg_b)))

        return f"#{r:02X}{g:02X}{b:02X}"


# Pre-compute all blended colors once at import time
class Colors:
    """Resolved RGB565-ready colors (no alpha, pre-blended against background)."""
    BG = Theme.BACKGROUND_COLOR
    _b = Theme.blend_colors

    GRID = _b(Theme.GRID_COLOR, BG)
    QUARTER = _b(Theme.QUARTER_NOTE_MARKER_COLOR, BG)
    CURSOR_NORMAL = _b(Theme.CURSOR_COLOR_NORMAL, BG)
    CURSOR_READY = _b(Theme.CURSOR_COLOR_READY, BG)
    CURSOR_LEARN = _b(Theme.CURSOR_COLOR_LEARN, BG)
    ACTIVATED = _b(Theme.SQUARE_ACTIVATED_COLOR, BG)
    ACTIVATED_BEAT = _b(Theme.SQUARE_ACTIVATED_WITH_BEAT_COLOR, BG)
    BEAT = _b(Theme.BEAT_INDICATOR_COLOR, BG)
    BEAT_FIRST = _b(Theme.BEAT_INDICATOR_FIRST_COLOR, BG)
    ADSR_CURVE = _b(Theme.ADSR_CURVE_INDICATOR, BG)


# ============================================================
# Note Subdivision
# ============================================================

SUBDIVISIONS = ["QUARTER", "EIGHTH", "SIXTEENTH", "THIRTY_SECOND"]
SUBDIVISION_MS = {
    "QUARTER": 60000,
    "EIGHTH": 30000,
    "SIXTEENTH": 15000,
    "THIRTY_SECOND": 7500,
}
SUBDIVISION_BEATS_PER_BAR = {
    "QUARTER": 4,
    "EIGHTH": 8,
    "SIXTEENTH": 16,
    "THIRTY_SECOND": 32,
}


# ============================================================
# Default MIDI note map for 4x2 grid
# ============================================================

DEFAULT_NOTE_MAP = {
    (0, 0): 60,  # C4
    (1, 0): 62,  # D4
    (2, 0): 64,  # E4
    (3, 0): 65,  # F4
    (0, 1): 67,  # G4
    (1, 1): 69,  # A4
    (2, 1): 71,  # B4
    (3, 1): 72,  # C5
}

NOTE_NAMES = {
    60: "C4", 61: "C#4", 62: "D4", 63: "D#4", 64: "E4", 65: "F4",
    66: "F#4", 67: "G4", 68: "G#4", 69: "A4", 70: "A#4", 71: "B4",
    72: "C5", 73: "C#5", 74: "D5", 75: "D#5", 76: "E5", 77: "F5",
}

def note_name(n: int) -> str:
    return NOTE_NAMES.get(n, f"#{n}")


# ============================================================
# Input Abstraction
# ============================================================

class InputHandler(ABC):
    @abstractmethod
    def poll(self) -> dict:
        """Returns dict with button1..3, knob_left/click/right, command."""
        pass


class DeviceInput(InputHandler):
    """Real SAYO device input via get_buttons()."""

    def __init__(self, device: sayodevice.SayoDevice):
        self.device = device
        self._prev = sayodevice.ButtonState()
        self._knob_armed = True

    def poll(self) -> dict:
        btns = self.device.get_buttons()
        prev = self._prev
        self._prev = btns

        # Knob encoder debounce: a full detent produces multiple
        # 0→1→0 bounce transitions. The _knob_armed flag ensures we
        # fire exactly one event per physical detent. After firing,
        # we disarm and only re-arm when both directions are released.
        knob_idle = not btns.knob_left and not btns.knob_right
        knob_right_edge = False
        knob_left_edge = False

        if self._knob_armed:
            if btns.knob_right and not prev.knob_right and not btns.knob_left:
                knob_right_edge = True
                self._knob_armed = False
            elif btns.knob_left and not prev.knob_left and not btns.knob_right:
                knob_left_edge = True
                self._knob_armed = False
        elif knob_idle:
            self._knob_armed = True

        result = {
            'button1': btns.button1,
            'button2': btns.button2,
            'button3': btns.button3,
            'knob_left': knob_left_edge,
            'knob_click': btns.knob_click and not prev.knob_click,
            'knob_right': knob_right_edge,
            'command': None,
        }
        return result

    def stop(self):
        pass


class DebugKeyboardInput(InputHandler):
    """Console keyboard input for testing without device buttons."""

    def __init__(self):
        self.keys: dict[str, bool] = {}
        self.held = {k: False for k in ('button1', 'button2', 'button3')}
        self.auto_release: dict[str, float | None] = {k: None for k in self.held}
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self.running:
            try:
                key = input("> ").strip().lower()
                if key:
                    self.keys[key] = True
            except (EOFError, KeyboardInterrupt):
                break

    def poll(self) -> dict:
        raw = self.keys.copy()
        self.keys.clear()
        now = time()

        # Auto-release expired holds
        for btn in self.held:
            if self.auto_release[btn] is not None and now >= self.auto_release[btn]:
                self.held[btn] = False
                self.auto_release[btn] = None

        # Momentary presses (auto-release after 10ms)
        for key, btn in (('1', 'button1'), ('2', 'button2'), ('3', 'button3')):
            if key in raw:
                self.held[btn] = True
                self.auto_release[btn] = now + 0.01
            if f'{key}p' in raw:
                self.held[btn] = True
                self.auto_release[btn] = None
            if f'{key}r' in raw:
                self.held[btn] = False
                self.auto_release[btn] = None

        result = {
            'button1': self.held['button1'],
            'button2': self.held['button2'],
            'button3': self.held['button3'],
            'knob_left': 'k1' in raw,
            'knob_click': 'k2' in raw,
            'knob_right': 'k3' in raw,
            'command': None,
        }

        if 'q' in raw or 'quit' in raw:
            result['command'] = 'quit'
        elif '+' in raw:
            result['command'] = 'bpm_up'
        elif '-' in raw:
            result['command'] = 'bpm_down'
        elif 'm' in raw:
            result['command'] = 'toggle_learn'
        elif 'e' in raw:
            result['command'] = 'toggle_adsr'

        return result

    def stop(self):
        self.running = False


# ============================================================
# ADSR Visual Editor
# ============================================================

class ADSREditor:
    """Visualizes and edits individual ADSR stages on the device screen.

    Shows the actual envelope curve shape as a 12-column bar chart.
    Each stage (A/D/S/R) gets its own full-screen view.

    Controls:
        Button 1:    previous stage (A←D←S←R)
        Button 3:    next stage (A→D→S→R)
        Button 2:    toggle property (time/value ↔ curve type)
        Knob:        adjust selected property
        Knob click:  exit to sequencer
    """

    STAGES = ['attack', 'decay', 'sustain', 'release']
    STAGE_COLORS = [Theme.ADSR_BAR_A, Theme.ADSR_BAR_D, Theme.ADSR_BAR_S, Theme.ADSR_BAR_R]
    STAGE_PROPS = {
        'attack': ['attack_ms', 'attack_curve'],
        'decay': ['decay_ms', 'decay_curve'],
        'sustain': ['sustain'],
        'release': ['release_ms', 'release_curve'],
    }
    PROP_LABELS = {
        'attack_ms': 'A time', 'attack_curve': 'A curve',
        'decay_ms': 'D time', 'decay_curve': 'D curve',
        'sustain': 'S level',
        'release_ms': 'R time', 'release_curve': 'R curve',
    }

    NUM_BARS = 12
    BAR_WIDTH = 12
    BAR_SPACING = 13  # 12px bar + 1px gap
    MAX_HEIGHT = 72
    BAR_Y_BOTTOM = 76

    def __init__(self, envelope):
        self.envelope = envelope
        self.stage_idx = 0  # 0=A, 1=D, 2=S, 3=R
        self.prop_idx = 0   # index into STAGE_PROPS[current_stage]
        self._prev_b1 = False
        self._prev_b2 = False
        self._prev_b3 = False
        self._element_states: dict[int, dict] = {}

    def enter(self):
        """Reset view to Attack stage when entering editor."""
        self.stage_idx = 0
        self.prop_idx = 0
        self._prev_b1 = False
        self._prev_b2 = False
        self._prev_b3 = False
        self._element_states.clear()

    @property
    def current_stage(self) -> str:
        return self.STAGES[self.stage_idx]

    @property
    def current_prop(self) -> str:
        props = self.STAGE_PROPS[self.current_stage]
        return props[self.prop_idx % len(props)]

    def _sample_curve(self) -> list[float]:
        """Sample the current stage's curve at NUM_BARS points."""
        from sayodevice.adsr import _apply_curve, _apply_curve_inverted
        env = self.envelope
        stage = self.current_stage
        samples = []

        for i in range(self.NUM_BARS):
            t = i / (self.NUM_BARS - 1)

            if stage == 'attack':
                val = _apply_curve(t, env.attack_curve)
            elif stage == 'decay':
                shaped = _apply_curve_inverted(t, env.decay_curve)
                val = env.sustain + (1.0 - env.sustain) * shaped
            elif stage == 'sustain':
                val = env.sustain
            elif stage == 'release':
                shaped = _apply_curve_inverted(t, env.release_curve)
                val = env.sustain * shaped
            else:
                val = 0.0

            samples.append(max(0.0, min(1.0, val)))

        return samples

    def _adjust_value(self, direction: int) -> None:
        """Adjust the current property by one step."""
        from sayodevice.adsr import CurveType
        prop = self.current_prop

        if prop == 'sustain':
            new_val = max(0.0, min(1.0, self.envelope.sustain + direction * 0.05))
            self.envelope.sustain = round(new_val, 2)
            print(f"  S = {self.envelope.sustain:.2f}")
        elif prop == 'attack_ms':
            new_val = max(0, min(2000, self.envelope.attack_ms + direction * 50))
            self.envelope.attack_ms = new_val
            print(f"  A = {self.envelope.attack_ms:.0f}ms")
        elif prop == 'decay_ms':
            new_val = max(0, min(2000, self.envelope.decay_ms + direction * 50))
            self.envelope.decay_ms = new_val
            print(f"  D = {self.envelope.decay_ms:.0f}ms")
        elif prop == 'release_ms':
            new_val = max(0, min(5000, self.envelope.release_ms + direction * 100))
            self.envelope.release_ms = new_val
            print(f"  R = {self.envelope.release_ms:.0f}ms")
        elif prop.endswith('_curve'):
            curves = list(CurveType)
            current = getattr(self.envelope, prop)
            idx = curves.index(current)
            new_idx = (idx + direction) % len(curves)
            setattr(self.envelope, prop, curves[new_idx])
            print(f"  {prop} = {curves[new_idx].value}")

    def _prop_indicator(self) -> tuple[int, str]:
        """Width and color of the top property indicator bar."""
        from sayodevice.adsr import CurveType
        prop = self.current_prop

        if prop == 'attack_ms':
            return max(2, int(min(self.envelope.attack_ms, 2000) / 2000 * 156)), '#FFFFFF'
        elif prop == 'decay_ms':
            return max(2, int(min(self.envelope.decay_ms, 2000) / 2000 * 156)), '#FFFFFF'
        elif prop == 'release_ms':
            return max(2, int(min(self.envelope.release_ms, 5000) / 5000 * 156)), '#FFFFFF'
        elif prop == 'sustain':
            return max(2, int(self.envelope.sustain * 156)), '#3399FF'
        elif prop.endswith('_curve'):
            curve = getattr(self.envelope, prop)
            colors = {
                CurveType.LINEAR: '#FFFFFF',
                CurveType.EXPONENTIAL: '#FF8800',
                CurveType.LOGARITHMIC: '#00FFFF',
            }
            return 156, colors.get(curve, '#FFFFFF')
        return 2, '#FFFFFF'

    def _set_element(self, dev, index: int, element_type: int,
                     x: int = 0, y: int = 0, width: int = 40, height: int = 40,
                     color: str = "#FFFFFF"):
        """Send screen element only if state actually changed."""
        state = {'type': element_type, 'x': x, 'y': y,
                 'w': width, 'h': height, 'color': color}
        if self._element_states.get(index) == state:
            return
        dev.set_screen_element(
            x=x, y=y, width=width, height=height,
            color=color, element_type=element_type, element_index=index,
            wait_response=False,
        )
        self._element_states[index] = state

    def render(self, dev) -> None:
        """Render the current stage's curve as a 12-column bar chart."""
        # Layer 0: background
        self._set_element(dev, 0, 1, 0, 0, 160, 80, Colors.BG)

        # Layers 1-12: curve sample bars
        samples = self._sample_curve()
        color = self.STAGE_COLORS[self.stage_idx]

        for i, val in enumerate(samples):
            bar_h = max(1, int(val * self.MAX_HEIGHT))
            bar_x = i * self.BAR_SPACING + 1
            bar_y = self.BAR_Y_BOTTOM - bar_h
            self._set_element(dev, i + 1, 1, bar_x, bar_y, self.BAR_WIDTH, bar_h, color)

        # Layer 13: bottom stage indicator (full-width bar in stage color)
        self._set_element(dev, 13, 1, 0, 77, 160, 3, color)

        # Layer 14: top property indicator bar
        bar_w, bar_c = self._prop_indicator()
        self._set_element(dev, 14, 1, 1, 0, bar_w, 3, bar_c)

        # Layer 15: clear
        self._set_element(dev, 15, 0, 0, 0, 1, 1, Colors.BG)

    def process_input(self, inp: dict) -> str | None:
        """Process input while in ADSR editor mode.

        Returns:
            'exit' to leave editor, 'quit' to quit app, None otherwise.
        """
        if inp.get('command') == 'quit':
            return 'quit'
        if inp.get('command') == 'toggle_adsr':
            return 'exit'

        # Knob click: exit editor
        if inp.get('knob_click'):
            return 'exit'

        b1 = inp.get('button1', False)
        b2 = inp.get('button2', False)
        b3 = inp.get('button3', False)

        # Button 1 edge: previous stage
        if b1 and not self._prev_b1:
            self.stage_idx = (self.stage_idx - 1) % 4
            self.prop_idx = 0
            print(f"  ADSR: {self.current_stage} [{self.PROP_LABELS[self.current_prop]}]")
        self._prev_b1 = b1

        # Button 3 edge: next stage
        if b3 and not self._prev_b3:
            self.stage_idx = (self.stage_idx + 1) % 4
            self.prop_idx = 0
            print(f"  ADSR: {self.current_stage} [{self.PROP_LABELS[self.current_prop]}]")
        self._prev_b3 = b3

        # Button 2 edge: toggle property (time/value ↔ curve type)
        if b2 and not self._prev_b2:
            props = self.STAGE_PROPS[self.current_stage]
            self.prop_idx = (self.prop_idx + 1) % len(props)
            print(f"  ADSR: editing {self.PROP_LABELS[self.current_prop]}")
        self._prev_b2 = b2

        # Knob: adjust selected property
        if inp.get('knob_right'):
            self._adjust_value(1)
        if inp.get('knob_left'):
            self._adjust_value(-1)

        return None


# ============================================================
# MIDI output handler for the sequencer
# ============================================================

class SequencerMidi:
    """Manages MIDI output for the sequence gate.

    Handles note_on/note_off for active beats, ADSR envelope integration,
    MIDI learn mode, and through routing.
    """

    def __init__(self, output_port: str = "", input_port: str = ""):
        self._output_port = output_port
        self._input_port = input_port
        self._midi_out = None
        self._midi_in = None
        self.note_map: dict[tuple[int, int], int] = dict(DEFAULT_NOTE_MAP)
        self._active_notes: set[int] = set()
        self._envelope_generators: dict[tuple[int, int], object] = {}
        self.envelope = None  # ADSREnvelope, set externally
        self.enabled = False

        # MIDI learn
        self.learn_mode = False
        self._learn_callback = None

        # MIDI through
        self.through_enabled = False

    def open(self) -> bool:
        """Open MIDI ports. Returns True if successful."""
        try:
            import mido
        except ImportError:
            print("[MIDI] mido not installed. Install with: pip install mido python-rtmidi")
            return False

        try:
            outputs = mido.get_output_names()
            if self._output_port:
                self._midi_out = mido.open_output(self._output_port)
            elif outputs:
                self._midi_out = mido.open_output(outputs[0])
            else:
                print("[MIDI] No output ports available")
                return False

            print(f"[MIDI] Output: {self._midi_out.name}")

            # Open input if available
            inputs = mido.get_input_names()
            if self._input_port:
                self._midi_in = mido.open_input(self._input_port, callback=self._on_midi_in)
                print(f"[MIDI] Input: {self._midi_in.name}")
            elif inputs:
                self._midi_in = mido.open_input(inputs[0], callback=self._on_midi_in)
                print(f"[MIDI] Input: {self._midi_in.name}")

            self.enabled = True
            return True
        except Exception as e:
            print(f"[MIDI] Error opening ports: {e}")
            return False

    def _on_midi_in(self, msg) -> None:
        """Handle incoming MIDI messages."""
        import mido
        # MIDI learn: capture note
        if self.learn_mode and msg.type == 'note_on' and msg.velocity > 0:
            if self._learn_callback:
                self._learn_callback(msg.note, msg.channel)
            return

        # Through mode
        if self.through_enabled and self._midi_out:
            self._midi_out.send(msg)

    def note_on(self, pos: tuple[int, int], velocity: int = 127) -> None:
        """Send note_on for a grid position."""
        if not self.enabled or not self._midi_out:
            return
        import mido
        note = self.note_map.get(pos)
        if note is not None:
            # Release any previously active note at this position
            if note in self._active_notes:
                self._midi_out.send(mido.Message('note_off', note=note, velocity=0))
            self._midi_out.send(mido.Message('note_on', note=note, velocity=velocity))
            self._active_notes.add(note)

    def note_off(self, pos: tuple[int, int]) -> None:
        """Send note_off for a grid position."""
        if not self.enabled or not self._midi_out:
            return
        import mido
        note = self.note_map.get(pos)
        if note is not None and note in self._active_notes:
            self._midi_out.send(mido.Message('note_off', note=note, velocity=0))
            self._active_notes.discard(note)

    def all_notes_off(self) -> None:
        """Release all active notes."""
        if not self.enabled or not self._midi_out:
            return
        import mido
        for note in list(self._active_notes):
            self._midi_out.send(mido.Message('note_off', note=note, velocity=0))
        self._active_notes.clear()

    def start_learn(self, callback) -> None:
        """Enter MIDI learn mode. Callback receives (note, channel)."""
        self.learn_mode = True
        self._learn_callback = callback
        print("[MIDI] Learn mode ON — play a note on your controller")

    def stop_learn(self) -> None:
        """Exit MIDI learn mode."""
        self.learn_mode = False
        self._learn_callback = None
        print("[MIDI] Learn mode OFF")

    def close(self) -> None:
        """Close MIDI ports."""
        self.all_notes_off()
        if self._midi_out:
            self._midi_out.close()
            self._midi_out = None
        if self._midi_in:
            self._midi_in.close()
            self._midi_in = None
        self.enabled = False


# ============================================================
# Sequence Gate
# ============================================================

class SequenceGate:
    def __init__(self, bpm: int, use_debug_input: bool = False,
                 midi_output: str = "", midi_input: str = "",
                 enable_midi: bool = False):
        self.bpm = bpm
        self.note_subdivision = Theme.NOTE_SUBDIVISION
        self.beats_per_bar = SUBDIVISION_BEATS_PER_BAR[self.note_subdivision]
        self.ms_between_beats = SUBDIVISION_MS[self.note_subdivision] / bpm
        self.device = sayodevice.SayoDevice.open()

        self.cursor_x = 0
        self.cursor_y = 0
        self.activated_squares: set[tuple[int, int]] = set()

        # Edge detection
        self.prev_button1 = False
        self.prev_button2 = False
        self.prev_button3 = False
        self.button2_press_time: float | None = None

        # Beat tracking
        self.beat_time = time()
        self.current_beat = 0
        self.total_beats = 0

        # Dirty-tracking for screen elements
        self.element_states: dict[int, dict] = {}

        # Input
        if use_debug_input:
            self.input: InputHandler = DebugKeyboardInput()
        else:
            self.input = DeviceInput(self.device)

        # MIDI
        self.midi = SequencerMidi(output_port=midi_output, input_port=midi_input)
        self._midi_enabled = enable_midi

        # ADSR (always available)
        self.adsr_editor: ADSREditor | None = None
        self._in_adsr_mode = False
        self._in_learn_mode = False

        # Envelope generators per grid position
        self._envelope_gens: dict[tuple[int, int], object] = {}

        # Track which squares had notes triggered on them (for note_off on beat departure)
        self._beat_notes: set[tuple[int, int]] = set()

    def _init_adsr(self):
        """Initialize ADSR components."""
        from sayodevice.adsr import ADSREnvelope, EnvelopeGenerator
        self.envelope = ADSREnvelope()
        self.adsr_editor = ADSREditor(self.envelope)
        self.midi.envelope = self.envelope

        # Create envelope generators for each grid position
        for col in range(Theme.GRID_COLS):
            for row in range(Theme.GRID_ROWS):
                self._envelope_gens[(col, row)] = EnvelopeGenerator(self.envelope)

    def _clear_all_elements(self, dev):
        """Reset all 16 screen elements to empty. Used between view switches."""
        for i in range(16):
            dev.set_screen_element(element_index=i, element_type=0, wait_response=False)
        self.element_states.clear()
        if self.adsr_editor:
            self.adsr_editor._element_states.clear()

    def _set_element(self, dev, index: int, element_type: int,
                     x: int = 0, y: int = 0, width: int = 40, height: int = 40,
                     color: str = "#FFFFFF"):
        """Send screen element only if state actually changed."""
        state = {'type': element_type, 'x': x, 'y': y,
                 'w': width, 'h': height, 'color': color}
        if self.element_states.get(index) == state:
            return
        dev.set_screen_element(
            x=x, y=y, width=width, height=height,
            color=color, element_type=element_type, element_index=index,
            wait_response=False,
        )
        self.element_states[index] = state

    def _set_bpm(self, bpm: int):
        self.bpm = max(Theme.MIN_BPM, min(Theme.MAX_BPM, bpm))
        self.ms_between_beats = SUBDIVISION_MS[self.note_subdivision] / self.bpm
        print(f"BPM: {self.bpm}")

    def _beat_color(self, activated: bool, is_first: bool) -> str:
        """Color for a square that currently has the beat on it."""
        if activated:
            return Colors.ACTIVATED_BEAT
        return Colors.BEAT_FIRST if is_first else Colors.BEAT

    def _draw_grid_lines(self, dev):
        """Draw grid lines (layers 12-15). Uses self.note_subdivision."""
        is_16th = self.note_subdivision == "SIXTEENTH"
        self._set_element(dev, 12, 1, 36, 0, Theme.GRID_WIDTH, 80, Colors.GRID)
        self._set_element(dev, 13, 1, 76, 0, Theme.GRID_WIDTH, 80,
                          Colors.QUARTER if not is_16th else Colors.GRID)
        self._set_element(dev, 14, 1, 116, 0, Theme.GRID_WIDTH, 80, Colors.GRID)
        self._set_element(dev, 15, 1, 0, 36, 160, Theme.GRID_WIDTH,
                          Colors.QUARTER if is_16th else Colors.GRID)

    def _set_subdivision(self, direction: int, dev):
        """Cycle note subdivision (knob left=-1, knob right=+1)."""
        idx = SUBDIVISIONS.index(self.note_subdivision)
        new_idx = max(0, min(len(SUBDIVISIONS) - 1, idx + direction))
        if new_idx == idx:
            return
        self.note_subdivision = SUBDIVISIONS[new_idx]
        self.beats_per_bar = SUBDIVISION_BEATS_PER_BAR[self.note_subdivision]
        self.ms_between_beats = SUBDIVISION_MS[self.note_subdivision] / self.bpm
        print(f"Subdivision: {self.note_subdivision} ({self.beats_per_bar} beats/bar)")
        self._draw_grid_lines(dev)

    def _midi_learn_note(self, note: int, channel: int) -> None:
        """Callback for MIDI learn — assign note to current cursor position."""
        pos = (self.cursor_x, self.cursor_y)
        old_note = self.midi.note_map.get(pos)
        self.midi.note_map[pos] = note
        print(f"[MIDI Learn] ({pos[0]},{pos[1]}) = {note_name(note)} "
              f"(was {note_name(old_note) if old_note else 'none'})")

    def update_beat(self):
        elapsed_ms = (time() - self.beat_time) * 1000
        if elapsed_ms >= self.ms_between_beats:
            self.beat_time = time()
            self.current_beat = (self.current_beat + 1) % 8
            self.total_beats = (self.total_beats + 1) % self.beats_per_bar

    def process_input(self, inp: dict, dev) -> bool:
        """Handle all input in sequencer mode. Returns False to quit."""
        # System commands
        cmd = inp.get('command')
        if cmd == 'quit':
            return False
        elif cmd == 'bpm_up':
            self._set_bpm(self.bpm + 10)
        elif cmd == 'bpm_down':
            self._set_bpm(self.bpm - 10)
        elif cmd == 'toggle_learn' and self._midi_enabled:
            if self._in_learn_mode:
                self.midi.stop_learn()
                self._in_learn_mode = False
            else:
                self.midi.start_learn(self._midi_learn_note)
                self._in_learn_mode = True
        elif cmd == 'toggle_adsr':
            self._in_adsr_mode = True
            self.adsr_editor.enter()
            self._clear_all_elements(dev)
            print("[ADSR] Entering editor — attack")
            return True

        # Knob: subdivision control + mode switches
        if inp.get('knob_right'):
            self._set_subdivision(1, dev)
        if inp.get('knob_left'):
            self._set_subdivision(-1, dev)
        if inp.get('knob_click'):
            if self._in_learn_mode:
                self.midi.stop_learn()
                self._in_learn_mode = False
            else:
                self._in_adsr_mode = True
                self.adsr_editor.enter()
                self._clear_all_elements(dev)
                print("[ADSR] Entering editor — attack")
                return True

        now = time()
        b1 = inp.get('button1', False)
        b2 = inp.get('button2', False)
        b3 = inp.get('button3', False)

        # Button 1: cursor left (on press edge)
        if b1 and not self.prev_button1:
            self.cursor_x -= 1
            if self.cursor_x < 0:
                self.cursor_x = Theme.GRID_COLS - 1
                self.cursor_y = (self.cursor_y - 1) % Theme.GRID_ROWS
            print(f"Cursor: ({self.cursor_x}, {self.cursor_y})"
                  + (f" [{note_name(self.midi.note_map.get((self.cursor_x, self.cursor_y), 0))}]"
                     if self._midi_enabled else ""))
        self.prev_button1 = b1

        # Button 3: cursor right (on press edge)
        if b3 and not self.prev_button3:
            self.cursor_x += 1
            if self.cursor_x >= Theme.GRID_COLS:
                self.cursor_x = 0
                self.cursor_y = (self.cursor_y + 1) % Theme.GRID_ROWS
            print(f"Cursor: ({self.cursor_x}, {self.cursor_y})"
                  + (f" [{note_name(self.midi.note_map.get((self.cursor_x, self.cursor_y), 0))}]"
                     if self._midi_enabled else ""))
        self.prev_button3 = b3

        # Button 2: toggle square at cursor (on press edge)
        if b2 and not self.prev_button2:
            self.button2_press_time = now
            pos = (self.cursor_x, self.cursor_y)
            idx = pos[1] * Theme.GRID_COLS + pos[0] + 2

            beat_x = self.current_beat % Theme.GRID_COLS
            beat_y = self.current_beat // Theme.GRID_COLS
            on_beat = (pos == (beat_x, beat_y))

            if pos in self.activated_squares:
                self.activated_squares.discard(pos)
                print(f"Deactivated {pos}")
                if on_beat:
                    color = self._beat_color(False, self.total_beats == 0)
                    self._set_element(dev, idx, 1, pos[0]*40, pos[1]*40, 40, 40, color)
                else:
                    self._set_element(dev, idx, 0, 0, 0, 40, 40, Colors.BG)
            else:
                self.activated_squares.add(pos)
                print(f"Activated {pos}"
                      + (f" [{note_name(self.midi.note_map.get(pos, 0))}]"
                         if self._midi_enabled else ""))
                color = Colors.ACTIVATED_BEAT if on_beat else Colors.ACTIVATED
                self._set_element(dev, idx, 1, pos[0]*40, pos[1]*40, 40, 40, color)

        elif not b2 and self.prev_button2:
            self.button2_press_time = None

        # Cursor color: learn mode=purple, ready=green, normal=red
        if self._in_learn_mode:
            cursor_color = Colors.CURSOR_LEARN
        elif self.button2_press_time is not None and (now - self.button2_press_time) * 1000 >= Theme.CURSOR_READY_TIME:
            cursor_color = Colors.CURSOR_READY
        else:
            cursor_color = Colors.CURSOR_NORMAL

        cx = self.cursor_x * 40 + 12
        cy = self.cursor_y * 40 + 12
        self._set_element(dev, 11, 1, cx, cy, Theme.CURSOR_SIZE, Theme.CURSOR_SIZE, cursor_color)

        self.prev_button2 = b2
        return True

    def render_beat_change(self, dev, prev_beat: int):
        """Update display and trigger MIDI when beat advances."""
        # Release notes from previous beat position
        px = prev_beat % Theme.GRID_COLS
        py = prev_beat // Theme.GRID_COLS
        prev_pos = (px, py)
        prev_idx = py * Theme.GRID_COLS + px + 2

        if prev_pos in self._beat_notes:
            self.midi.note_off(prev_pos)
            self._beat_notes.discard(prev_pos)
            # If we have envelope generators, trigger release
            gen = self._envelope_gens.get(prev_pos)
            if gen:
                gen.gate_off()

        if prev_pos in self.activated_squares:
            self._set_element(dev, prev_idx, 1, px*40, py*40, 40, 40, Colors.ACTIVATED)
        else:
            self._set_element(dev, prev_idx, 0, 0, 0, 40, 40, Colors.BG)

        # Light up new beat square and trigger MIDI
        bx = self.current_beat % Theme.GRID_COLS
        by = self.current_beat // Theme.GRID_COLS
        beat_pos = (bx, by)
        curr_idx = by * Theme.GRID_COLS + bx + 2
        is_first = (self.total_beats == 0)
        color = self._beat_color(beat_pos in self.activated_squares, is_first)
        self._set_element(dev, curr_idx, 1, bx*40, by*40, 40, 40, color)

        # MIDI: trigger note if square is activated
        if beat_pos in self.activated_squares and self._midi_enabled:
            # Calculate velocity from ADSR envelope
            velocity = 127
            gen = self._envelope_gens.get(beat_pos)
            if gen and self.envelope:
                gen.gate_on()
                velocity = max(1, int(gen.get_value() * 127))
                # For the initial note_on, use full velocity since attack hasn't started yet
                velocity = 127

            self.midi.note_on(beat_pos, velocity=velocity)
            self._beat_notes.add(beat_pos)

    def _redraw_sequencer(self, dev):
        """Redraw the full sequencer screen after leaving editor mode."""
        self._clear_all_elements(dev)

        # Background (layer 1)
        dev.set_screen_element(
            x=0, y=0, width=Theme.SCREEN_WIDTH, height=Theme.SCREEN_HEIGHT,
            color=Colors.BG, element_type=1, element_index=1,
            wait_response=False,
        )

        # Grid lines (layers 12-15)
        self._draw_grid_lines(dev)

        # Redraw activated squares
        for pos in self.activated_squares:
            idx = pos[1] * Theme.GRID_COLS + pos[0] + 2
            beat_x = self.current_beat % Theme.GRID_COLS
            beat_y = self.current_beat // Theme.GRID_COLS
            on_beat = (pos == (beat_x, beat_y))
            color = Colors.ACTIVATED_BEAT if on_beat else Colors.ACTIVATED
            self._set_element(dev, idx, 1, pos[0]*40, pos[1]*40, 40, 40, color)

        # Cursor
        cx = self.cursor_x * 40 + 12
        cy = self.cursor_y * 40 + 12
        self._set_element(dev, 11, 1, cx, cy, Theme.CURSOR_SIZE, Theme.CURSOR_SIZE, Colors.CURSOR_NORMAL)

    def run(self):
        mode_str = "device" if not isinstance(self.input, DebugKeyboardInput) else "debug"
        print("Sequence Gate")
        print(f"  Mode: {self.note_subdivision} ({self.beats_per_bar} beats/bar)")
        print(f"  Input: {mode_str}")

        # Initialize MIDI if enabled
        if self._midi_enabled:
            if self.midi.open():
                print(f"  Note map: {', '.join(f'({k[0]},{k[1]})={note_name(v)}' for k, v in sorted(self.midi.note_map.items()))}")
            else:
                print("  MIDI disabled (failed to open)")
                self._midi_enabled = False

        # Initialize ADSR (always available via knob click)
        self._init_adsr()
        print(f"  ADSR: A={self.envelope.attack_ms}ms D={self.envelope.decay_ms}ms "
              f"S={self.envelope.sustain:.2f} R={self.envelope.release_ms}ms")

        if isinstance(self.input, DebugKeyboardInput):
            print("  Keys: 1/2/3 buttons, k1/k2/k3 knob, +/- BPM, m learn, e ADSR, q quit")
        else:
            print("  Device: btn1=left, btn2=toggle, btn3=right, knob=subdiv, click=ADSR")

        with self.device as dev:
            # Clear all 16 elements for clean state (no stale artifacts)
            self._clear_all_elements(dev)

            # Initial screen setup
            dev.set_screen_element(
                x=0, y=0, width=Theme.SCREEN_WIDTH, height=Theme.SCREEN_HEIGHT,
                color=Colors.BG, element_type=1, element_index=1,
                wait_response=False,
            )

            # Grid lines (layers 12-15)
            self._draw_grid_lines(dev)

            # Initial cursor (layer 11)
            cx = (self.current_beat % Theme.GRID_COLS) * 40 + 12
            cy = (self.current_beat // Theme.GRID_COLS) * 40 + 12
            self._set_element(dev, 11, 1, cx, cy, Theme.CURSOR_SIZE, Theme.CURSOR_SIZE, Colors.CURSOR_NORMAL)

            prev_beat = self.current_beat

            while True:
                inp = self.input.poll()

                if self._in_adsr_mode and self.adsr_editor:
                    # ADSR editor mode
                    result = self.adsr_editor.process_input(inp)
                    if result == 'quit':
                        break
                    elif result == 'exit':
                        self._in_adsr_mode = False
                        print(f"[ADSR] A={self.envelope.attack_ms}ms D={self.envelope.decay_ms}ms "
                              f"S={self.envelope.sustain:.2f} R={self.envelope.release_ms}ms")
                        # Redraw sequencer
                        self._redraw_sequencer(dev)
                    else:
                        self.adsr_editor.render(dev)
                else:
                    # Normal sequencer mode
                    self.update_beat()

                    if not self.process_input(inp, dev):
                        break

                    if self.current_beat != prev_beat:
                        self.render_beat_change(dev, prev_beat)
                        prev_beat = self.current_beat

                sleep(0.01)

        # Cleanup
        if self._midi_enabled:
            self.midi.close()


if __name__ == "__main__":
    use_debug = "--debug" in sys.argv
    use_midi = "--midi" in sys.argv

    # Parse --output flag
    midi_output = ""
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            midi_output = sys.argv[idx + 1]

    # Parse --input flag
    midi_input = ""
    if "--input" in sys.argv:
        idx = sys.argv.index("--input")
        if idx + 1 < len(sys.argv):
            midi_input = sys.argv[idx + 1]

    print("\n" + "=" * 50)
    flags = []
    if use_debug: flags.append("debug")
    if use_midi: flags.append("MIDI")
    mode = f" ({', '.join(flags)})" if flags else " (device mode)"
    print(f"Sequence Gate{mode}")
    print("=" * 50 + "\n")

    gate = SequenceGate(
        bpm=Theme.DEFAULT_BPM,
        use_debug_input=use_debug,
        midi_output=midi_output,
        midi_input=midi_input,
        enable_midi=use_midi,
    )
    try:
        gate.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if hasattr(gate.input, 'stop'):
            gate.input.stop()
        if gate._midi_enabled:
            gate.midi.close()

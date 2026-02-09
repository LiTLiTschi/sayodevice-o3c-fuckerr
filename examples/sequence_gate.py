"""
Sequence Gate — 4x2 step sequencer on SayoDevice O3C with MIDI output + ADSR.

Usage:
    python sequence_gate.py                        # device mode
    python sequence_gate.py --debug                # keyboard debug input
    python sequence_gate.py --midi                 # with MIDI output (default port)
    python sequence_gate.py --midi --output "port" # specific MIDI output port
    python sequence_gate.py --midi --adsr          # with ADSR envelope editor

Controls (device — sequencer mode):
    Button 1:    cursor left
    Button 2:    toggle square (hold 500ms for ready indicator)
    Button 3:    cursor right
    Knob left:   BPM -10
    Knob right:  BPM +10
    Knob click:  enter ADSR editor (if --adsr) / reset screen

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

    def poll(self) -> dict:
        btns = self.device.get_buttons()
        prev = self._prev
        self._prev = btns

        # Knob encoder debounce: a full detent right sends
        # rrp -> lrp -> rrr -> lrr (and mirrored for left).
        # Only register a direction if the opposite is NOT pressed.
        knob_right_edge = btns.knob_right and not prev.knob_right and not btns.knob_left
        knob_left_edge = btns.knob_left and not prev.knob_left and not btns.knob_right

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
    """Renders and edits ADSR envelope parameters on the device screen.

    Uses 4 vertical bars (40px wide each) filling the 160x80 display:
        A (green) | D (yellow) | S (blue) | R (red)

    The selected parameter has a bright bar; others are dimmed.
    Knob adjusts the selected value. Buttons cycle selection.
    """

    PARAM_NAMES = ['attack_ms', 'decay_ms', 'sustain', 'release_ms']
    PARAM_LABELS = ['A', 'D', 'S', 'R']
    BAR_COLORS = [Theme.ADSR_BAR_A, Theme.ADSR_BAR_D, Theme.ADSR_BAR_S, Theme.ADSR_BAR_R]
    BAR_COLORS_DIM = [Theme.ADSR_BAR_A_DIM, Theme.ADSR_BAR_D_DIM, Theme.ADSR_BAR_S_DIM, Theme.ADSR_BAR_R_DIM]

    # Extended params: 4 values + 3 curves (attack_curve, decay_curve, release_curve)
    EXTENDED_NAMES = ['attack_ms', 'decay_ms', 'sustain', 'release_ms',
                      'attack_curve', 'decay_curve', 'release_curve']
    EXTENDED_LABELS = ['A', 'D', 'S', 'R', 'A-curve', 'D-curve', 'R-curve']

    def __init__(self, envelope):
        self.envelope = envelope
        self.selected = 0  # 0-6: A,D,S,R, A-curve, D-curve, R-curve
        self._element_states: dict[int, dict] = {}

    def _param_to_bar_height(self, param_name: str) -> int:
        """Convert parameter value to bar height (0-76px, leaving 4px margin)."""
        val = getattr(self.envelope, param_name)
        if param_name == 'sustain':
            return int(val * 72)
        elif param_name == 'release_ms':
            return int(min(val, 5000) / 5000 * 72)
        else:  # attack_ms, decay_ms
            return int(min(val, 2000) / 2000 * 72)

    def _adjust_value(self, direction: int) -> None:
        """Adjust the selected parameter by one step."""
        from sayodevice.adsr import CurveType

        param = self.EXTENDED_NAMES[self.selected]

        if param == 'sustain':
            new_val = max(0.0, min(1.0, self.envelope.sustain + direction * 0.05))
            self.envelope.sustain = round(new_val, 2)
            print(f"  S = {self.envelope.sustain:.2f}")
        elif param == 'attack_ms':
            new_val = max(0, min(2000, self.envelope.attack_ms + direction * 50))
            self.envelope.attack_ms = new_val
            print(f"  A = {self.envelope.attack_ms:.0f}ms")
        elif param == 'decay_ms':
            new_val = max(0, min(2000, self.envelope.decay_ms + direction * 50))
            self.envelope.decay_ms = new_val
            print(f"  D = {self.envelope.decay_ms:.0f}ms")
        elif param == 'release_ms':
            new_val = max(0, min(5000, self.envelope.release_ms + direction * 100))
            self.envelope.release_ms = new_val
            print(f"  R = {self.envelope.release_ms:.0f}ms")
        elif param.endswith('_curve'):
            curves = list(CurveType)
            current = getattr(self.envelope, param)
            idx = curves.index(current)
            new_idx = (idx + direction) % len(curves)
            setattr(self.envelope, param, curves[new_idx])
            print(f"  {param} = {curves[new_idx].value}")

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
        """Render the ADSR bar diagram on the device display."""
        # Background (layer 0)
        self._set_element(dev, 0, 1, 0, 0, 160, 80, Colors.BG)

        # Editing a curve? Show which ADSR value the curve belongs to
        is_curve_mode = self.selected >= 4
        curve_bar_idx = self.selected - 4 if is_curve_mode else -1
        # Map curve index to bar: 0=A-curve→bar0, 1=D-curve→bar1, 2=R-curve→bar3
        curve_to_bar = {0: 0, 1: 1, 2: 3}

        for i in range(4):
            bar_h = self._param_to_bar_height(self.PARAM_NAMES[i])
            bar_x = i * 40
            bar_y = 76 - bar_h  # bars grow upward from bottom

            # Choose color: bright if selected, dim otherwise
            if is_curve_mode:
                highlight = (curve_to_bar.get(curve_bar_idx) == i)
            else:
                highlight = (self.selected == i)

            color = self.BAR_COLORS[i] if highlight else self.BAR_COLORS_DIM[i]

            # Bar fill (layer 2-5)
            if bar_h > 0:
                self._set_element(dev, i + 2, 1, bar_x + 2, bar_y, 36, bar_h, color)
            else:
                self._set_element(dev, i + 2, 1, bar_x + 2, 74, 36, 2, color)

        # Separator lines between bars (layers 6-8)
        for i in range(3):
            self._set_element(dev, i + 6, 1, (i + 1) * 40 - 1, 0, 2, 80, '#333333')

        # Selection indicator at bottom (layer 9) — small bright rect
        if is_curve_mode:
            bar_idx = curve_to_bar.get(curve_bar_idx, 0)
        else:
            bar_idx = self.selected
        sel_x = bar_idx * 40 + 4
        self._set_element(dev, 9, 1, sel_x, 77, 32, 3, '#FFFFFF')

        # Curve type indicator (layer 10) — show current curve name with color coding
        if is_curve_mode:
            from sayodevice.adsr import CurveType
            param = self.EXTENDED_NAMES[self.selected]
            curve_val = getattr(self.envelope, param)
            # Color-code by curve type: linear=white, exp=orange, log=cyan
            if curve_val == CurveType.LINEAR:
                curve_color = '#FFFFFF'
            elif curve_val == CurveType.EXPONENTIAL:
                curve_color = '#FF8800'
            else:
                curve_color = '#00FFFF'
            # Small indicator dot at top of selected bar
            self._set_element(dev, 10, 1, sel_x + 10, 2, 12, 6, curve_color)
        else:
            self._set_element(dev, 10, 0, 0, 0, 1, 1, Colors.BG)

        # Clear unused layers (11-15)
        for i in range(11, 16):
            self._set_element(dev, i, 0, 0, 0, 1, 1, Colors.BG)

    def process_input(self, inp: dict) -> str | None:
        """Process input while in ADSR editor mode.

        Returns:
            'exit' to leave editor mode, None otherwise.
        """
        if inp.get('command') == 'quit':
            return 'quit'
        if inp.get('command') == 'toggle_adsr':
            return 'exit'

        # Button 2 or knob_click: exit editor
        b2 = inp.get('button2', False)
        if b2:
            return 'exit'

        # Button 1: previous parameter
        b1 = inp.get('button1', False)
        if b1:
            self.selected = (self.selected - 1) % len(self.EXTENDED_NAMES)
            print(f"  ADSR: editing {self.EXTENDED_LABELS[self.selected]}")

        # Button 3: next parameter
        b3 = inp.get('button3', False)
        if b3:
            self.selected = (self.selected + 1) % len(self.EXTENDED_NAMES)
            print(f"  ADSR: editing {self.EXTENDED_LABELS[self.selected]}")

        # Knob: adjust value
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
                 enable_midi: bool = False, enable_adsr: bool = False):
        self.bpm = bpm
        self.ms_between_beats = 30000 / bpm
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
        self.beats_per_bar = 16 if Theme.NOTE_SUBDIVISION == "SIXTEENTH" else 8

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

        # ADSR
        self._adsr_enabled = enable_adsr
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
        self.ms_between_beats = 30000 / self.bpm
        print(f"BPM: {self.bpm}")

    def _beat_color(self, activated: bool, is_first: bool) -> str:
        """Color for a square that currently has the beat on it."""
        if activated:
            return Colors.ACTIVATED_BEAT
        return Colors.BEAT_FIRST if is_first else Colors.BEAT

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
        elif cmd == 'toggle_adsr' and self._adsr_enabled:
            self._in_adsr_mode = True
            self.element_states.clear()
            print("[ADSR] Entering editor")
            return True

        # Knob: BPM control + mode switches
        if inp.get('knob_right'):
            self._set_bpm(self.bpm + 10)
        if inp.get('knob_left'):
            self._set_bpm(self.bpm - 10)
        if inp.get('knob_click'):
            if self._in_learn_mode:
                self.midi.stop_learn()
                self._in_learn_mode = False
            elif self._adsr_enabled:
                self._in_adsr_mode = True
                self.element_states.clear()
                print("[ADSR] Entering editor")
                return True
            else:
                # Reset screen
                print("[knob_click] Reset")
                for i in range(16):
                    dev.set_screen_element(element_index=i, element_type=0, wait_response=False)
                self.element_states.clear()
                self.activated_squares.clear()

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
        self.element_states.clear()

        # Background (layer 1) + clear layer 0
        dev.set_screen_element(
            x=0, y=0, width=Theme.SCREEN_WIDTH, height=Theme.SCREEN_HEIGHT,
            color=Colors.BG, element_type=1, element_index=1,
            wait_response=False,
        )
        dev.set_screen_element(element_index=0, element_type=0, wait_response=False)

        # Grid lines (layers 12-15)
        is_16th = Theme.NOTE_SUBDIVISION == "SIXTEENTH"
        self._set_element(dev, 12, 1, 36, 0, Theme.GRID_WIDTH, 80, Colors.GRID)
        self._set_element(dev, 13, 1, 76, 0, Theme.GRID_WIDTH, 80,
                          Colors.QUARTER if not is_16th else Colors.GRID)
        self._set_element(dev, 14, 1, 116, 0, Theme.GRID_WIDTH, 80, Colors.GRID)
        self._set_element(dev, 15, 1, 0, 36, 160, Theme.GRID_WIDTH,
                          Colors.QUARTER if is_16th else Colors.GRID)

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
        print(f"  Mode: {Theme.NOTE_SUBDIVISION} ({self.beats_per_bar} beats/bar)")
        print(f"  Input: {mode_str}")

        # Initialize MIDI if enabled
        if self._midi_enabled:
            if self.midi.open():
                print(f"  Note map: {', '.join(f'({k[0]},{k[1]})={note_name(v)}' for k, v in sorted(self.midi.note_map.items()))}")
            else:
                print("  MIDI disabled (failed to open)")
                self._midi_enabled = False

        # Initialize ADSR
        if self._adsr_enabled:
            self._init_adsr()
            print(f"  ADSR: A={self.envelope.attack_ms}ms D={self.envelope.decay_ms}ms "
                  f"S={self.envelope.sustain:.2f} R={self.envelope.release_ms}ms")

        if isinstance(self.input, DebugKeyboardInput):
            print("  Keys: 1/2/3 buttons, k1/k2/k3 knob, +/- BPM, m learn, e ADSR, q quit")
        else:
            print("  Device: btn1=left, btn2=toggle, btn3=right, knob=BPM, click=ADSR/reset")

        with self.device as dev:
            # Initial screen setup
            dev.set_screen_element(
                x=0, y=0, width=Theme.SCREEN_WIDTH, height=Theme.SCREEN_HEIGHT,
                color=Colors.BG, element_type=1, element_index=1,
                wait_response=False,
            )
            dev.set_screen_element(element_index=0, element_type=0, wait_response=False)

            # Grid lines (layers 12-15)
            is_16th = Theme.NOTE_SUBDIVISION == "SIXTEENTH"
            self._set_element(dev, 12, 1, 36, 0, Theme.GRID_WIDTH, 80, Colors.GRID)
            self._set_element(dev, 13, 1, 76, 0, Theme.GRID_WIDTH, 80,
                              Colors.QUARTER if not is_16th else Colors.GRID)
            self._set_element(dev, 14, 1, 116, 0, Theme.GRID_WIDTH, 80, Colors.GRID)
            self._set_element(dev, 15, 1, 0, 36, 160, Theme.GRID_WIDTH,
                              Colors.QUARTER if is_16th else Colors.GRID)

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
    use_adsr = "--adsr" in sys.argv

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

    # --adsr implies --midi
    if use_adsr and not use_midi:
        use_midi = True

    print("\n" + "=" * 50)
    flags = []
    if use_debug: flags.append("debug")
    if use_midi: flags.append("MIDI")
    if use_adsr: flags.append("ADSR")
    mode = f" ({', '.join(flags)})" if flags else " (device mode)"
    print(f"Sequence Gate{mode}")
    print("=" * 50 + "\n")

    gate = SequenceGate(
        bpm=Theme.DEFAULT_BPM,
        use_debug_input=use_debug,
        midi_output=midi_output,
        midi_input=midi_input,
        enable_midi=use_midi,
        enable_adsr=use_adsr,
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

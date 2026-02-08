"""
Sequence Gate — 4x2 step sequencer on SayoDevice O3C.

Usage:
    python sequence_gate.py            # real device buttons
    python sequence_gate.py --debug    # keyboard debug input

Controls (device):
    Button 1:    cursor left
    Button 2:    toggle square (hold 500ms for ready indicator)
    Button 3:    cursor right
    Knob left:   BPM -10
    Knob right:  BPM +10
    Knob click:  reset screen

Controls (debug):
    1/2/3:       momentary button press
    1p/2p/3p:    hold button
    1r/2r/3r:    release button
    k1/k2/k3:    knob left/click/right
    +/-:         BPM ±10
    q:           quit
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
    ACTIVATED = _b(Theme.SQUARE_ACTIVATED_COLOR, BG)
    ACTIVATED_BEAT = _b(Theme.SQUARE_ACTIVATED_WITH_BEAT_COLOR, BG)
    BEAT = _b(Theme.BEAT_INDICATOR_COLOR, BG)
    BEAT_FIRST = _b(Theme.BEAT_INDICATOR_FIRST_COLOR, BG)


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

        result = {
            'button1': btns.button1,
            'button2': btns.button2,
            'button3': btns.button3,
            # Knob rotations are transient — fire once per edge
            'knob_left': btns.knob_left and not prev.knob_left,
            'knob_click': btns.knob_click and not prev.knob_click,
            'knob_right': btns.knob_right and not prev.knob_right,
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

        return result

    def stop(self):
        self.running = False


# ============================================================
# Sequence Gate
# ============================================================

class SequenceGate:
    def __init__(self, bpm: int, use_debug_input: bool = False):
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

    def update_beat(self):
        elapsed_ms = (time() - self.beat_time) * 1000
        if elapsed_ms >= self.ms_between_beats:
            self.beat_time = time()
            self.current_beat = (self.current_beat + 1) % 8
            self.total_beats = (self.total_beats + 1) % self.beats_per_bar

    def process_input(self, inp: dict, dev) -> bool:
        """Handle all input. Returns False to quit."""
        # System commands
        cmd = inp.get('command')
        if cmd == 'quit':
            return False
        elif cmd == 'bpm_up':
            self._set_bpm(self.bpm + 10)
        elif cmd == 'bpm_down':
            self._set_bpm(self.bpm - 10)

        # Knob: BPM control + reset
        if inp.get('knob_right'):
            self._set_bpm(self.bpm + 10)
        if inp.get('knob_left'):
            self._set_bpm(self.bpm - 10)
        if inp.get('knob_click'):
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
            print(f"Cursor: ({self.cursor_x}, {self.cursor_y})")
        self.prev_button1 = b1

        # Button 3: cursor right (on press edge)
        if b3 and not self.prev_button3:
            self.cursor_x += 1
            if self.cursor_x >= Theme.GRID_COLS:
                self.cursor_x = 0
                self.cursor_y = (self.cursor_y + 1) % Theme.GRID_ROWS
            print(f"Cursor: ({self.cursor_x}, {self.cursor_y})")
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
                print(f"Activated {pos}")
                color = Colors.ACTIVATED_BEAT if on_beat else Colors.ACTIVATED
                self._set_element(dev, idx, 1, pos[0]*40, pos[1]*40, 40, 40, color)

        elif not b2 and self.prev_button2:
            self.button2_press_time = None

        # Cursor color: green after holding button2 for 500ms, else red
        if self.button2_press_time is not None and (now - self.button2_press_time) * 1000 >= Theme.CURSOR_READY_TIME:
            cursor_color = Colors.CURSOR_READY
        else:
            cursor_color = Colors.CURSOR_NORMAL

        cx = self.cursor_x * 40 + 12
        cy = self.cursor_y * 40 + 12
        self._set_element(dev, 11, 1, cx, cy, Theme.CURSOR_SIZE, Theme.CURSOR_SIZE, cursor_color)

        self.prev_button2 = b2
        return True

    def render_beat_change(self, dev, prev_beat: int):
        """Update display when beat advances."""
        # Clear previous beat square
        px = prev_beat % Theme.GRID_COLS
        py = prev_beat // Theme.GRID_COLS
        prev_idx = py * Theme.GRID_COLS + px + 2
        if (px, py) in self.activated_squares:
            self._set_element(dev, prev_idx, 1, px*40, py*40, 40, 40, Colors.ACTIVATED)
        else:
            self._set_element(dev, prev_idx, 0, 0, 0, 40, 40, Colors.BG)

        # Light up new beat square
        bx = self.current_beat % Theme.GRID_COLS
        by = self.current_beat // Theme.GRID_COLS
        curr_idx = by * Theme.GRID_COLS + bx + 2
        is_first = (self.total_beats == 0)
        color = self._beat_color((bx, by) in self.activated_squares, is_first)
        self._set_element(dev, curr_idx, 1, bx*40, by*40, 40, 40, color)

    def run(self):
        print("Sequence Gate")
        print(f"  Mode: {Theme.NOTE_SUBDIVISION} ({self.beats_per_bar} beats/bar)")
        if isinstance(self.input, DebugKeyboardInput):
            print("  Debug: 1/2/3 buttons, 1p/1r hold/release, k1/k2/k3 knob, +/- BPM, q quit")
        else:
            print("  Device: btn1=left, btn2=toggle, btn3=right, knob=BPM, click=reset")

        with self.device as dev:
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

            # Initial cursor (layer 11)
            cx = (self.current_beat % Theme.GRID_COLS) * 40 + 12
            cy = (self.current_beat // Theme.GRID_COLS) * 40 + 12
            self._set_element(dev, 11, 1, cx, cy, Theme.CURSOR_SIZE, Theme.CURSOR_SIZE, Colors.CURSOR_NORMAL)

            prev_beat = self.current_beat

            while True:
                inp = self.input.poll()
                self.update_beat()

                if not self.process_input(inp, dev):
                    break

                if self.current_beat != prev_beat:
                    self.render_beat_change(dev, prev_beat)
                    prev_beat = self.current_beat

                sleep(0.01)


if __name__ == "__main__":
    use_debug = "--debug" in sys.argv

    print("\n" + "=" * 50)
    print(f"Sequence Gate {'(debug mode)' if use_debug else '(device mode)'}")
    print("=" * 50 + "\n")

    gate = SequenceGate(bpm=Theme.DEFAULT_BPM, use_debug_input=use_debug)
    try:
        gate.start() if hasattr(gate, 'start') else gate.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if hasattr(gate.input, 'stop'):
            gate.input.stop()

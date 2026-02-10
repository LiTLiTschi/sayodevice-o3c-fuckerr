"""
Sequence Gate — 4x2 step sequencer on SayoDevice O3C with MIDI output + ADSR.

Usage:
    python sequence_gate.py                        # device mode
    python sequence_gate.py --debug                # keyboard debug input
    python sequence_gate.py --midi                 # with MIDI output (default port)
    python sequence_gate.py --midi --output "port" # specific MIDI output port
    python sequence_gate.py --midi --verbose-cc    # show CC messages in terminal
    python sequence_gate.py --setup                # interactive config menu

Controls (device — sequencer screen):
    Button 1:    cursor left
    Button 2:    toggle square (hold 500ms for ready indicator)
    Button 3:    cursor right
    Knob left:   subdivision down (quarter → eighth → sixteenth → 32nd)
    Knob right:  subdivision up
    Knob click:  cycle to next screen

Controls (device — StemFX screen):
    Button 1:    toggle Drums stem
    Button 2:    toggle Inst stem
    Button 3:    toggle Voc stem
    Knob click:  cycle to next screen

Controls (device — ADSR editor screen):
    Button 1:    select previous parameter (A/D/S/R/curves)
    Button 2:    toggle property (time/value ↔ curve type)
    Button 3:    select next parameter
    Knob left:   decrease selected value
    Knob right:  increase selected value
    Knob click:  cycle to next screen

Controls (device — MIDI learn mode, sequencer only):
    Knob click (while in learn): exit learn mode
    In learn mode:
        Button 1/3: move cursor to grid position
        Play MIDI note: assign note to cursor position

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

MIDI CC output for Bome MIDI Translator Pro (channel 15 by default):
    CC 102  Beat position (0-7)              0-126 (step 18)
    CC 103  BPM (30-300)                     0-127 (linear)
    CC 104  Subdivision                      0/42/85/127
    CC 105-112  Grid steps 0-7              0=off, 127=on
    CC 113  Attack time (0-2000ms)           0-127
    CC 114  Decay time (0-2000ms)            0-127
    CC 115  Sustain level (0-100%)           0-127
    CC 116  Release time (0-5000ms)          0-127
    CC 117  Attack curve (LIN/EXP/LOG)       0/63/127
    CC 118  Decay curve                      0/63/127
    CC 119  Release curve                    0/63/127

    Use --cc-channel N to change from default channel 15.

StemFX MIDI output (configurable via --setup):
    CC 20  Drums toggle                      0=off, 127=on
    CC 21  Inst toggle                       0=off, 127=on
    CC 22  Voc toggle                        0=off, 127=on

Config:
    ~/.sayodevice/sequence_gate.json  — persistent settings (screens, MIDI, stems)
    Use --setup to edit interactively.
"""

import json
import sys
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from time import sleep, time

import sayodevice


# ============================================================
# CONFIG SYSTEM
# ============================================================

CONFIG_DIR = Path.home() / ".sayodevice"
CONFIG_FILE = CONFIG_DIR / "sequence_gate.json"

DEFAULT_CONFIG = {
    "screens": ["sequencer", "stemfx", "adsr"],
    "midi_output": "",
    "midi_input": "",
    "cc_channel": 15,
    "verbose_cc": False,
    "stemfx": {
        "stems": [
            {"name": "Drums", "color": "#0066FF", "dim": "#001133", "cc": 20},
            {"name": "Inst",  "color": "#FF3300", "dim": "#330A00", "cc": 21},
            {"name": "Voc",   "color": "#00FF00", "dim": "#003300", "cc": 22},
        ],
        "channel": 15,
    },
    "sequencer": {
        "bpm": 120,
        "subdivision": "SIXTEENTH",
    },
}


def load_config() -> dict:
    """Load config from JSON file, merged with defaults for missing keys."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            # Shallow merge: top-level defaults filled in
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            # Deep merge nested dicts
            for key in ('stemfx', 'sequencer'):
                if key in cfg and isinstance(cfg[key], dict):
                    d = dict(DEFAULT_CONFIG[key])
                    d.update(cfg[key])
                    merged[key] = d
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    """Save config to JSON file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
    print(f"  Saved to {CONFIG_FILE}")


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

    # Per-subdivision beat colors (normal / first-beat-of-bar)
    BEAT_COLORS = {
        "QUARTER":      ("#FFFFFFCC", "#FFFFFFEE"),       # white
        "EIGHTH":       ("#66FF66FF", "#AAFFAAFF"),       # green
        "SIXTEENTH":    ("#6BDAFFFF", "#16FFEFFF"),       # cyan (default)
        "THIRTY_SECOND": ("#FF66FFFF", "#FFAAFFFF"),      # magenta
    }

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

    # Per-subdivision beat colors {subdivision: (normal, first)}
    BEAT_BY_SUBDIV = {
        k: (Theme.blend_colors(v[0], Theme.BACKGROUND_COLOR),
            Theme.blend_colors(v[1], Theme.BACKGROUND_COLOR))
        for k, v in Theme.BEAT_COLORS.items()
    }


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

class _LockedDev:
    """Thread-safe proxy for SayoDevice — serializes HID access."""

    def __init__(self, dev, lock: threading.Lock):
        self._dev = dev
        self._lock = lock

    def set_screen_element(self, **kw):
        with self._lock:
            self._dev.set_screen_element(**kw)


class InputHandler(ABC):
    @abstractmethod
    def poll(self) -> dict:
        """Returns dict with button1..3, knob_left/click/right, command."""
        pass


class DeviceInput(InputHandler):
    """Real SAYO device input via threaded USB poller.

    A dedicated thread polls get_buttons() as fast as USB allows (~140Hz).
    Knob edges are accumulated into an integer counter so that fast turning
    never loses detents — even when the main loop is busy rendering.
    """

    KNOB_COOLDOWN_SEC = 0.025  # 25ms debounce per detent (bounce settles in 5-20ms)

    def __init__(self, device: sayodevice.SayoDevice,
                 dev_lock: threading.Lock | None = None):
        self.device = device
        self.dev_lock = dev_lock or threading.Lock()
        self._buttons = sayodevice.ButtonState()  # latest snapshot from thread
        self._knob_accum = 0  # +N = right, -N = left
        self._lock = threading.Lock()  # protects _buttons and _knob_accum
        self._prev_main = sayodevice.ButtonState()  # for button edge detection in poll()
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self):
        """Fast USB polling thread — detects knob edges and accumulates them."""
        prev = sayodevice.ButtonState()
        cooldown = 0.0
        while self._running:
            with self.dev_lock:
                try:
                    btns = self.device.get_buttons()
                except OSError:
                    sleep(0.005)
                    continue
            now = time()
            if now >= cooldown:
                if btns.knob_right and not prev.knob_right:
                    with self._lock:
                        self._knob_accum += 1
                    cooldown = now + self.KNOB_COOLDOWN_SEC
                elif btns.knob_left and not prev.knob_left:
                    with self._lock:
                        self._knob_accum -= 1
                    cooldown = now + self.KNOB_COOLDOWN_SEC
            with self._lock:
                self._buttons = btns
            prev = btns

    def poll(self) -> dict:
        with self._lock:
            btns = self._buttons
            accum = self._knob_accum
            self._knob_accum = 0
        prev = self._prev_main
        self._prev_main = btns
        return {
            'button1': btns.button1,
            'button2': btns.button2,
            'button3': btns.button3,
            'knob_left': accum < 0,
            'knob_left_count': abs(accum) if accum < 0 else 0,
            'knob_click': btns.knob_click and not prev.knob_click,
            'knob_right': accum > 0,
            'knob_right_count': accum if accum > 0 else 0,
            'command': None,
        }

    def stop(self):
        self._running = False
        self._thread.join(timeout=1.0)


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

        kl = 'k1' in raw
        kr = 'k3' in raw
        result = {
            'button1': self.held['button1'],
            'button2': self.held['button2'],
            'button3': self.held['button3'],
            'knob_left': kl,
            'knob_left_count': 1 if kl else 0,
            'knob_click': 'k2' in raw,
            'knob_right': kr,
            'knob_right_count': 1 if kr else 0,
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
            'quit' to quit app, None otherwise.
            (Screen cycling is handled by main loop via knob_click.)
        """
        if inp.get('command') == 'quit':
            return 'quit'
        if inp.get('command') == 'toggle_adsr':
            return 'quit'

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

        # Knob: adjust selected property (supports multiple steps per poll)
        for _ in range(inp.get('knob_right_count', 0)):
            self._adjust_value(1)
        for _ in range(inp.get('knob_left_count', 0)):
            self._adjust_value(-1)

        return None


# ============================================================
# StemFX Screen — Rekordbox 3-stem toggle
# ============================================================

class StemFXScreen:
    """3-stem toggle display for Rekordbox StemFX control.

    Shows 3 colored bars on the device screen. Buttons toggle each stem.
    Active stems show bright color, inactive show dim color.
    Sends MIDI CC per toggle.
    """

    BAR_WIDTH = 50
    GAP = 5  # (50*3 + 5*2 = 160)

    def __init__(self, config: dict, cc_output):
        stemfx_cfg = config.get('stemfx', DEFAULT_CONFIG['stemfx'])
        stems_cfg = stemfx_cfg.get('stems', DEFAULT_CONFIG['stemfx']['stems'])
        self.stems = []
        for s in stems_cfg:
            self.stems.append({
                'name': s['name'],
                'color': s['color'],
                'dim': s['dim'],
                'cc': s['cc'],
                'active': True,
            })
        self.channel = stemfx_cfg.get('channel', 15)
        self.cc_output = cc_output
        self._prev_buttons = [False, False, False]
        self._element_states: dict[int, dict] = {}

    def enter(self):
        """Called when switching to this screen."""
        self._prev_buttons = [False, False, False]
        self._element_states.clear()

    def _set_element(self, dev, index: int, element_type: int,
                     x: int, y: int, width: int, height: int, color: str):
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
        """Draw 3 colored bars — bright=active, dim=inactive."""
        # Layer 0: background
        self._set_element(dev, 0, 1, 0, 0, 160, 80, Colors.BG)

        # Layers 1-3: stem bars
        for i, stem in enumerate(self.stems):
            x = i * (self.BAR_WIDTH + self.GAP)
            color = stem['color'] if stem['active'] else stem['dim']
            self._set_element(dev, i + 1, 1, x, 0, self.BAR_WIDTH, 80, color)

        # Clear remaining layers
        for j in range(len(self.stems) + 1, 16):
            self._set_element(dev, j, 0, 0, 0, 1, 1, Colors.BG)

    def process_input(self, inp: dict) -> str | None:
        """Button 1/2/3 toggle stems. Returns 'quit' to exit app."""
        if inp.get('command') == 'quit':
            return 'quit'

        buttons = [inp.get('button1', False),
                   inp.get('button2', False),
                   inp.get('button3', False)]

        for i, (btn, prev) in enumerate(zip(buttons, self._prev_buttons)):
            if btn and not prev and i < len(self.stems):
                stem = self.stems[i]
                stem['active'] = not stem['active']
                state_str = "ON" if stem['active'] else "OFF"
                print(f"  [StemFX] {stem['name']}: {state_str}")
                self.cc_output.send_stem(stem['cc'], stem['active'], self.channel)

        self._prev_buttons = list(buttons)
        return None

    def send_initial_state(self):
        """Send MIDI CC for all stems (call on startup)."""
        for stem in self.stems:
            self.cc_output.send_stem(stem['cc'], stem['active'], self.channel)


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
# MIDI CC Output for Bome MIDI Translator Pro
# ============================================================

class CCOutput:
    """Broadcasts sequencer/ADSR state as MIDI CC on a dedicated channel.

    Uses CC 102-119 ("undefined" in MIDI spec). Only sends when a value
    actually changes. Shares the same mido output port as SequencerMidi.
    """

    CC_BEAT = 102
    CC_BPM = 103
    CC_SUBDIVISION = 104
    CC_GRID_BASE = 105       # 105-112 for grid steps 0-7
    CC_ATTACK = 113
    CC_DECAY = 114
    CC_SUSTAIN = 115
    CC_RELEASE = 116
    CC_ATTACK_CURVE = 117
    CC_DECAY_CURVE = 118
    CC_RELEASE_CURVE = 119

    _CURVE_CC = {"linear": 0, "exponential": 63, "logarithmic": 127}

    _CC_NAMES = {
        102: "Beat", 103: "BPM", 104: "Subdiv",
        105: "Step0", 106: "Step1", 107: "Step2", 108: "Step3",
        109: "Step4", 110: "Step5", 111: "Step6", 112: "Step7",
        113: "AtkTime", 114: "DecTime", 115: "Sustain", 116: "RelTime",
        117: "AtkCurve", 118: "DecCurve", 119: "RelCurve",
    }

    def __init__(self, channel: int = 15, verbose: bool = False):
        self.channel = channel
        self.verbose = verbose
        self._port = None
        self._prev: dict[int, int] = {}

    def attach(self, port) -> None:
        """Attach a mido output port (shared with SequencerMidi)."""
        self._port = port

    def _send(self, cc: int, value: int, force: bool = False) -> None:
        value = max(0, min(127, value))
        if not force and self._prev.get(cc) == value:
            return
        if self._port:
            import mido
            self._port.send(mido.Message(
                'control_change', control=cc, value=value, channel=self.channel))
        if self.verbose:
            name = self._CC_NAMES.get(cc, f"CC{cc}")
            print(f"  [CC] ch{self.channel} {name}={value}")
        self._prev[cc] = value

    def send_beat(self, beat: int, force: bool = False) -> None:
        self._send(self.CC_BEAT, beat * 18, force)

    def send_bpm(self, bpm: int, force: bool = False) -> None:
        self._send(self.CC_BPM, int((bpm - 30) / 270 * 127), force)

    def send_subdivision(self, subdivision: str, force: bool = False) -> None:
        idx = SUBDIVISIONS.index(subdivision)
        self._send(self.CC_SUBDIVISION, min(127, idx * 42), force)

    def send_grid_step(self, step: int, active: bool, force: bool = False) -> None:
        self._send(self.CC_GRID_BASE + step, 127 if active else 0, force)

    def send_grid_all(self, activated_squares: set, force: bool = False) -> None:
        for i in range(8):
            pos = (i % 4, i // 4)
            self.send_grid_step(i, pos in activated_squares, force)

    def send_adsr(self, envelope, force: bool = False) -> None:
        self._send(self.CC_ATTACK, int(min(envelope.attack_ms, 2000) / 2000 * 127), force)
        self._send(self.CC_DECAY, int(min(envelope.decay_ms, 2000) / 2000 * 127), force)
        self._send(self.CC_SUSTAIN, int(envelope.sustain * 127), force)
        self._send(self.CC_RELEASE, int(min(envelope.release_ms, 5000) / 5000 * 127), force)
        self._send(self.CC_ATTACK_CURVE, self._CURVE_CC.get(envelope.attack_curve.value, 0), force)
        self._send(self.CC_DECAY_CURVE, self._CURVE_CC.get(envelope.decay_curve.value, 0), force)
        self._send(self.CC_RELEASE_CURVE, self._CURVE_CC.get(envelope.release_curve.value, 0), force)

    def send_stem(self, cc: int, on: bool, channel: int | None = None) -> None:
        """Send stem toggle as MIDI CC."""
        ch = channel if channel is not None else self.channel
        value = 127 if on else 0
        if self._port:
            import mido
            self._port.send(mido.Message(
                'control_change', control=cc, value=value, channel=ch))
        if self.verbose:
            print(f"  [CC] ch{ch} Stem(CC{cc})={'ON' if on else 'OFF'}")

    def dump_all(self, beat, bpm, subdivision, activated_squares, envelope) -> None:
        """Force-send all state (call on startup)."""
        self.send_beat(beat, force=True)
        self.send_bpm(bpm, force=True)
        self.send_subdivision(subdivision, force=True)
        self.send_grid_all(activated_squares, force=True)
        self.send_adsr(envelope, force=True)


# ============================================================
# Sequence Gate
# ============================================================

class SequenceGate:
    def __init__(self, config: dict | None = None,
                 use_debug_input: bool = False,
                 midi_output: str = "", midi_input: str = "",
                 enable_midi: bool = False, cc_channel: int = 15,
                 verbose_cc: bool = False):
        # Config: CLI flags override config file values
        cfg = config or load_config()
        if not midi_output:
            midi_output = cfg.get('midi_output', '')
        if not midi_input:
            midi_input = cfg.get('midi_input', '')
        if cc_channel == 15:  # default → use config
            cc_channel = cfg.get('cc_channel', 15)
        if not verbose_cc:
            verbose_cc = cfg.get('verbose_cc', False)

        seq_cfg = cfg.get('sequencer', {})
        bpm = seq_cfg.get('bpm', Theme.DEFAULT_BPM)
        subdivision = seq_cfg.get('subdivision', Theme.NOTE_SUBDIVISION)
        if subdivision not in SUBDIVISIONS:
            subdivision = Theme.NOTE_SUBDIVISION

        self.bpm = bpm
        self.note_subdivision = subdivision
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

        # Input (device lock serializes HID access between poller thread + main thread)
        self._dev_lock = threading.Lock()
        if use_debug_input:
            self.input: InputHandler = DebugKeyboardInput()
        else:
            self.input = DeviceInput(self.device, self._dev_lock)

        # MIDI
        self.midi = SequencerMidi(output_port=midi_output, input_port=midi_input)
        self._midi_enabled = enable_midi
        self.cc = CCOutput(channel=cc_channel, verbose=verbose_cc)

        # Screen cycling
        self._screens = cfg.get('screens', ['sequencer', 'stemfx', 'adsr'])
        # Ensure sequencer is always present
        if 'sequencer' not in self._screens:
            self._screens.insert(0, 'sequencer')
        self._screen_idx = 0  # start on sequencer

        # StemFX
        self.stemfx = StemFXScreen(cfg, self.cc)

        # ADSR (always available)
        self.adsr_editor: ADSREditor | None = None
        self._in_learn_mode = False

        # Envelope generators per grid position
        self._envelope_gens: dict[tuple[int, int], object] = {}

        # Track which squares had notes triggered on them (for note_off on beat departure)
        self._beat_notes: set[tuple[int, int]] = set()

    @property
    def _current_screen(self) -> str:
        return self._screens[self._screen_idx]

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
        self.stemfx._element_states.clear()

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
        if self._midi_enabled:
            self.cc.send_bpm(self.bpm)

    def _beat_color(self, activated: bool, is_first: bool) -> str:
        """Color for a square that currently has the beat on it."""
        if activated:
            return Colors.ACTIVATED_BEAT
        normal, first = Colors.BEAT_BY_SUBDIV[self.note_subdivision]
        return first if is_first else normal

    def _draw_grid_lines(self, dev):
        """Draw grid lines (layers 12-15) with subdivision-aware quarter markers."""
        sub = self.note_subdivision
        q = Colors.BEAT_BY_SUBDIV[sub][0]
        g = Colors.GRID

        if sub == "QUARTER":
            v1, v2, v3, h = q, q, q, g
        elif sub == "EIGHTH":
            v1, v2, v3, h = g, q, g, q
        elif sub == "SIXTEENTH":
            v1, v2, v3, h = g, g, g, q
        else:  # THIRTY_SECOND
            v1, v2, v3, h = g, g, g, g

        self._set_element(dev, 12, 1, 36, 0, Theme.GRID_WIDTH, 80, v1)
        self._set_element(dev, 13, 1, 76, 0, Theme.GRID_WIDTH, 80, v2)
        self._set_element(dev, 14, 1, 116, 0, Theme.GRID_WIDTH, 80, v3)
        self._set_element(dev, 15, 1, 0, 36, 160, Theme.GRID_WIDTH, h)

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
        if self._midi_enabled:
            self.cc.send_subdivision(self.note_subdivision)

    def _midi_learn_note(self, note: int, channel: int) -> None:
        """Callback for MIDI learn — assign note to current cursor position."""
        pos = (self.cursor_x, self.cursor_y)
        old_note = self.midi.note_map.get(pos)
        self.midi.note_map[pos] = note
        print(f"[MIDI Learn] ({pos[0]},{pos[1]}) = {note_name(note)} "
              f"(was {note_name(old_note) if old_note else 'none'})")

    def _next_screen(self, dev):
        """Cycle to the next screen."""
        self._clear_all_elements(dev)
        self._screen_idx = (self._screen_idx + 1) % len(self._screens)
        name = self._current_screen
        print(f"[Screen] → {name}")

        if name == 'sequencer':
            self._redraw_sequencer(dev)
        elif name == 'adsr':
            if self.adsr_editor:
                self.adsr_editor.enter()
                print(f"  ADSR: {self.adsr_editor.current_stage} "
                      f"[{self.adsr_editor.PROP_LABELS[self.adsr_editor.current_prop]}]")
        elif name == 'stemfx':
            self.stemfx.enter()

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
            # Debug shortcut: jump to ADSR screen
            self._clear_all_elements(dev)
            try:
                self._screen_idx = self._screens.index('adsr')
            except ValueError:
                return True
            if self.adsr_editor:
                self.adsr_editor.enter()
            print("[Screen] → adsr")
            return True

        # Knob: subdivision control (supports multi-step)
        for _ in range(inp.get('knob_right_count', 0)):
            self._set_subdivision(1, dev)
        for _ in range(inp.get('knob_left_count', 0)):
            self._set_subdivision(-1, dev)

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

            if self._midi_enabled:
                step = pos[1] * Theme.GRID_COLS + pos[0]
                self.cc.send_grid_step(step, pos in self.activated_squares)

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

    def _beat_midi(self, prev_beat: int):
        """Handle MIDI note off/on when beat advances (runs regardless of screen)."""
        # Release notes from previous beat position
        px = prev_beat % Theme.GRID_COLS
        py = prev_beat // Theme.GRID_COLS
        prev_pos = (px, py)

        if prev_pos in self._beat_notes:
            self.midi.note_off(prev_pos)
            self._beat_notes.discard(prev_pos)
            gen = self._envelope_gens.get(prev_pos)
            if gen:
                gen.gate_off()

        # Send beat CC
        if self._midi_enabled:
            self.cc.send_beat(self.current_beat)

        # Trigger notes for new beat position
        bx = self.current_beat % Theme.GRID_COLS
        by = self.current_beat // Theme.GRID_COLS
        beat_pos = (bx, by)

        if beat_pos in self.activated_squares and self._midi_enabled:
            velocity = 127
            gen = self._envelope_gens.get(beat_pos)
            if gen and self.envelope:
                gen.gate_on()
                velocity = 127
            self.midi.note_on(beat_pos, velocity=velocity)
            self._beat_notes.add(beat_pos)

    def render_beat_change(self, dev, prev_beat: int):
        """Update sequencer display when beat advances (only when sequencer is visible)."""
        px = prev_beat % Theme.GRID_COLS
        py = prev_beat // Theme.GRID_COLS
        prev_pos = (px, py)
        prev_idx = py * Theme.GRID_COLS + px + 2

        if prev_pos in self.activated_squares:
            self._set_element(dev, prev_idx, 1, px*40, py*40, 40, 40, Colors.ACTIVATED)
        else:
            self._set_element(dev, prev_idx, 0, 0, 0, 40, 40, Colors.BG)

        # Light up new beat square
        bx = self.current_beat % Theme.GRID_COLS
        by = self.current_beat // Theme.GRID_COLS
        beat_pos = (bx, by)
        curr_idx = by * Theme.GRID_COLS + bx + 2
        is_first = (self.total_beats == 0)
        color = self._beat_color(beat_pos in self.activated_squares, is_first)
        self._set_element(dev, curr_idx, 1, bx*40, by*40, 40, 40, color)

    def _redraw_sequencer(self, dev):
        """Redraw the full sequencer screen after leaving another screen."""
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
        print(f"  Screens: {' → '.join(self._screens)}")
        print(f"  Mode: {self.note_subdivision} ({self.beats_per_bar} beats/bar)")
        print(f"  Input: {mode_str}")

        # Initialize MIDI if enabled
        if self._midi_enabled:
            if self.midi.open():
                print(f"  Note map: {', '.join(f'({k[0]},{k[1]})={note_name(v)}' for k, v in sorted(self.midi.note_map.items()))}")
                self.cc.attach(self.midi._midi_out)
            else:
                print("  MIDI disabled (failed to open)")
                self._midi_enabled = False

        # Initialize ADSR (always available via screen cycling)
        self._init_adsr()
        print(f"  ADSR: A={self.envelope.attack_ms}ms D={self.envelope.decay_ms}ms "
              f"S={self.envelope.sustain:.2f} R={self.envelope.release_ms}ms")

        # Send initial CC state dump
        if self._midi_enabled:
            self.cc.dump_all(self.current_beat, self.bpm, self.note_subdivision,
                             self.activated_squares, self.envelope)
            self.stemfx.send_initial_state()
            print(f"  CC output: channel {self.cc.channel}, CC 102-119")

        if isinstance(self.input, DebugKeyboardInput):
            print("  Keys: 1/2/3 buttons, k1/k2/k3 knob, +/- BPM, m learn, e ADSR, q quit")
        else:
            print("  Device: btn1/2/3 = context, knob = adjust, click = next screen")

        with self.device as raw_dev:
            dev = _LockedDev(raw_dev, self._dev_lock)

            # Clear all 16 elements for clean state (no stale artifacts)
            self._clear_all_elements(dev)

            # Initial screen setup (sequencer)
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

                # Beat tracking always runs (keeps sequencer in sync across all screens)
                self.update_beat()

                # Handle knob click: screen cycling (consumed before dispatch)
                if inp.get('knob_click'):
                    screen = self._current_screen
                    if screen == 'sequencer' and self._in_learn_mode:
                        # In learn mode, knob click exits learn mode instead
                        self.midi.stop_learn()
                        self._in_learn_mode = False
                    else:
                        self._next_screen(dev)
                        # Force beat sync after screen switch
                        prev_beat = self.current_beat
                    inp = dict(inp, knob_click=False)

                # Dispatch to active screen
                screen = self._current_screen

                if screen == 'sequencer':
                    if not self.process_input(inp, dev):
                        break
                    if self.current_beat != prev_beat:
                        self._beat_midi(prev_beat)
                        self.render_beat_change(dev, prev_beat)
                        prev_beat = self.current_beat

                elif screen == 'adsr' and self.adsr_editor:
                    result = self.adsr_editor.process_input(inp)
                    if result == 'quit':
                        break
                    if self._midi_enabled:
                        self.cc.send_adsr(self.envelope)
                    self.adsr_editor.render(dev)
                    # Background beat MIDI (no display update)
                    if self.current_beat != prev_beat:
                        self._beat_midi(prev_beat)
                        prev_beat = self.current_beat

                elif screen == 'stemfx':
                    result = self.stemfx.process_input(inp)
                    if result == 'quit':
                        break
                    self.stemfx.render(dev)
                    # Background beat MIDI (no display update)
                    if self.current_beat != prev_beat:
                        self._beat_midi(prev_beat)
                        prev_beat = self.current_beat

                sleep(0.002)

        # Cleanup
        if self._midi_enabled:
            self.midi.close()


# ============================================================
# Interactive Setup Menu
# ============================================================

def _list_midi_ports(direction: str) -> list[str]:
    """List available MIDI ports. Returns empty list if mido not available."""
    try:
        import mido
        if direction == 'output':
            return list(mido.get_output_names())
        else:
            return list(mido.get_input_names())
    except ImportError:
        return []


def _setup_midi_port(cfg: dict, key: str, direction: str) -> None:
    """Interactive MIDI port selection."""
    ports = _list_midi_ports(direction)
    current = cfg.get(key, '')

    print(f"\n  Available {direction} ports:")
    if ports:
        for i, p in enumerate(ports):
            marker = " ←" if p == current else ""
            print(f"    {i + 1}. {p}{marker}")
    else:
        print("    (none found — is mido installed?)")
    print(f"    0. Auto (first available)")
    print(f"    c. Clear (empty)")

    choice = input("  Select: ").strip()
    if choice == '0':
        cfg[key] = ""
    elif choice.lower() == 'c':
        cfg[key] = ""
    elif choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(ports):
            cfg[key] = ports[idx]
            print(f"  → {ports[idx]}")
    else:
        # Treat as literal port name
        if choice:
            cfg[key] = choice


def _setup_screens(cfg: dict) -> None:
    """Configure which screens are active and their order."""
    available = ['sequencer', 'stemfx', 'adsr']
    current = cfg.get('screens', DEFAULT_CONFIG['screens'])
    print(f"\n  Current order: {' → '.join(current)}")
    print(f"  Available: {', '.join(available)}")
    print(f"  (sequencer is always included)")
    screens_str = input("  Enter screens (comma-separated): ").strip()
    if screens_str:
        parts = [s.strip().lower() for s in screens_str.split(',')]
        valid = [s for s in parts if s in available]
        if 'sequencer' not in valid:
            valid.insert(0, 'sequencer')
        if valid:
            cfg['screens'] = valid
            print(f"  → {' → '.join(valid)}")


def _setup_stemfx(cfg: dict) -> None:
    """Configure StemFX stems."""
    stemfx = cfg.get('stemfx', dict(DEFAULT_CONFIG['stemfx']))
    stems = stemfx.get('stems', DEFAULT_CONFIG['stemfx']['stems'])

    print(f"\n  StemFX Channel: {stemfx.get('channel', 15)}")
    for i, s in enumerate(stems):
        print(f"    {i + 1}. {s['name']}: CC {s['cc']}, color {s['color']}, dim {s['dim']}")

    ch = input(f"  New channel (0-15) [{stemfx.get('channel', 15)}]: ").strip()
    if ch and ch.isdigit():
        stemfx['channel'] = max(0, min(15, int(ch)))

    for i, s in enumerate(stems):
        cc_str = input(f"  {s['name']} CC [{s['cc']}]: ").strip()
        if cc_str and cc_str.isdigit():
            s['cc'] = int(cc_str)
        color_str = input(f"  {s['name']} color [{s['color']}]: ").strip()
        if color_str.startswith('#') and len(color_str) in (7, 9):
            s['color'] = color_str
        dim_str = input(f"  {s['name']} dim color [{s['dim']}]: ").strip()
        if dim_str.startswith('#') and len(dim_str) in (7, 9):
            s['dim'] = dim_str

    stemfx['stems'] = stems
    cfg['stemfx'] = stemfx


def _setup_sequencer(cfg: dict) -> None:
    """Configure sequencer defaults."""
    seq = cfg.get('sequencer', dict(DEFAULT_CONFIG['sequencer']))

    bpm_str = input(f"  BPM (30-300) [{seq.get('bpm', 120)}]: ").strip()
    if bpm_str and bpm_str.isdigit():
        seq['bpm'] = max(30, min(300, int(bpm_str)))

    print(f"  Subdivisions: {', '.join(SUBDIVISIONS)}")
    sub = input(f"  Subdivision [{seq.get('subdivision', 'SIXTEENTH')}]: ").strip().upper()
    if sub in SUBDIVISIONS:
        seq['subdivision'] = sub

    cfg['sequencer'] = seq


def run_setup() -> dict | None:
    """Interactive terminal setup. Returns config dict to run, or None to quit."""
    cfg = load_config()

    while True:
        print("\n" + "=" * 40)
        print("  Sequence Gate Setup")
        print("=" * 40)
        print(f"  Config: {CONFIG_FILE}\n")

        print(f"  1. MIDI Output Port:  {cfg.get('midi_output') or '(auto)'}")
        print(f"  2. MIDI Input Port:   {cfg.get('midi_input') or '(auto)'}")
        print(f"  3. CC Channel:        {cfg.get('cc_channel', 15)}")
        print(f"  4. Verbose CC:        {'yes' if cfg.get('verbose_cc') else 'no'}")
        print(f"  5. Screens:           {' → '.join(cfg.get('screens', []))}")
        print(f"  6. StemFX config")
        print(f"  7. Sequencer defaults")
        print()
        print("  s. Save config")
        print("  r. Save & Run")
        print("  q. Quit")

        choice = input("\nChoice: ").strip().lower()

        if choice == '1':
            _setup_midi_port(cfg, 'midi_output', 'output')
        elif choice == '2':
            _setup_midi_port(cfg, 'midi_input', 'input')
        elif choice == '3':
            ch = input(f"  CC Channel (0-15) [{cfg.get('cc_channel', 15)}]: ").strip()
            if ch and ch.isdigit():
                cfg['cc_channel'] = max(0, min(15, int(ch)))
        elif choice == '4':
            cfg['verbose_cc'] = not cfg.get('verbose_cc', False)
            print(f"  → {'yes' if cfg['verbose_cc'] else 'no'}")
        elif choice == '5':
            _setup_screens(cfg)
        elif choice == '6':
            _setup_stemfx(cfg)
        elif choice == '7':
            _setup_sequencer(cfg)
        elif choice == 's':
            save_config(cfg)
        elif choice == 'r':
            save_config(cfg)
            return cfg
        elif choice == 'q':
            return None

    return None


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    # --setup: interactive config menu
    if "--setup" in sys.argv:
        result = run_setup()
        if result is None:
            sys.exit(0)
        # Run with config from setup
        use_midi = "--midi" in sys.argv or True  # setup implies MIDI
        gate = SequenceGate(
            config=result,
            use_debug_input="--debug" in sys.argv,
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
        sys.exit(0)

    # Normal CLI mode
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

    # Parse --cc-channel flag (MIDI CC output channel, default 15)
    cc_channel = 15
    if "--cc-channel" in sys.argv:
        idx = sys.argv.index("--cc-channel")
        if idx + 1 < len(sys.argv):
            cc_channel = int(sys.argv[idx + 1])

    verbose_cc = "--verbose-cc" in sys.argv

    print("\n" + "=" * 50)
    flags = []
    if use_debug: flags.append("debug")
    if use_midi: flags.append("MIDI")
    if verbose_cc: flags.append("verbose-cc")
    mode = f" ({', '.join(flags)})" if flags else " (device mode)"
    print(f"Sequence Gate{mode}")
    print("=" * 50 + "\n")

    gate = SequenceGate(
        use_debug_input=use_debug,
        midi_output=midi_output,
        midi_input=midi_input,
        enable_midi=use_midi,
        cc_channel=cc_channel,
        verbose_cc=verbose_cc,
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

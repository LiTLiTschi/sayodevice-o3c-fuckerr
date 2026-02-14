#!/usr/bin/env python3
"""
SayoDevice O3C → MIDI — press / release / hold actions + MIDI-IN visual feedback

Architecture:
  • SayoDevice buttons  →  MIDI OUT  (press / release / hold)
  • MIDI IN              →  SayoDevice screen elements & button LEDs  (feedback rules)

Button input is read directly from the SayoDevice via USB HID (no virtual Xbox).
Each button can send different MIDI on press, release, and hold.
Visual feedback is 100 % driven by incoming MIDI messages.
"""

import os
import re
import ast
import sys
import copy
import msvcrt
import shlex
import argparse
import operator
import subprocess
import threading
import time
from pathlib import Path

import mido
import yaml
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from InquirerPy.separator import Separator

try:
    from sayodevice import SayoDevice, DeviceListener
except ImportError:
    SayoDevice = None  # type: ignore[misc,assignment]
    DeviceListener = None  # type: ignore[misc,assignment]

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "sayo_midi_config.yaml"

# SayoDevice physical inputs — these are the config key names
BUTTON_NAMES = {
    "button1":    "Button 1 (left)",
    "button2":    "Button 2 (middle)",
    "button3":    "Button 3 (right)",
    "knob_click": "Knob click",
    "knob_left":  "Knob left",
    "knob_right": "Knob right",
}


# ═══════════════════════════════════════════════════════════════
#  Validators & tiny helpers
# ═══════════════════════════════════════════════════════════════

def _vint(mn, mx):
    """Return a validator for integer text in [mn, mx]."""
    return lambda v: v.isdigit() and mn <= int(v) <= mx


def _vcolor(v):
    """Validate a #RRGGBB hex color string or a $var expression."""
    if '$' in v:
        return v.startswith('#') or v.startswith('$')
    return len(v) == 7 and v[0] == "#" and all(c in "0123456789ABCDEFabcdef" for c in v[1:])


# Mutable hint storage — set by config editor to show bank vars in prompts
_bank_vars_hint: dict = {}
_bank_scenes_hint: dict = {}

def _ask_int(msg, default, mn, mx):
    return int(inquirer.text(message=msg, default=str(default), validate=_vint(mn, mx)).execute())


def _ask_int_or_var(msg, default, mn, mx):
    """Ask for an integer or a $var / ${expr}. Returns int or string."""
    comp = {}
    for k, v in _bank_vars_hint.items():
        comp[f"${k}"] = None
    # Add ${expr} patterns for common math
    var_names = list(_bank_vars_hint.keys())
    if var_names:
        comp["${" + var_names[0] + "}"] = None
        if len(var_names) >= 2:
            comp["${" + var_names[0] + " + " + var_names[1] + "}"] = None
    def _validate(v):
        if '$' in v:
            return True
        return v.lstrip('-').isdigit() and mn <= int(v) <= mx
    if _bank_vars_hint:
        var_list = "  ".join(f"${k}={v}" for k, v in _bank_vars_hint.items())
        print(f"  Vars: {var_list}")
        print(f"  Syntax: $var | ${{var + var}} | ${{var * 2 + 5}}  Ops: + - * / // %")
    result = inquirer.text(
        message=msg,
        default=str(default),
        validate=_validate,
        completer=comp if comp else None,
    ).execute()
    if '$' in result:
        return result
    return int(result)


def _color_completer():
    """Build a completer dict for color input with $var names and ${expr}."""
    comp = {}
    for k, v in _bank_vars_hint.items():
        comp[f"${k}"] = None
    # Common color presets
    comp["#FF0000"] = None
    comp["#00FF00"] = None
    comp["#0000FF"] = None
    comp["#FFFFFF"] = None
    comp["#000000"] = None
    comp["#FFFF00"] = None
    comp["#FF00FF"] = None
    comp["#00FFFF"] = None
    # Build common $var color patterns
    var_names = list(_bank_vars_hint.keys())
    if var_names:
        for v in var_names:
            comp[f"#${v}"] = None
        # ${expr} patterns for composing colors from vars
        comp["${" + var_names[0] + "}"] = None
        if len(var_names) >= 2:
            comp["#${" + var_names[0] + "}${" + var_names[1] + "}"] = None
    return comp


def _ask_color(msg, default="#FF0000"):
    comp = _color_completer()
    if _bank_vars_hint:
        var_list = "  ".join(f"${k}={v}" for k, v in _bank_vars_hint.items())
        print(f"  Vars: {var_list}")
        print(f"  Syntax: $var | ${{var}} | #${{r}}${{g}}${{b}} | #$var$var  Ops: + - * / // %")
    result = inquirer.text(
        message=msg,
        default=default,
        validate=_vcolor,
        completer=comp if comp else None,
    ).execute()
    # Only uppercase pure hex colors, not $var expressions
    if '$' not in result:
        result = result.upper()
    return result


# ═══════════════════════════════════════════════════════════════
#  Bank variables — $var and ${expr} resolution
# ═══════════════════════════════════════════════════════════════

_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}


def _safe_math_eval(expr_str):
    """Safely evaluate a simple arithmetic expression (no builtins, only +-*/%)."""
    try:
        tree = ast.parse(str(expr_str).strip(), mode="eval")
    except SyntaxError:
        return expr_str

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp):
            op = _SAFE_OPS.get(type(node.op))
            if not op:
                raise ValueError
            return op(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -_eval(node.operand)
        raise ValueError

    try:
        result = _eval(tree)
        return int(result) if isinstance(result, float) and result == int(result) else result
    except (ValueError, TypeError):
        return expr_str


def _resolve_vars(obj, vars_dict):
    """Recursively resolve $var and ${expr} references in a config structure.

    - ``$varname``  →  simple value substitution
    - ``${expr}``   →  arithmetic expression (vars available by name)
    - Fully-numeric results are auto-converted to int/float.
    """
    if not vars_dict:
        return obj

    if isinstance(obj, str):
        result = obj

        # 1. ${expr} — arithmetic expressions with var substitution
        def _eval_expr(m):
            expr = m.group(1).strip()
            for k, v in sorted(vars_dict.items(), key=lambda x: -len(x[0])):
                expr = expr.replace(k, str(v))
            val = _safe_math_eval(expr)
            return str(val)

        result = re.sub(r"\$\{([^}]+)}", _eval_expr, result)

        # 2. $varname — simple substitution (longest names first)
        def _subst(m):
            name = m.group(1)
            return str(vars_dict[name]) if name in vars_dict else m.group(0)

        result = re.sub(r"\$([a-zA-Z_]\w*)", _subst, result)

        # 3. Auto-convert to number if the whole string became numeric
        if result != obj:
            try:
                return int(result)
            except ValueError:
                try:
                    f = float(result)
                    return int(f) if f == int(f) else f
                except ValueError:
                    pass
        return result

    if isinstance(obj, dict):
        return {k: _resolve_vars(v, vars_dict) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_vars(item, vars_dict) for item in obj]
    return obj


def _lerp_color(c1, c2, t):
    """Linearly interpolate between two #RRGGBB colors.  t in [0..1]."""
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02X}{g:02X}{b:02X}"


def _lerp_int(a, b, t):
    """Linearly interpolate between two ints.  t in [0..1]."""
    return int(a + (b - a) * t)


def _resolve_dynamic(cfg, msg, layout=None):
    """Resolve dynamic properties on a feedback config dict.

    Returns a NEW dict with dynamic values replaced by concrete values
    computed from the MIDI message velocity / CC value.
    The original dict is not mutated.
    """
    dyn = cfg.get("dynamic")
    if not dyn:
        return cfg  # nothing dynamic, return as-is

    resolved = dict(cfg)  # shallow copy
    for prop, spec in dyn.items():
        source = spec.get("from", "velocity")
        if source == "velocity" and hasattr(msg, "velocity"):
            raw = msg.velocity  # 0-127
        elif source == "value" and hasattr(msg, "value"):
            raw = msg.value  # 0-127
        else:
            continue
        t = raw / 127.0

        lo = spec.get("min")
        hi = spec.get("max")
        if lo is None or hi is None:
            continue

        if isinstance(lo, str) and lo.startswith("#"):
            # Color interpolation
            resolved[prop] = _lerp_color(lo, hi, t)
        else:
            # Numeric interpolation
            resolved[prop] = _lerp_int(int(lo), int(hi), t)

    return resolved


# ═══════════════════════════════════════════════════════════════
#  Formatting helpers (used in view, menus, summaries)
# ═══════════════════════════════════════════════════════════════

def _fmt_action(cfg):
    """Format a single MIDI action dict to a compact string."""
    t = cfg.get("type", "?")
    ch = cfg.get("channel", 0)
    if t in ("note", "note_on"):
        return f"note_on {cfg.get('note','?')} ch{ch} vel{cfg.get('velocity',127)}"
    if t == "note_off":
        return f"note_off {cfg.get('note','?')} ch{ch}"
    if t == "cc":
        return f"CC {cfg.get('cc','?')}={cfg.get('value',127)} ch{ch}"
    if t == "program_change":
        return f"PC {cfg.get('program','?')} ch{ch}"
    if t in ("chord", "chord_off"):
        return f"{t} {cfg.get('notes',[])} ch{ch}"
    if t == "bank_switch":
        return f"bank→{cfg.get('bank','?')}"
    return t


def _fmt_button(cfg):
    """Format a button mapping (simple or multi) to a short string."""
    if any(k in cfg for k in ("on_press", "on_release", "on_hold")):
        parts = []
        for key, sym in [("on_press", "▶"), ("on_release", "⏹"), ("on_hold", "⏸"), ("on_hold_release", "⏏")]:
            if key in cfg:
                parts.append(f"{sym}{_fmt_action(cfg[key])}")
        return "multi  " + " | ".join(parts)
    return _fmt_action(cfg)


def _fmt_match(match):
    """Format a feedback match to a compact string."""
    t = match.get("type", "?")
    ch = match.get("channel")
    ch_s = f"ch{ch}" if ch is not None else "ch*"
    if t in ("note_on", "note_off"):
        n = match.get("note")
        return f"{t:9s} {ch_s} N{n if n is not None else '*'}"
    if t == "cc":
        cc = match.get("cc")
        v = match.get("value")
        s = f"{'cc':9s} {ch_s} CC{cc if cc is not None else '*'}"
        if v is not None:
            s += f"={v}"
        return s
    if t == "program_change":
        p = match.get("program")
        return f"{'pc':9s} {ch_s} P{p if p is not None else '*'}"
    if t == "raw":
        pat = match.get("hex", "?")
        val_idx = match.get("value_byte")
        extra = f" val@{val_idx}" if val_idx is not None else ""
        return f"{'raw':9s} {pat}{extra}"
    return t


def _fmt_feedback_actions(rule):
    """Format a feedback rule's screen + led actions."""
    parts = []
    sc = rule.get("screen")
    if sc:
        for s in (sc if isinstance(sc, list) else [sc]):
            dyn = s.get("dynamic", {})
            extras = []
            for prop, spec in dyn.items():
                extras.append(f"{prop}={spec.get('min')}→{spec.get('max')}({spec.get('from','vel')})")
            vmap = s.get("value_map")
            if vmap:
                extras.append(f"map({len(vmap)} vals)")
            dyn_s = " " + " ".join(extras) if extras else ""
            parts.append(f"🖥️[{s.get('element_index','?')}] {s.get('color','?')}{dyn_s}")
    ld = rule.get("led")
    if ld:
        for l in (ld if isinstance(ld, list) else [ld]):
            dyn = l.get("dynamic", {})
            extras = []
            for prop, spec in dyn.items():
                extras.append(f"{prop}={spec.get('min')}→{spec.get('max')}({spec.get('from','vel')})")
            vmap = l.get("value_map")
            if vmap:
                extras.append(f"map({len(vmap)} vals)")
            dyn_s = " " + " ".join(extras) if extras else ""
            parts.append(f"💡[{l.get('key_index','?')}] {l.get('color','?')}{dyn_s}")
    return "  ".join(parts) if parts else "(no actions)"


class _RawValueMsg:
    """Thin wrapper that injects a .velocity and .value from a raw byte,
    so _resolve_dynamic can treat raw hex matches the same as note/CC."""

    def __init__(self, original_msg, value_byte):
        self._msg = original_msg
        self.velocity = value_byte  # 0-127 (or 0-255, clamped in _resolve)
        self.value = value_byte

    def __getattr__(self, name):
        return getattr(self._msg, name)

    def bytes(self):
        return self._msg.bytes()


def _apply_value_map(cfg, msg, bank_vars):
    """If cfg has a value_map, look up the MIDI value and override properties.

    value_map entries can be:
      - A color string: "#FF0000" or "#$var1$var2$var3"
      - A scene reference: "scene:scene_name"
      - A dict of property overrides: {color: "#FF0000", width: 50}
    Supports bank var references in all values.
    Falls through to the base color if no key matches.
    """
    vmap = cfg.get("value_map")
    if not vmap:
        return cfg
    # Get the value from the message
    val = None
    if hasattr(msg, "velocity"):
        val = msg.velocity
    elif hasattr(msg, "value"):
        val = msg.value
    if val is None:
        return cfg
    # Look up: try exact int key, then string key
    mapped = vmap.get(val) or vmap.get(str(val))
    if mapped is None:
        return cfg
    resolved = dict(cfg)
    # Resolve scene reference
    if isinstance(mapped, str) and mapped.startswith("scene:"):
        scene_name = mapped[6:]
        # bank_vars is from the current bank; we need scenes from config
        # Scenes are stored as _scenes_cache on the SayoMIDI instance,
        # but here we receive them embedded in bank_vars under __scenes__
        scenes = bank_vars.get("__scenes__", {})
        scene = scenes.get(scene_name, {})
        if scene:
            scene_resolved = _resolve_vars(scene, bank_vars) if bank_vars else scene
            resolved.update(scene_resolved)
        return resolved
    # mapped can be a simple color string or a dict of overrides
    if isinstance(mapped, str):
        resolved["color"] = _resolve_vars(mapped, bank_vars) if bank_vars else mapped
    elif isinstance(mapped, dict):
        mapped_resolved = _resolve_vars(mapped, bank_vars) if bank_vars else mapped
        resolved.update(mapped_resolved)
    return resolved


# ═══════════════════════════════════════════════════════════════
#  SayoMIDI — the runtime class
# ═══════════════════════════════════════════════════════════════

class SayoMIDI:
    """SayoDevice O3C → MIDI OUT, with MIDI IN → SayoDevice feedback."""

    def __init__(self, config_path, verbose=False):
        self.config_path = config_path
        self.verbose = verbose
        self.config = self._load_config()
        self.outport = None
        self.inport = None
        self.running = False
        self.current_bank = self.config.get("default_bank", "default")
        self.listener = None   # DeviceListener for input  (set up in run())

        # Knob fix
        self.last_button_press_time = {}
        self.knob_cooldown = {}

        # Hold detection
        self.hold_timers = {}
        self.hold_triggered = {}
        self.button_press_ts = {}

        # SayoDevice
        self._sayo_lock = threading.Lock()
        self.sayo = None
        try:
            if SayoDevice:
                self.sayo = SayoDevice.open()
        except Exception as e:
            print(f"⚠️  SayoDevice not available: {e}")

        # Cache screen layout for feedback rules (element_index → pos/size)
        self._screen_layout: dict = {}
        self._build_screen_layout()

    # ── helpers ─────────────────────────────────────────────

    def _load_config(self):
        if not self.config_path.exists():
            print(f"❌ Config not found: {self.config_path}")
            print("Run with --setup to create one.")
            sys.exit(1)
        with open(self.config_path, "r") as f:
            return yaml.safe_load(f)

    def reload_config(self):
        """Reload config from disk and apply safe hot-reload changes.

        Returns a list of warnings (e.g. port changes needing restart).
        """
        old_out = self.config.get("midi_out_port") or self.config.get("midi_port")
        old_in = self.config.get("midi_in_port")

        self.config = self._load_config()
        self.current_bank = self.config.get("default_bank", self.current_bank)
        self._build_screen_layout()
        self.apply_screen_elements()

        warnings = []
        new_out = self.config.get("midi_out_port") or self.config.get("midi_port")
        new_in = self.config.get("midi_in_port")
        if new_out != old_out:
            warnings.append("MIDI OUT port changed → restart required")
        if new_in != old_in:
            warnings.append("MIDI IN port changed → restart required")
        return warnings

    def stop(self):
        """Signal the engine to stop."""
        self.running = False

    def _build_screen_layout(self):
        se = self.config.get("screen_elements", {})
        if not se.get("enabled"):
            return
        bank_vars = self._get_bank_vars()
        for idx_str, elem in se.get("elements", {}).items():
            resolved = _resolve_vars(elem, bank_vars)
            self._screen_layout[int(idx_str)] = {
                "x": resolved.get("x", 0),
                "y": resolved.get("y", 0),
                "width": resolved.get("width", 40),
                "height": resolved.get("height", 40),
                "element_type": resolved.get("element_type", 1),
            }

    # ── connection ──────────────────────────────────────────

    def connect_device(self):
        """Ensure SayoDevice is open (used for both input and visuals)."""
        if self.sayo:
            return True
        if not SayoDevice:
            print("❌ sayodevice library not installed!")
            return False
        try:
            self.sayo = SayoDevice.open()
            print(f"✅ SayoDevice connected")
            return True
        except Exception as e:
            print(f"❌ SayoDevice error: {e}")
            return False

    def connect_midi_out(self):
        port = self.config.get("midi_out_port") or self.config.get("midi_port")
        if not port:
            print("❌ No MIDI OUT port configured!")
            return False
        try:
            self.outport = mido.open_output(port)  # pyright: ignore[reportAttributeAccessIssue]
            print(f"🎹 MIDI OUT: {port}")
            return True
        except Exception as e:
            print(f"❌ MIDI OUT error: {e}")
            print("Available:", mido.get_output_names())  # pyright: ignore[reportAttributeAccessIssue]
            return False

    def connect_midi_in(self):
        port = self.config.get("midi_in_port")
        if not port:
            print("ℹ️  No MIDI IN port — feedback disabled")
            return True
        out_port = self.config.get("midi_out_port") or self.config.get("midi_port")
        if port == out_port:
            print(f"⚠️  WARNING: MIDI IN and OUT use the same port '{port}'!")
            print("   This causes a feedback loop — the script will receive its own output.")
            print("   Use separate ports (e.g. Virtual In for OUT, Virtual Out for IN).")
        try:
            self.inport = mido.open_input(port, callback=self._on_midi_in)  # pyright: ignore[reportAttributeAccessIssue]
            print(f"📥 MIDI IN:  {port}")
            return True
        except Exception as e:
            print(f"⚠️  MIDI IN error: {e}")
            print("Available:", mido.get_input_names())  # pyright: ignore[reportAttributeAccessIssue]
            return True  # non-fatal

    # ── startup visuals ─────────────────────────────────────

    def _get_bank_vars(self):
        """Return vars dict from the current bank, with __scenes__ injected."""
        banks = self.config.get("banks", {})
        bank = banks.get(self.current_bank, {})
        v = dict(bank.get("vars", {}))
        scenes = bank.get("scenes", {})
        if scenes:
            v["__scenes__"] = scenes
        return v

    def apply_screen_elements(self):
        if not self.sayo:
            return
        se = self.config.get("screen_elements", {})
        if not se.get("enabled"):
            return
        elements = se.get("elements", {})
        if not elements:
            return
        bank_vars = self._get_bank_vars()
        self._screen_layout.clear()
        try:
            with self._sayo_lock:
                for idx_str, elem in elements.items():
                    resolved = _resolve_vars(elem, bank_vars)
                    idx = int(idx_str)
                    self._screen_layout[idx] = {
                        "x": resolved.get("x", 0),
                        "y": resolved.get("y", 0),
                        "width": resolved.get("width", 40),
                        "height": resolved.get("height", 40),
                        "element_type": resolved.get("element_type", 1),
                    }
                    self.sayo.set_screen_element(
                        x=resolved.get("x", 0),
                        y=resolved.get("y", 0),
                        width=resolved.get("width", 40),
                        height=resolved.get("height", 40),
                        color=resolved.get("color", "#FFFFFF"),
                        element_type=resolved.get("element_type", 1),
                        element_index=idx,
                        refresh=False,
                        wait_response=False,
                    )
                self.sayo.refresh_display()
            print(f"✅ Screen elements applied ({len(elements)})")
        except Exception as e:
            print(f"⚠️  Screen error: {e}")

    # ── MIDI OUT — button / knob ───────────────────────────

    def get_current_mappings(self):
        banks = self.config.get("banks", {})
        return banks.get(self.current_bank, {"buttons": {}})

    def switch_bank(self, name):
        if name in self.config.get("banks", {}):
            self.current_bank = name
            print(f"\n🔄 Bank: {name}")
            self.apply_screen_elements()
        else:
            print(f"❌ Bank '{name}' not found")

    def _send_midi(self, msg):
        """Send a MIDI message and print with raw hex."""
        if not self.outport:
            return
        self.outport.send(msg)
        raw = " ".join(f"{b:02X}" for b in msg.bytes())
        print(f"  → {msg}  [{raw}]")

    # ── fire a single action dict ───────────────────────────

    def _fire_action(self, action_config, label=""):
        t = action_config.get("type")
        if not t:
            return
        if t == "bank_switch":
            self.switch_bank(action_config.get("bank", "default"))
            return
        if not self.outport:
            return
        ch = action_config.get("channel", 0)
        if t in ("note", "note_on"):
            self._send_midi(mido.Message("note_on", note=action_config["note"],
                                         velocity=action_config.get("velocity", 127), channel=ch))
        elif t == "note_off":
            self._send_midi(mido.Message("note_off", note=action_config["note"], channel=ch))
        elif t == "cc":
            self._send_midi(mido.Message("control_change", control=action_config["cc"],
                                         value=action_config.get("value", 127), channel=ch))
        elif t == "program_change":
            self._send_midi(mido.Message("program_change", program=action_config["program"], channel=ch))
        elif t == "chord":
            for n in action_config.get("notes", []):
                self._send_midi(mido.Message("note_on", note=n,
                                             velocity=action_config.get("velocity", 127), channel=ch))
        elif t == "chord_off":
            for n in action_config.get("notes", []):
                self._send_midi(mido.Message("note_off", note=n, channel=ch))

    # ── hold timer ──────────────────────────────────────────

    def _on_hold_timer(self, button_id, midi_config):
        self.hold_triggered[button_id] = True
        if self.verbose:
            print(f"  ⏱️  HOLD {button_id}")
        if "on_hold" in midi_config:
            self._fire_action(midi_config["on_hold"], "[HOLD] ")

    # ── multi-action dispatch ───────────────────────────────

    def _handle_multi_action(self, button_id, cfg, pressed):
        hold_ms = cfg.get("hold_time_ms", 500)
        if pressed:
            self.button_press_ts[button_id] = time.time()
            if "on_press" in cfg:
                self._fire_action(cfg["on_press"], "[PRESS] ")
            if "on_hold" in cfg:
                self.hold_triggered[button_id] = False
                if button_id in self.hold_timers:
                    self.hold_timers[button_id].cancel()
                t = threading.Timer(hold_ms / 1000.0, self._on_hold_timer,
                                    args=[button_id, cfg])
                t.daemon = True
                t.start()
                self.hold_timers[button_id] = t
        else:
            if button_id in self.hold_timers:
                self.hold_timers[button_id].cancel()
                del self.hold_timers[button_id]
            held = self.hold_triggered.get(button_id, False)
            if held and "on_hold_release" in cfg:
                self._fire_action(cfg["on_hold_release"], "[HOLD-REL] ")
            elif "on_release" in cfg:
                self._fire_action(cfg["on_release"], "[RELEASE] ")
            self.hold_triggered.pop(button_id, None)
            self.button_press_ts.pop(button_id, None)

    # ── simple (legacy) button ──────────────────────────────

    def _handle_simple_button(self, button_id, cfg, pressed):
        t = cfg.get("type")
        if not t:
            return
        if t == "bank_switch":
            if pressed:
                self.switch_bank(cfg.get("bank", "default"))
            return
        ch = cfg.get("channel", 0)
        if t == "note":
            if pressed:
                self._send_midi(mido.Message("note_on", note=cfg["note"],
                                             velocity=cfg.get("velocity", 127), channel=ch))
            else:
                self._send_midi(mido.Message("note_off", note=cfg["note"], channel=ch))
        elif t == "cc":
            if pressed:
                self._send_midi(mido.Message("control_change", control=cfg["cc"],
                                             value=cfg.get("value", 127), channel=ch))
        elif t == "program_change":
            if pressed:
                self._send_midi(mido.Message("program_change", program=cfg["program"], channel=ch))
        elif t == "chord":
            if pressed:
                for n in cfg.get("notes", []):
                    self._send_midi(mido.Message("note_on", note=n,
                                                 velocity=cfg.get("velocity", 127), channel=ch))
            else:
                for n in cfg.get("notes", []):
                    self._send_midi(mido.Message("note_off", note=n, channel=ch))

    # ── button dispatch ─────────────────────────────────────

    def handle_button(self, code, pressed):
        if self._is_knob_button(code):
            if self._handle_knob(code, pressed):
                return
        mappings = self.get_current_mappings()
        cfg = mappings.get("buttons", {}).get(code)
        if not cfg:
            if self.verbose:
                print(f"  ⚠️  {code} unmapped")
            return
        if any(k in cfg for k in ("on_press", "on_release", "on_hold")):
            self._handle_multi_action(code, cfg, pressed)
        else:
            self._handle_simple_button(code, cfg, pressed)

    # ── knob fix ────────────────────────────────────────────

    def _is_knob_button(self, code):
        kf = self.config.get("knob_fix", {})
        if not kf.get("enabled"):
            return False
        return code in (kf.get("left_button"), kf.get("right_button"))

    def _handle_knob(self, code, pressed):
        kf = self.config.get("knob_fix", {})
        if not kf.get("enabled"):
            return False
        left = kf.get("left_button")
        right = kf.get("right_button")
        debounce = kf.get("debounce_ms", 50) / 1000.0
        now = time.time()
        if not pressed:
            return True
        ck = f"{left}_{right}"
        if ck in self.knob_cooldown and now - self.knob_cooldown[ck] < 0.1:
            return True
        other = right if code == left else left
        if other in self.last_button_press_time:
            if now - self.last_button_press_time[other] < debounce:
                if kf.get("test_mode"):
                    print(f"⏭️  DEBOUNCED {code}")
                return True
        self.last_button_press_time[code] = now
        direction = "left" if code == left else "right"
        midi_cfg = kf.get(f"{direction}_midi")
        if kf.get("test_mode"):
            print(f"✅ KNOB {direction.upper()}")
        else:
            print(f"🔄 Knob {direction}")
        if midi_cfg and self.outport:
            ch = midi_cfg.get("channel", 0)
            if midi_cfg.get("type") == "note":
                self._send_midi(mido.Message("note_on", note=midi_cfg["note"],
                                             velocity=midi_cfg.get("velocity", 127), channel=ch))
                time.sleep(0.05)
                self._send_midi(mido.Message("note_off", note=midi_cfg["note"], channel=ch))
            elif midi_cfg.get("type") == "cc":
                self._send_midi(mido.Message("control_change", control=midi_cfg["cc"],
                                             value=midi_cfg.get("value", 127), channel=ch))
        self.knob_cooldown[ck] = now
        return True

    # ── MIDI IN feedback ────────────────────────────────────

    def _on_midi_in(self, msg):
        """Callback for incoming MIDI — runs in mido's background thread."""
        if not self.running:
            return
        if self.verbose:
            raw = " ".join(f"{b:02X}" for b in msg.bytes())
            print(f"  📥 IN: {msg}  [{raw}]")
        rules = self.config.get("midi_feedback", [])
        if not rules:
            return
        matching = [r for r in rules if self._match_rule(r.get("match", {}), msg)]
        if matching:
            self._apply_feedback(matching, msg)

    def _match_rule(self, match, msg):
        rtype = match.get("type")
        # raw hex match
        if rtype == "raw":
            raw_bytes = msg.bytes()
            pat = match.get("hex", "")
            tokens = pat.split()
            if len(tokens) > len(raw_bytes):
                return False
            for i, tok in enumerate(tokens):
                if tok.lower() in ("xx", "**"):
                    continue  # wildcard byte
                try:
                    expected = int(tok, 16)
                except ValueError:
                    return False
                if raw_bytes[i] != expected:
                    return False
            return True
        # note_on (velocity > 0)
        if rtype == "note_on" and msg.type == "note_on" and msg.velocity > 0:
            if match.get("note") is not None and msg.note != match["note"]:
                return False
            if match.get("channel") is not None and msg.channel != match["channel"]:
                return False
            return True
        # note_off  (or note_on vel=0)
        if rtype == "note_off":
            is_off = msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)
            if not is_off:
                return False
            if match.get("note") is not None and msg.note != match["note"]:
                return False
            if match.get("channel") is not None and msg.channel != match["channel"]:
                return False
            return True
        # cc
        if rtype == "cc" and msg.type == "control_change":
            if match.get("cc") is not None and msg.control != match["cc"]:
                return False
            if match.get("channel") is not None and msg.channel != match["channel"]:
                return False
            if match.get("value") is not None and msg.value != match["value"]:
                return False
            return True
        # program_change
        if rtype == "program_change" and msg.type == "program_change":
            if match.get("program") is not None and msg.program != match["program"]:
                return False
            if match.get("channel") is not None and msg.channel != match["channel"]:
                return False
            return True
        return False

    def _apply_feedback(self, rules, msg):
        """Batch-apply screen + LED actions from all matched rules."""
        screen_updates = {}  # element_index → resolved screen config
        led_updates = {}  # key_index → resolved led config

        # Resolve bank vars once
        bank_vars = self._get_bank_vars()

        # For raw matches, inject a synthetic .velocity attribute from value_byte
        raw_bytes = msg.bytes()

        for rule in rules:
            match = rule.get("match", {})
            # Wrap msg to inject value from raw byte position if needed
            eff_msg = msg
            if match.get("type") == "raw" and match.get("value_byte") is not None:
                vb_idx = match["value_byte"]
                if vb_idx < len(raw_bytes):
                    eff_msg = _RawValueMsg(msg, raw_bytes[vb_idx])

            sc = rule.get("screen")
            if sc:
                for s in (sc if isinstance(sc, list) else [sc]):
                    s_resolved = _resolve_vars(s, bank_vars)
                    s_resolved = _apply_value_map(s_resolved, eff_msg, bank_vars)
                    idx = s_resolved.get("element_index", 0)
                    layout = self._screen_layout.get(idx, {})
                    screen_updates[idx] = _resolve_dynamic(s_resolved, eff_msg, layout)
            ld = rule.get("led")
            if ld:
                for l in (ld if isinstance(ld, list) else [ld]):
                    l_resolved = _resolve_vars(l, bank_vars)
                    l_resolved = _apply_value_map(l_resolved, eff_msg, bank_vars)
                    led_updates[l_resolved.get("key_index", 0)] = _resolve_dynamic(l_resolved, eff_msg)

        if not self.sayo:
            # Still print for debugging
            for idx, s in screen_updates.items():
                print(f"  🖥️  [{idx}] → {s.get('color','?')}  (no SayoDevice)")
            for ki, l in led_updates.items():
                print(f"  💡 [{ki}] → {l.get('color','?')}  (no SayoDevice)")
            return

        try:
            with self._sayo_lock:
                # Screen updates (batch, one refresh)
                if screen_updates:
                    for idx, s in screen_updates.items():
                        layout = self._screen_layout.get(idx, {})
                        self.sayo.set_screen_element(
                            x=s.get("x", layout.get("x", 0)),
                            y=s.get("y", layout.get("y", 0)),
                            width=s.get("width", layout.get("width", 40)),
                            height=s.get("height", layout.get("height", 40)),
                            color=s.get("color", "#FFFFFF"),
                            element_type=s.get("element_type", layout.get("element_type", 1)),
                            element_index=idx,
                            refresh=False,
                            wait_response=False,
                        )
                    self.sayo.refresh_display()
                    for idx, s in screen_updates.items():
                        print(f"  🖥️  [{idx}] → {s.get('color','?')}")

                # LED updates
                for ki, l in led_updates.items():
                    self.sayo.set_key_light(
                        color=l.get("color", "#FFFFFF"),
                        brightness=100,
                        key_index=ki,
                    )
                    print(f"  💡 [{ki}] → {l.get('color','?')}")
        except Exception as e:
            print(f"  ⚠️  Feedback error: {e}")

    # ── main loop ───────────────────────────────────────────

    def run(self):
        if not self.connect_device():
            return
        if not self.connect_midi_out():
            return
        self.connect_midi_in()

        kf = self.config.get("knob_fix", {})
        fb_count = len(self.config.get("midi_feedback", []))

        print(f"\n🎮 SayoDevice-to-MIDI v2 running...")
        print(f"📦 Bank: {self.current_bank}")
        print(f"🔧 Knob fix: {'ON' if kf.get('enabled') else 'OFF'}")
        print(f"📥 Feedback rules: {fb_count}")

        self.apply_screen_elements()

        if self.verbose:
            print("🔍 VERBOSE mode ON")
        print("Ctrl+C to quit.\n")
        self.running = True

        # Set up DeviceListener for button input
        self.listener = DeviceListener(self.sayo, poll_interval_ms=10)
        self.listener.on_button(self._on_sayo_button)
        self.listener.start()

        try:
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n\n👋 Shutting down...")
            for t in self.hold_timers.values():
                t.cancel()
            self.hold_timers.clear()
        finally:
            self.running = False
            if self.listener:
                self.listener.stop()
            if self.outport:
                self.outport.close()
            if self.inport:
                self.inport.close()

    def _on_sayo_button(self, event):
        """Called by DeviceListener when a SayoDevice button state changes."""
        btn_name = event.button
        if btn_name not in BUTTON_NAMES:
            if self.verbose:
                print(f"  └─ Unknown sayo button: {btn_name}")
            return

        if self.verbose:
            ts = time.strftime("%H:%M:%S")
            act = "PRESS" if event.pressed else "RELEASE"
            friendly = BUTTON_NAMES.get(btn_name, btn_name)
            print(f"  [{ts}] {act}: {btn_name} ({friendly})")

        self.handle_button(btn_name, event.pressed)


# ═══════════════════════════════════════════════════════════════
#  Config editor — ask helpers
# ═══════════════════════════════════════════════════════════════

def _ask_midi_action(prompt_label):
    """Interactively configure a single MIDI action.

    Returns a config dict or None if skipped.
    """
    try:
        action_type = inquirer.select(
            message=f"▶ {prompt_label} — MIDI type",
            choices=[
                Choice("note_on", "🎵 Note ON (momentary)"),
                Choice("note_off", "🎵 Note OFF"),
                Choice("cc", "🎛️  CC (control change)"),
                Choice("program_change", "📝 Program Change"),
                Choice("bank_switch", "🔄 Bank Switch"),
                Choice("skip", "⏭️  Skip (no MIDI)"),
            ],
        ).execute()
    except KeyboardInterrupt:
        return None

    if action_type == "skip":
        return None

    cfg = {"type": action_type}

    try:
        if action_type in ("note_on", "note_off"):
            cfg["note"] = _ask_int("MIDI note (0-127)", 60, 0, 127)
            if action_type == "note_on":
                cfg["velocity"] = _ask_int("Velocity (0-127)", 127, 0, 127)
            cfg["channel"] = _ask_int("MIDI channel (0-15)", 0, 0, 15)

        elif action_type == "cc":
            cfg["cc"] = _ask_int("CC number (0-127)", 1, 0, 127)
            cfg["value"] = _ask_int("CC value (0-127)", 127, 0, 127)
            cfg["channel"] = _ask_int("MIDI channel (0-15)", 0, 0, 15)

        elif action_type == "program_change":
            cfg["program"] = _ask_int("Program (0-127)", 0, 0, 127)
            cfg["channel"] = _ask_int("MIDI channel (0-15)", 0, 0, 15)

        elif action_type == "bank_switch":
            cfg["bank"] = inquirer.text(message="Target bank name").execute()

    except KeyboardInterrupt:
        return None

    return cfg


def _detect_button():
    """Listen for a SayoDevice button press and return its name."""
    if not SayoDevice:
        print("❌ sayodevice library not installed!")
        return None
    try:
        dev = SayoDevice.open()
    except Exception as e:
        print(f"❌ SayoDevice error: {e}")
        return None

    result = [None]
    stop = threading.Event()

    def on_btn(event):
        if event.pressed:
            if event.button in BUTTON_NAMES:
                friendly = BUTTON_NAMES[event.button]
                print(f"  ✅ Detected: {event.button} ({friendly})")
                result[0] = event.button
                stop.set()

    listener = DeviceListener(dev, poll_interval_ms=10)
    listener.on_button(on_btn)
    listener.start()
    print("\n🎮 Press any button on the SayoDevice... (b = back)")
    try:
        while not stop.is_set():
            if msvcrt.kbhit():
                key = msvcrt.getch()
                if key in (b'b', b'B'):
                    print("  ⬅️  Back")
                    break
            stop.wait(timeout=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        dev.close()
    return result[0]


# ═══════════════════════════════════════════════════════════════
#  Config editor — view
# ═══════════════════════════════════════════════════════════════

def view_config(config, current_bank):
    print("\n" + "=" * 60)
    print("📋 CONFIGURATION")
    print("=" * 60)

    print(f"\n  🎹 MIDI OUT: {config.get('midi_out_port', config.get('midi_port', 'Not set'))}")
    print(f"  📥 MIDI IN:  {config.get('midi_in_port', 'Not set')}")

    # Buttons in current bank
    banks = config.get("banks", {})
    print(f"\n  📦 Banks: {', '.join(banks.keys())}  (current: {current_bank})")

    bank = banks.get(current_bank, {})
    bank_vars = bank.get("vars", {})
    if bank_vars:
        print(f"\n  📐 Variables:")
        for k, val in bank_vars.items():
            print(f"     {k} = {val}")
    bank_scenes = bank.get("scenes", {})
    if bank_scenes:
        print(f"\n  🎬 Scenes:")
        for name, props in bank_scenes.items():
            parts = ", ".join(f"{k}={v}" for k, v in props.items())
            print(f"     {name}: {parts}")
    buttons = bank.get("buttons", {})
    print(f"\n  🔘 Buttons ({len(buttons)}):")
    if buttons:
        for btn, cfg in buttons.items():
            friendly = BUTTON_NAMES.get(btn, btn)
            is_multi = any(k in cfg for k in ("on_press", "on_release", "on_hold"))
            if is_multi:
                print(f"     {btn} ({friendly})  [multi-action, hold={cfg.get('hold_time_ms', 500)}ms]")
                for key, sym in [("on_press", "▶"), ("on_release", "⏹"), ("on_hold", "⏸"), ("on_hold_release", "⏏")]:
                    if key in cfg:
                        print(f"       {sym} {key}: {_fmt_action(cfg[key])}")
                    else:
                        print(f"       {sym} {key}: (not set)")
            else:
                print(f"     {btn} ({friendly}):  {_fmt_action(cfg)}")
    else:
        print("     (none)")

    # Feedback rules
    rules = config.get("midi_feedback", [])
    print(f"\n  📥 Feedback Rules ({len(rules)}):")
    if rules:
        for i, r in enumerate(rules):
            match_s = _fmt_match(r.get("match", {}))
            acts_s = _fmt_feedback_actions(r)
            print(f"     {i+1}. {match_s}  →  {acts_s}")
    else:
        print("     (none)")

    # Screen layout
    se = config.get("screen_elements", {})
    print(f"\n  🖥️  Screen Layout: {'ON' if se.get('enabled') else 'OFF'}")
    for idx_str, elem in se.get("elements", {}).items():
        print(f"     [{idx_str}] {elem.get('width',40)}x{elem.get('height',40)}"
              f" at ({elem.get('x',0)},{elem.get('y',0)}) color={elem.get('color','?')}")

    # Knob fix
    kf = config.get("knob_fix", {})
    print(f"\n  🔧 Knob Fix: {'ON' if kf.get('enabled') else 'OFF'}")
    if kf.get("enabled"):
        print(f"     L={kf.get('left_button')}  R={kf.get('right_button')}  debounce={kf.get('debounce_ms',50)}ms")

    input("\n⏎  Press Enter to continue...")


# ═══════════════════════════════════════════════════════════════
#  Config editor — MIDI ports
# ═══════════════════════════════════════════════════════════════

def select_midi_out_port(config):
    ports = mido.get_output_names()  # pyright: ignore[reportAttributeAccessIssue]
    if not ports:
        print("❌ No MIDI output ports available!")
        return
    try:
        selected = inquirer.select(
            message="🎹 Select MIDI OUT port",
            choices=ports,
            default=config.get("midi_out_port", config.get("midi_port")),
        ).execute()
        config["midi_out_port"] = selected
        print(f"✅ MIDI OUT: {selected}")
    except KeyboardInterrupt:
        pass


def select_midi_in_port(config):
    ports = mido.get_input_names()  # pyright: ignore[reportAttributeAccessIssue]
    if not ports:
        print("❌ No MIDI input ports available!")
        return
    choices = [Choice("__none__", "🚫 None (disable feedback)")] + list(ports)
    try:
        selected = inquirer.select(
            message="📥 Select MIDI IN port (for feedback)",
            choices=choices,
            default=config.get("midi_in_port"),
        ).execute()
        if selected == "__none__":
            config.pop("midi_in_port", None)
            print("✅ MIDI IN disabled")
        else:
            config["midi_in_port"] = selected
            print(f"✅ MIDI IN: {selected}")
    except KeyboardInterrupt:
        pass




# ═══════════════════════════════════════════════════════════════
#  Config editor — button mappings (MIDI OUT)
# ═══════════════════════════════════════════════════════════════

def add_modify_button(config, current_bank):
    """Add or modify a button → MIDI OUT mapping."""
    banks = config.setdefault("banks", {})
    bank = banks.setdefault(current_bank, {"buttons": {}})
    buttons = bank.setdefault("buttons", {})

    # Pick button
    try:
        method = inquirer.select(
            message="How to select the button?",
            choices=[
                Choice("detect", "🎮 Press a button to auto-detect"),
                Choice("list", "📋 Choose from list"),
            ],
        ).execute()
    except KeyboardInterrupt:
        return

    if method == "detect":
        try:
            button_code = _detect_button()
        except KeyboardInterrupt:
            return
    else:
        btn_choices = [Choice(c, f"{c} ({n})") for c, n in BUTTON_NAMES.items()]
        try:
            button_code = inquirer.select(message="Select button", choices=btn_choices).execute()
        except KeyboardInterrupt:
            return

    if not button_code:
        return

    # Check existing
    if button_code in buttons:
        existing = buttons[button_code]
        friendly = BUTTON_NAMES.get(button_code, button_code)
        print(f"\n⚠️  {button_code} ({friendly}) already mapped: {_fmt_button(existing)}")
        try:
            overwrite = inquirer.confirm(message="Overwrite?", default=True).execute()
        except KeyboardInterrupt:
            return
        if not overwrite:
            return

    # Choose mapping preset
    try:
        mode = inquirer.select(
            message="Mapping preset",
            choices=[
                Choice("note_momentary", "🎵 Note momentary (press=on, release=off)"),
                Choice("cc_momentary", "🎛️  CC momentary (press=value, release=0)"),
                Choice("cc_press_only", "🎛️  CC on press only"),
                Choice("program_change", "📝 Program Change (on press)"),
                Choice("bank_switch", "🔄 Bank Switch"),
                Choice("custom", "⚙️  Custom (configure press / release / hold)"),
            ],
        ).execute()
    except KeyboardInterrupt:
        return

    try:
        if mode == "note_momentary":
            note = _ask_int("MIDI note (0-127)", 60, 0, 127)
            vel = _ask_int("Velocity (0-127)", 127, 0, 127)
            ch = _ask_int("MIDI channel (0-15)", 0, 0, 15)
            buttons[button_code] = {
                "on_press": {"type": "note_on", "note": note, "velocity": vel, "channel": ch},
                "on_release": {"type": "note_off", "note": note, "channel": ch},
            }

        elif mode == "cc_momentary":
            cc = _ask_int("CC number (0-127)", 1, 0, 127)
            val = _ask_int("CC value (0-127)", 127, 0, 127)
            ch = _ask_int("MIDI channel (0-15)", 0, 0, 15)
            buttons[button_code] = {
                "on_press": {"type": "cc", "cc": cc, "value": val, "channel": ch},
                "on_release": {"type": "cc", "cc": cc, "value": 0, "channel": ch},
            }

        elif mode == "cc_press_only":
            cc = _ask_int("CC number (0-127)", 1, 0, 127)
            val = _ask_int("CC value (0-127)", 127, 0, 127)
            ch = _ask_int("MIDI channel (0-15)", 0, 0, 15)
            buttons[button_code] = {
                "on_press": {"type": "cc", "cc": cc, "value": val, "channel": ch},
            }

        elif mode == "program_change":
            prog = _ask_int("Program (0-127)", 0, 0, 127)
            ch = _ask_int("MIDI channel (0-15)", 0, 0, 15)
            buttons[button_code] = {
                "on_press": {"type": "program_change", "program": prog, "channel": ch},
            }

        elif mode == "bank_switch":
            target = inquirer.text(message="Target bank name").execute()
            buttons[button_code] = {
                "on_press": {"type": "bank_switch", "bank": target},
            }

        elif mode == "custom":
            hold_ms = _ask_int("Hold threshold ms (how long before hold triggers)", 500, 100, 10000)
            btn_cfg = {"hold_time_ms": hold_ms}

            for key, label in [
                ("on_press", "ON_PRESS (fires immediately when pressed)"),
                ("on_release", "ON_RELEASE (fires when released, if NOT held)"),
                ("on_hold", "ON_HOLD (fires after hold threshold)"),
                ("on_hold_release", "ON_HOLD_RELEASE (fires when released AFTER hold)"),
            ]:
                print(f"\n▶  Configure {label}:")
                act = _ask_midi_action(key)
                if act:
                    btn_cfg[key] = act

            if not any(k in btn_cfg for k in ("on_press", "on_release", "on_hold", "on_hold_release")):
                print("❌ No actions configured — skipping")
                return

            buttons[button_code] = btn_cfg

    except KeyboardInterrupt:
        return

    friendly = BUTTON_NAMES.get(button_code, button_code)
    print(f"\n✅ {button_code} ({friendly}) mapped: {_fmt_button(buttons[button_code])}")


def remove_button(config, current_bank):
    bank = config.get("banks", {}).get(current_bank, {})
    buttons = bank.get("buttons", {})
    if not buttons:
        print("\n❌ No buttons mapped.")
        return
    choices = [Choice(c, f"{c} ({BUTTON_NAMES.get(c, c)}): {_fmt_button(cfg)}")
               for c, cfg in buttons.items()]
    try:
        selected = inquirer.select(message="Remove which button?", choices=choices).execute()
        confirm = inquirer.confirm(message=f"Remove {selected}?", default=False).execute()
        if confirm:
            del buttons[selected]
            print(f"✅ Removed {selected}")
    except KeyboardInterrupt:
        pass


def _config_submenu_buttons(config, current_bank):
    """Buttons sub-menu with add/modify and remove."""
    while True:
        bank = config.get("banks", {}).get(current_bank, {})
        buttons = bank.get("buttons", {})
        if buttons:
            print(f"\n  🔘 Buttons ({len(buttons)}) in bank '{current_bank}':")
            for btn, cfg in buttons.items():
                friendly = BUTTON_NAMES.get(btn, btn)
                is_multi = any(k in cfg for k in ("on_press", "on_release", "on_hold"))
                if is_multi:
                    parts = []
                    for key in ("on_press", "on_release", "on_hold", "on_hold_release"):
                        if key in cfg:
                            parts.append(f"{key}={_fmt_action(cfg[key])}")
                    print(f"     {btn} ({friendly}): {', '.join(parts)}")
                else:
                    print(f"     {btn} ({friendly}): {_fmt_action(cfg)}")
        else:
            print("\n  🔘 No buttons mapped yet.")

        try:
            action = inquirer.select(
                message="Buttons",
                choices=[
                    Choice("add", "➕ Add / modify button"),
                    Choice("remove", "❌ Remove button"),
                    Separator(),
                    Choice("back", "⬅️  Back"),
                ],
            ).execute()
        except KeyboardInterrupt:
            break

        if action == "back":
            break
        elif action == "add":
            add_modify_button(config, current_bank)
        elif action == "remove":
            remove_button(config, current_bank)


# ═══════════════════════════════════════════════════════════════
#  Config editor — MIDI feedback rules
# ═══════════════════════════════════════════════════════════════

def _ask_feedback_match():
    """Interactively build a feedback match dict. Returns dict or None."""
    try:
        rtype = inquirer.select(
            message="Match incoming MIDI type",
            choices=[
                Choice("note_on", "🎵 Note ON"),
                Choice("note_off", "🎵 Note OFF"),
                Choice("cc", "🎛️  CC (control change)"),
                Choice("program_change", "📝 Program Change"),
                Choice("raw", "📦 Raw hex bytes"),
            ],
        ).execute()
    except KeyboardInterrupt:
        return None

    match = {"type": rtype}

    try:
        if rtype == "raw":
            print("\n  Enter hex bytes to match. Use xx for wildcard bytes.")
            print("  Examples: 90 3C xx    B0 01 xx    F0 7E xx xx F7")
            hex_str = inquirer.text(
                message="Hex pattern",
                validate=lambda v: len(v.strip()) > 0,
            ).execute().strip().upper()
            # Normalize: replace ** with XX
            hex_str = hex_str.replace("**", "XX")
            match["hex"] = hex_str
            use_vb = inquirer.confirm(
                message="Extract a value byte for dynamic properties?",
                default=True,
            ).execute()
            if use_vb:
                n_tokens = len(hex_str.split())
                match["value_byte"] = _ask_int(
                    f"Value byte index (0-based, pattern has {n_tokens} bytes)",
                    min(2, n_tokens - 1), 0, 31,
                )
        elif rtype in ("note_on", "note_off"):
            match["note"] = _ask_int("Note number (0-127)", 60, 0, 127)
            match["channel"] = _ask_int("Channel (0-15)", 0, 0, 15)
        elif rtype == "cc":
            match["cc"] = _ask_int("CC number (0-127)", 1, 0, 127)
            match["channel"] = _ask_int("Channel (0-15)", 0, 0, 15)
            use_val = inquirer.confirm(message="Match specific CC value?", default=False).execute()
            if use_val:
                match["value"] = _ask_int("CC value (0-127)", 127, 0, 127)
        elif rtype == "program_change":
            match["program"] = _ask_int("Program (0-127)", 0, 0, 127)
            match["channel"] = _ask_int("Channel (0-15)", 0, 0, 15)
    except KeyboardInterrupt:
        return None

    return match


def _ask_dynamic_props(available_props, existing_dynamic=None):
    """Ask user which properties should be driven by velocity/value.

    Returns a dynamic dict or None.
    """
    existing_dynamic = existing_dynamic or {}
    dynamic = {}

    # Only ask for non-color props (size/position) — color is handled by color mode
    remaining = [p for p in available_props if p != "color" and p not in dynamic]
    if not remaining:
        return existing_dynamic if existing_dynamic else None

    try:
        want = inquirer.confirm(
            message="📐 Map velocity/value to size/position?",
            default=any(p in existing_dynamic for p in remaining),
        ).execute()
        if not want:
            return existing_dynamic if existing_dynamic else None
    except KeyboardInterrupt:
        return existing_dynamic if existing_dynamic else None

    # Keep existing color dynamic if any
    if "color" in existing_dynamic:
        dynamic["color"] = existing_dynamic["color"]

    while True:
        remaining = [p for p in available_props if p != "color" and p not in dynamic]
        if not remaining:
            break

        choices = [Choice(p, p) for p in remaining]
        choices.append(Separator())
        choices.append(Choice("__done__", "✅ Done"))

        try:
            prop = inquirer.select(
                message="Which property to drive dynamically?",
                choices=choices,
            ).execute()
        except KeyboardInterrupt:
            break
        if prop == "__done__":
            break

        ex = existing_dynamic.get(prop, {})

        try:
            source = inquirer.select(
                message=f"Drive '{prop}' from",
                choices=[
                    Choice("velocity", "🎵 Velocity (note_on 0-127)"),
                    Choice("value", "🎛️  CC value (0-127)"),
                ],
                default=ex.get("from", "velocity"),
            ).execute()
        except KeyboardInterrupt:
            break

        bounds = {"width": (0, 160), "height": (0, 80),
                  "x": (0, 159), "y": (0, 79)}
        mn, mx = bounds.get(prop, (0, 127))
        d_min = ex.get("min", mn)
        d_max = ex.get("max", mx)
        lo = _ask_int(f"{prop} at 0 (min)", int(d_min), mn, mx)
        hi = _ask_int(f"{prop} at 127 (max)", int(d_max), mn, mx)

        dynamic[prop] = {"from": source, "min": lo, "max": hi}
        print(f"  ✅ {prop}: {lo} → {hi} (from {source})")

        try:
            more = inquirer.confirm(message="Add another dynamic property?", default=False).execute()
            if not more:
                break
        except KeyboardInterrupt:
            break

    return dynamic if dynamic else None


def _ask_color_mode(existing=None):
    """Ask user how color should be determined.

    Returns (color, dynamic_color_entry_or_None, value_map_or_None).
    existing should be the full screen/led config dict (or None).
    """
    existing = existing or {}
    ex_vmap = existing.get("value_map")
    ex_dyn = existing.get("dynamic", {}).get("color")

    # Determine current mode for default
    if ex_vmap:
        default_mode = "map"
    elif ex_dyn:
        default_mode = "gradient"
    else:
        default_mode = "fixed"

    try:
        mode = inquirer.select(
            message="🎨 Color mode",
            choices=[
                Choice("fixed", "🔒 Fixed color"),
                Choice("map", "🗂️  Value map (value → color, like switch/case)"),
                Choice("gradient", "🌈 Gradient (interpolate min → max)"),
            ],
            default=default_mode,
        ).execute()
    except KeyboardInterrupt:
        d_color = existing.get("color", "#FF0000")
        return _ask_color("Color (#RRGGBB)", d_color), None, None

    if mode == "fixed":
        d_color = existing.get("color", "#FF0000")
        color = _ask_color("Color (#RRGGBB)", d_color)
        return color, None, None

    elif mode == "map":
        d_color = existing.get("color", "#000000")
        color = _ask_color("Fallback color (when no value matches)", d_color)
        vmap = _ask_value_map_quick(ex_vmap)
        return color, None, vmap

    elif mode == "gradient":
        ex_min = ex_dyn.get("min", "#000000") if ex_dyn else "#000000"
        ex_max = ex_dyn.get("max", "#FF0000") if ex_dyn else "#FF0000"
        lo = _ask_color("Color at value 0 (min)", ex_min)
        hi = _ask_color("Color at value 127 (max)", ex_max)
        try:
            source = inquirer.select(
                message="Drive from",
                choices=[
                    Choice("velocity", "🎵 Velocity (0-127)"),
                    Choice("value", "🎛️  CC / raw value (0-127)"),
                ],
                default=ex_dyn.get("from", "velocity") if ex_dyn else "velocity",
            ).execute()
        except KeyboardInterrupt:
            source = "velocity"
        dyn_entry = {"from": source, "min": lo, "max": hi}
        return lo, dyn_entry, None

    return "#FF0000", None, None


def _ask_value_map_quick(existing_map=None):
    """Quick value map editor. Supports batch entry, scene references.

    Returns dict or None.
    """
    vmap = dict(existing_map) if existing_map else {}
    scenes = _bank_scenes_hint

    if vmap:
        print("  Current map:")
        for k, v in vmap.items():
            print(f"    {k} → {v}")

    # Build completer for quick entry
    _vmap_comp = {}
    for name, props in scenes.items():
        _vmap_comp[f"scene:{name}"] = None
    for k, v in _bank_vars_hint.items():
        _vmap_comp[f"${k}"] = None

    print("\n  Enter value=color or value=scene:<name> pairs. Tab to complete.")
    print("  Examples: 0=#FF0000  5=#$off_subdued$off_subdued$off_dominant")
    if scenes:
        print(f"  Scenes: {', '.join(scenes.keys())}")
    if _bank_vars_hint:
        print(f"  Vars: {', '.join(f'${k}={v}' for k,v in _bank_vars_hint.items())}")
    print("  Press Enter for interactive mode.\n")

    try:
        batch = inquirer.text(
            message="Quick entry (value=color/scene ...)",
            default="",
            completer=_vmap_comp if _vmap_comp else None,
        ).execute().strip()
    except KeyboardInterrupt:
        return vmap if vmap else None

    if batch:
        try:
            tokens = shlex.split(batch)
        except ValueError:
            tokens = batch.split()
        for token in tokens:
            if "=" in token:
                k, v = token.split("=", 1)
                vmap[k.strip()] = v.strip()
                print(f"  ✅ {k.strip()} → {v.strip()}")
            else:
                print(f"  ⚠️  Skipped '{token}' (no = found)")

    # Offer interactive add/remove
    while True:
        scene_choices = []
        if scenes:
            scene_choices = [Choice("add_scene", "🎬 Add scene entry")]

        try:
            action = inquirer.select(
                message=f"Value map ({len(vmap)} entries)",
                choices=[
                    Choice("done", f"✅ Done ({len(vmap)} entries)"),
                    Choice("add", "➕ Add color entry"),
                    *scene_choices,
                    Choice("batch", "📝 Batch entry"),
                    *([ Choice("remove", "🗑️  Remove entry") ] if vmap else []),
                    *([ Choice("clear", "🗑️  Clear all") ] if vmap else []),
                ],
            ).execute()
        except KeyboardInterrupt:
            break
        if action == "done":
            break
        elif action == "add":
            val = _ask_int("Value (0-255)", 0, 0, 255)
            color = _ask_color("Color", "#FF0000")
            vmap[str(val)] = color
            print(f"  ✅ {val} → {color}")
        elif action == "add_scene":
            val = _ask_int("Value (0-255)", 0, 0, 255)
            try:
                scene_name = inquirer.select(
                    message="Which scene?",
                    choices=[Choice(n, f"{n}: {', '.join(f'{k}={v}' for k,v in p.items())}")
                             for n, p in scenes.items()],
                ).execute()
                vmap[str(val)] = f"scene:{scene_name}"
                print(f"  ✅ {val} → scene:{scene_name}")
            except KeyboardInterrupt:
                pass
        elif action == "batch":
            try:
                more = inquirer.text(message="value=color/scene ...", default="").execute().strip()
            except KeyboardInterrupt:
                continue
            if more:
                try:
                    tokens = shlex.split(more)
                except ValueError:
                    tokens = more.split()
                for token in tokens:
                    if "=" in token:
                        k, v = token.split("=", 1)
                        vmap[k.strip()] = v.strip()
                        print(f"  ✅ {k.strip()} → {v.strip()}")
        elif action == "remove":
            if not vmap:
                continue
            try:
                key = inquirer.select(
                    message="Remove which?",
                    choices=[Choice(k, f"{k} → {v}") for k, v in vmap.items()],
                ).execute()
                del vmap[key]
                print(f"  🗑️  Removed {key}")
            except KeyboardInterrupt:
                continue
        elif action == "clear":
            vmap.clear()
            print("  🗑️  Cleared")

    return vmap if vmap else None


def _ask_feedback_screen(existing=None):
    """Ask for a screen element action. Returns dict or None."""
    try:
        link = inquirer.confirm(message="🖥️  Set a screen element color?", default=existing is not None).execute()
        if not link:
            return None
        d_idx = existing.get("element_index", 1) if existing else 1
        idx = _ask_int("Element index (0-15)", d_idx, 0, 15)

        color, dyn_color, vmap = _ask_color_mode(existing)
        result = {"element_index": idx, "color": color}

        # Build dynamic dict: color gradient + size/position
        dynamic = {}
        if dyn_color:
            dynamic["color"] = dyn_color
        size_dyn = _ask_dynamic_props(
            ["width", "height", "x", "y"],
            existing.get("dynamic") if existing else None,
        )
        if size_dyn:
            dynamic.update(size_dyn)
        if dynamic:
            result["dynamic"] = dynamic
        if vmap:
            result["value_map"] = vmap
        return result
    except KeyboardInterrupt:
        return None


def _ask_feedback_led(existing=None):
    """Ask for a button LED action. Returns dict or None."""
    try:
        link = inquirer.confirm(message="💡 Set a button LED color?", default=existing is not None).execute()
        if not link:
            return None
        d_ki = existing.get("key_index", 0) if existing else 0
        ki = _ask_int("Key index (0=btn1, 1=btn2, 2=btn3)", d_ki, 0, 2)

        color, dyn_color, vmap = _ask_color_mode(existing)
        result = {"key_index": ki, "color": color}
        if dyn_color:
            result["dynamic"] = {"color": dyn_color}
        if vmap:
            result["value_map"] = vmap
        return result
    except KeyboardInterrupt:
        return None


def add_feedback_rule(config):
    """Add a new MIDI IN feedback rule."""
    rules = config.setdefault("midi_feedback", [])

    try:
        mode = inquirer.select(
            message="Add feedback rule",
            choices=[
                Choice("single", "📝 Single rule (one match → one action)"),
                Choice("pair", "🔄 Note pair (note_on → color A,  note_off → color B)"),
            ],
        ).execute()
    except KeyboardInterrupt:
        return

    if mode == "single":
        match = _ask_feedback_match()
        if not match:
            return
        screen = _ask_feedback_screen()
        led = _ask_feedback_led()
        if not screen and not led:
            print("❌ No actions — rule not created.")
            return
        rule = {"match": match}
        if screen:
            rule["screen"] = screen
        if led:
            rule["led"] = led
        rules.append(rule)
        print(f"✅ Rule added: {_fmt_match(match)} → {_fmt_feedback_actions(rule)}")
        return

    elif mode == "pair":
        try:
            note = _ask_int("Note number (0-127)", 60, 0, 127)
            ch = _ask_int("Channel (0-15)", 0, 0, 15)
        except KeyboardInterrupt:
            return

        # ON actions
        print("\n── note_on actions ──")
        on_screen = _ask_feedback_screen()
        on_led = _ask_feedback_led()

        # OFF actions
        print("\n── note_off actions ──")
        off_screen = _ask_feedback_screen()
        off_led = _ask_feedback_led()

        if not any([on_screen, on_led, off_screen, off_led]):
            print("❌ No actions — rules not created.")
            return

        if on_screen or on_led:
            r_on = {"match": {"type": "note_on", "note": note, "channel": ch}}
            if on_screen:
                r_on["screen"] = on_screen
            if on_led:
                r_on["led"] = on_led
            rules.append(r_on)
            print(f"✅ Rule: note_on N{note} ch{ch} → {_fmt_feedback_actions(r_on)}")

        if off_screen or off_led:
            r_off = {"match": {"type": "note_off", "note": note, "channel": ch}}
            if off_screen:
                r_off["screen"] = off_screen
            if off_led:
                r_off["led"] = off_led
            rules.append(r_off)
            print(f"✅ Rule: note_off N{note} ch{ch} → {_fmt_feedback_actions(r_off)}")


def edit_feedback_rule(config):
    rules = config.get("midi_feedback", [])
    if not rules:
        print("\n❌ No feedback rules to edit.")
        return

    choices = []
    for i, r in enumerate(rules):
        label = f"{i+1}. {_fmt_match(r.get('match', {}))} → {_fmt_feedback_actions(r)}"
        choices.append(Choice(i, label))

    try:
        idx = inquirer.select(message="Edit which rule?", choices=choices).execute()
    except KeyboardInterrupt:
        return

    rule = rules[idx]
    print(f"\nEditing rule {idx+1}: {_fmt_match(rule.get('match', {}))}")

    try:
        what = inquirer.select(
            message="What to edit?",
            choices=[
                Choice("match", "🎯 Change match criteria"),
                Choice("screen", "🖥️  Change screen action"),
                Choice("led", "💡 Change LED action"),
                Choice("remove_screen", "🗑️  Remove screen action"),
                Choice("remove_led", "🗑️  Remove LED action"),
            ],
        ).execute()
    except KeyboardInterrupt:
        return

    if what == "match":
        new_match = _ask_feedback_match()
        if new_match:
            rule["match"] = new_match
            print("✅ Match updated")
    elif what == "screen":
        s = _ask_feedback_screen(existing=rule.get("screen"))
        if s:
            rule["screen"] = s
            print("✅ Screen action updated")
    elif what == "led":
        l = _ask_feedback_led(existing=rule.get("led"))
        if l:
            rule["led"] = l
            print("✅ LED action updated")
    elif what == "remove_screen":
        rule.pop("screen", None)
        print("✅ Screen action removed")
    elif what == "remove_led":
        rule.pop("led", None)
        print("✅ LED action removed")

    # Remove rule if no actions left
    if "screen" not in rule and "led" not in rule:
        rules.pop(idx)
        print("ℹ️  Rule had no actions left — removed entirely.")


def remove_feedback_rule(config):
    rules = config.get("midi_feedback", [])
    if not rules:
        print("\n❌ No feedback rules.")
        return

    choices = []
    for i, r in enumerate(rules):
        label = f"{i+1}. {_fmt_match(r.get('match', {}))} → {_fmt_feedback_actions(r)}"
        choices.append(Choice(i, label))

    try:
        idx = inquirer.select(message="Remove which rule?", choices=choices).execute()
        confirm = inquirer.confirm(message=f"Remove rule {idx+1}?", default=False).execute()
        if confirm:
            rules.pop(idx)
            print("✅ Rule removed")
    except KeyboardInterrupt:
        pass


# ═══════════════════════════════════════════════════════════════
#  Config editor — quick wire wizard
# ═══════════════════════════════════════════════════════════════

def quick_wire(config, current_bank):
    """Map a button AND create feedback rules in one flow."""
    banks = config.setdefault("banks", {})
    bank = banks.setdefault(current_bank, {"buttons": {}})
    buttons = bank.setdefault("buttons", {})
    rules = config.setdefault("midi_feedback", [])

    print("\n" + "=" * 60)
    print("⚡ QUICK WIRE — button + visual feedback in one step")
    print("=" * 60)

    # 1. Select button
    try:
        method = inquirer.select(
            message="Select button",
            choices=[
                Choice("detect", "🎮 Press a button to auto-detect"),
                Choice("list", "📋 Choose from list"),
            ],
        ).execute()
    except KeyboardInterrupt:
        return

    if method == "detect":
        try:
            button_code = _detect_button()
        except KeyboardInterrupt:
            return
    else:
        btn_choices = [Choice(c, f"{c} ({n})") for c, n in BUTTON_NAMES.items()]
        try:
            button_code = inquirer.select(message="Select button", choices=btn_choices).execute()
        except KeyboardInterrupt:
            return

    if button_code in buttons:
        friendly = BUTTON_NAMES.get(button_code, button_code)
        print(f"\n⚠️  {button_code} ({friendly}) already mapped: {_fmt_button(buttons[button_code])}")
        try:
            if not inquirer.confirm(message="Overwrite?", default=True).execute():
                return
        except KeyboardInterrupt:
            return

    # 2. MIDI type
    try:
        midi_type = inquirer.select(
            message="MIDI type",
            choices=[
                Choice("note", "🎵 Note (momentary — most common)"),
                Choice("cc", "🎛️  CC (control change)"),
            ],
        ).execute()
    except KeyboardInterrupt:
        return

    try:
        if midi_type == "note":
            note = _ask_int("MIDI note (0-127)", 60, 0, 127)
            vel = _ask_int("Velocity (0-127)", 127, 0, 127)
            ch = _ask_int("MIDI channel (0-15)", 0, 0, 15)
            buttons[button_code] = {
                "on_press": {"type": "note_on", "note": note, "velocity": vel, "channel": ch},
                "on_release": {"type": "note_off", "note": note, "channel": ch},
            }
        else:
            cc = _ask_int("CC number (0-127)", 1, 0, 127)
            val = _ask_int("CC value (0-127)", 127, 0, 127)
            ch = _ask_int("Channel (0-15)", 0, 0, 15)
            buttons[button_code] = {
                "on_press": {"type": "cc", "cc": cc, "value": val, "channel": ch},
                "on_release": {"type": "cc", "cc": cc, "value": 0, "channel": ch},
            }
            note = None  # type: ignore[assignment]
            vel = None  # type: ignore[assignment]
    except KeyboardInterrupt:
        return

    friendly = BUTTON_NAMES.get(button_code, button_code)
    print(f"\n✅ {button_code} ({friendly}) → {_fmt_button(buttons[button_code])}")

    # 3. Wire screen element feedback?
    on_screen = None
    off_screen = None
    try:
        wire_screen = inquirer.confirm(message="🖥️  Wire screen element feedback?", default=False).execute()
        if wire_screen:
            el_idx = _ask_int("Element index (0-15)", 1, 0, 15)
            on_color = _ask_color("Color ON (#RRGGBB)", "#FF0000")
            off_color = _ask_color("Color OFF (#RRGGBB)", "#333333")
            on_screen = {"element_index": el_idx, "color": on_color}
            off_screen = {"element_index": el_idx, "color": off_color}
    except KeyboardInterrupt:
        pass

    # 4. Wire LED feedback?
    on_led = None
    off_led = None
    try:
        wire_led = inquirer.confirm(message="💡 Wire LED feedback?", default=False).execute()
        if wire_led:
            ki = _ask_int("Key index (0=btn1, 1=btn2, 2=btn3)", 0, 0, 2)
            led_on_color = _ask_color("LED color ON (#RRGGBB)", "#FF0000")
            led_off_color = _ask_color("LED color OFF (#RRGGBB)", "#000033")
            on_led = {"key_index": ki, "color": led_on_color}
            off_led = {"key_index": ki, "color": led_off_color}
    except KeyboardInterrupt:
        pass

    # 5. Create feedback rules
    if midi_type == "note" and (on_screen or on_led or off_screen or off_led):
        if on_screen or on_led:
            r_on = {"match": {"type": "note_on", "note": note, "channel": ch}}
            if on_screen:
                r_on["screen"] = on_screen
            if on_led:
                r_on["led"] = on_led
            rules.append(r_on)
            print(f"  📥 Rule: note_on  N{note} ch{ch} → {_fmt_feedback_actions(r_on)}")

        if off_screen or off_led:
            r_off = {"match": {"type": "note_off", "note": note, "channel": ch}}
            if off_screen:
                r_off["screen"] = off_screen
            if off_led:
                r_off["led"] = off_led
            rules.append(r_off)
            print(f"  📥 Rule: note_off N{note} ch{ch} → {_fmt_feedback_actions(r_off)}")

    elif midi_type == "cc" and (on_screen or on_led):
        # For CC, create a single rule that matches any value
        r = {"match": {"type": "cc", "cc": cc, "channel": ch}}
        if on_screen:
            r["screen"] = on_screen
        if on_led:
            r["led"] = on_led
        rules.append(r)
        print(f"  📥 Rule: CC{cc} ch{ch} → {_fmt_feedback_actions(r)}")

    print("\n✅ Quick wire complete!")


# ═══════════════════════════════════════════════════════════════
#  Config editor — screen layout
# ═══════════════════════════════════════════════════════════════

def edit_screen_layout(config):
    se = config.setdefault("screen_elements", {"enabled": False, "elements": {}})
    elements = se.setdefault("elements", {})

    while True:
        try:
            action = inquirer.select(
                message=f"🖥️  Screen Layout ({'ON' if se.get('enabled') else 'OFF'})",
                choices=[
                    Choice("toggle", f"{'🔴 Disable' if se.get('enabled') else '🟢 Enable'} screen elements"),
                    Choice("add", "➕ Add / edit element"),
                    Choice("remove", "🗑️  Remove element"),
                    Choice("back", "⬅️  Back"),
                ],
            ).execute()
        except KeyboardInterrupt:
            break

        if action == "back":
            break
        elif action == "toggle":
            se["enabled"] = not se.get("enabled", False)
            print(f"✅ Screen elements {'enabled' if se['enabled'] else 'disabled'}")
        elif action == "add":
            try:
                idx = _ask_int("Element index (0-15)", 1, 0, 15)
                idx_str = str(idx)
                defaults = elements.get(idx_str, {})
                x = _ask_int_or_var("X position (0-159)", defaults.get("x", 0), 0, 159)
                y = _ask_int_or_var("Y position (0-79)", defaults.get("y", 0), 0, 79)
                w = _ask_int_or_var("Width (1-160)", defaults.get("width", 40), 1, 160)
                h = _ask_int_or_var("Height (1-80)", defaults.get("height", 40), 1, 80)
                color = _ask_color("Initial color (#RRGGBB)", defaults.get("color", "#333333"))
                elements[idx_str] = {"x": x, "y": y, "width": w, "height": h,
                                     "color": color, "element_type": 1}
                print(f"✅ Element {idx}: {w}x{h} at ({x},{y}) color={color}")
            except KeyboardInterrupt:
                pass
        elif action == "remove":
            if not elements:
                print("❌ No elements to remove")
                continue
            choices = [Choice(k, f"[{k}] {v.get('width',40)}x{v.get('height',40)} at ({v.get('x',0)},{v.get('y',0)}) {v.get('color','?')}")
                       for k, v in elements.items()]
            try:
                sel = inquirer.select(message="Remove element", choices=choices).execute()
                del elements[sel]
                print(f"✅ Element {sel} removed")
            except KeyboardInterrupt:
                pass


# ═══════════════════════════════════════════════════════════════
#  Config editor — knob fix
# ═══════════════════════════════════════════════════════════════

def edit_knob_fix(config):
    kf = config.setdefault("knob_fix", {
        "enabled": False, "left_button": "knob_left", "right_button": "knob_right",
        "debounce_ms": 50, "test_mode": False,
        "left_midi": None, "right_midi": None,
    })

    while True:
        try:
            action = inquirer.select(
                message=f"🔧 Knob Fix ({'ON' if kf.get('enabled') else 'OFF'})",
                choices=[
                    Choice("toggle", f"{'🔴 Disable' if kf.get('enabled') else '🟢 Enable'} knob fix"),
                    Choice("buttons", "🔘 Set left / right buttons"),
                    Choice("debounce", "⏱️  Set debounce"),
                    Choice("left_midi", "⬅️  Left rotation MIDI"),
                    Choice("right_midi", "➡️  Right rotation MIDI"),
                    Choice("test_mode", f"🧪 Test mode: {'ON' if kf.get('test_mode') else 'OFF'}"),
                    Choice("back", "⬅️  Back"),
                ],
            ).execute()
        except KeyboardInterrupt:
            break

        if action == "back":
            break
        elif action == "toggle":
            kf["enabled"] = not kf.get("enabled", False)
            print(f"✅ Knob fix {'enabled' if kf['enabled'] else 'disabled'}")
        elif action == "buttons":
            try:
                print("\nDetect LEFT rotation button:")
                kf["left_button"] = _detect_button()
                print("Detect RIGHT rotation button:")
                kf["right_button"] = _detect_button()
                print(f"✅ L={kf['left_button']}  R={kf['right_button']}")
            except KeyboardInterrupt:
                pass
        elif action == "debounce":
            try:
                kf["debounce_ms"] = _ask_int("Debounce ms", kf.get("debounce_ms", 50), 10, 500)
            except KeyboardInterrupt:
                pass
        elif action == "left_midi":
            m = _ask_midi_action("left rotation")
            if m:
                kf["left_midi"] = m
        elif action == "right_midi":
            m = _ask_midi_action("right rotation")
            if m:
                kf["right_midi"] = m
        elif action == "test_mode":
            kf["test_mode"] = not kf.get("test_mode", False)
            print(f"✅ Test mode {'ON' if kf['test_mode'] else 'OFF'}")


# ═══════════════════════════════════════════════════════════════
#  Config editor — bank management
# ═══════════════════════════════════════════════════════════════

def select_bank(config):
    banks = config.get("banks", {})
    if not banks:
        print("\n❌ No banks.")
        return config.get("default_bank", "default")
    try:
        selected = inquirer.select(
            message="📦 Select bank",
            choices=list(banks.keys()),
            default=config.get("default_bank"),
        ).execute()
        print(f"✅ Now editing bank: {selected}")
    except KeyboardInterrupt:
        return config.get("default_bank", "default")
    return selected


def create_bank(config):
    banks = config.setdefault("banks", {})
    try:
        name = inquirer.text(
            message="New bank name",
            validate=lambda x: len(x.strip()) > 0,
        ).execute().strip()
    except KeyboardInterrupt:
        return

    if name in banks:
        print(f"❌ Bank '{name}' already exists!")
        return

    try:
        mode = inquirer.select(
            message="Create from",
            choices=[
                Choice("empty", "📄 Empty bank"),
                Choice("copy", "📋 Copy from existing bank"),
            ],
        ).execute()
    except KeyboardInterrupt:
        return

    if mode == "empty":
        banks[name] = {"buttons": {}}
    elif mode == "copy":
        if not banks:
            print("❌ No banks to copy from!")
            return
        try:
            src = inquirer.select(message="Copy from", choices=list(banks.keys())).execute()
            banks[name] = copy.deepcopy(banks[src])
        except KeyboardInterrupt:
            return

    print(f"✅ Bank '{name}' created")


def delete_bank(config, current_bank):
    banks = config.get("banks", {})
    if len(banks) <= 1:
        print("\n❌ Cannot delete the last bank!")
        return current_bank
    try:
        to_del = inquirer.select(message="🗑️  Delete bank", choices=list(banks.keys())).execute()
        if not inquirer.confirm(message=f"Delete '{to_del}'?", default=False).execute():
            return current_bank
    except KeyboardInterrupt:
        return current_bank

    del banks[to_del]
    if config.get("default_bank") == to_del:
        config["default_bank"] = list(banks.keys())[0]
    print(f"✅ Deleted '{to_del}'")
    return config["default_bank"] if current_bank == to_del else current_bank


def rename_bank(config, current_bank):
    banks = config.get("banks", {})
    try:
        old = inquirer.select(message="✏️  Rename bank", choices=list(banks.keys())).execute()
        new = inquirer.text(message=f"New name for '{old}'", default=old,
                            validate=lambda x: len(x.strip()) > 0).execute().strip()
    except KeyboardInterrupt:
        return current_bank

    if new in banks and new != old:
        print("❌ Name already taken!")
        return current_bank

    banks[new] = banks.pop(old)
    if config.get("default_bank") == old:
        config["default_bank"] = new
    print(f"✅ Renamed '{old}' → '{new}'")
    return new if current_bank == old else current_bank


# ═══════════════════════════════════════════════════════════════
#  Config editor — test mode
# ═══════════════════════════════════════════════════════════════

def test_device():
    """Listen for SayoDevice button events and print them."""
    if not SayoDevice:
        print("❌ sayodevice library not installed!")
        return
    try:
        dev = SayoDevice.open()
    except Exception as e:
        print(f"❌ SayoDevice error: {e}")
        return

    print("\n" + "=" * 60)
    print("🧪 TEST — press buttons / turn knob  (Ctrl+C to stop)")
    print("=" * 60 + "\n")
    print("Available inputs:")
    for name, friendly in BUTTON_NAMES.items():
        print(f"   {name:12s} — {friendly}")
    print()

    def on_btn(event):
        friendly = BUTTON_NAMES.get(event.button, event.button)
        state = "PRESSED 🟢" if event.pressed else "RELEASED 🔴"
        print(f"🔘 {event.button} ({friendly}) — {state}")

    listener = DeviceListener(dev, poll_interval_ms=10)
    listener.on_button(on_btn)
    listener.start()
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n")
    finally:
        listener.stop()
        dev.close()


def monitor_midi_in(config):
    """Listen to a MIDI input port and display all incoming messages."""
    ports = mido.get_input_names()  # pyright: ignore[reportAttributeAccessIssue]
    if not ports:
        print("❌ No MIDI input ports available!")
        return

    # Use configured port as default, otherwise let the user pick
    default_port = config.get("midi_in_port")
    try:
        port_name = inquirer.select(
            message="📥 Select MIDI IN port to monitor",
            choices=ports,
            default=default_port if default_port in ports else None,
        ).execute()
    except KeyboardInterrupt:
        return

    try:
        inport = mido.open_input(port_name)  # pyright: ignore[reportAttributeAccessIssue]
    except Exception as e:
        print(f"❌ Cannot open port: {e}")
        return

    print("\n" + "=" * 60)
    print(f"📥 MIDI MONITOR — {port_name}")
    print("=" * 60)
    print("Listening for incoming MIDI messages...  (Ctrl+C to stop)\n")
    print(f"  {'TIME':<10} {'TYPE':<18} {'DETAILS'}")
    print("  " + "─" * 48)

    count = 0
    print("  (press any key to stop)\n")
    try:
        while True:
            # Check for keypress (non-blocking) — Ctrl+C doesn't work in
            # mido's blocking iterator on Windows, so we poll instead.
            if msvcrt.kbhit():
                msvcrt.getch()
                break

            msg = inport.poll()
            if msg is None:
                time.sleep(0.002)
                continue

            ts = time.strftime("%H:%M:%S")
            count += 1

            if msg.type == "note_on":
                vel_bar = "█" * (msg.velocity * 10 // 127)
                detail = (f"note={msg.note:<4d} vel={msg.velocity:<4d} ch={msg.channel}"
                          f"  {vel_bar}")
                icon = "🟢" if msg.velocity > 0 else "⚫"
                print(f"  {ts}  {icon} note_on        {detail}")

            elif msg.type == "note_off":
                print(f"  {ts}  🔴 note_off       "
                      f"note={msg.note:<4d} vel={msg.velocity:<4d} ch={msg.channel}")

            elif msg.type == "control_change":
                val_bar = "▓" * (msg.value * 10 // 127)
                print(f"  {ts}  🎛️  control_change "
                      f"cc={msg.control:<4d} val={msg.value:<4d} ch={msg.channel}"
                      f"  {val_bar}")

            elif msg.type == "program_change":
                print(f"  {ts}  📝 program_change"
                      f" program={msg.program:<4d} ch={msg.channel}")

            elif msg.type == "pitchwheel":
                print(f"  {ts}  🎡 pitchwheel     "
                      f"pitch={msg.pitch:<6d} ch={msg.channel}")

            elif msg.type == "aftertouch":
                print(f"  {ts}  👆 aftertouch     "
                      f"value={msg.value:<4d} ch={msg.channel}")

            elif msg.type == "polytouch":
                print(f"  {ts}  👆 polytouch      "
                      f"note={msg.note:<4d} value={msg.value:<4d} ch={msg.channel}")

            elif msg.type in ("clock", "start", "stop", "continue",
                              "songpos", "song_select", "active_sensing"):
                # Realtime / system messages — show but don't spam
                if msg.type not in ("clock", "active_sensing"):
                    print(f"  {ts}  ⏱️  {msg.type}")

            else:
                print(f"  {ts}  ❓ {msg.type:<16s} {msg}")

    except KeyboardInterrupt:
        pass
    print(f"\n  ✅ Stopped — {count} messages received.")
    inport.close()


# ═══════════════════════════════════════════════════════════════
#  Config editor — main menu
# ═══════════════════════════════════════════════════════════════

def _config_status(config, current_bank):
    """Print a compact status summary header."""
    midi_out = config.get("midi_out_port", config.get("midi_port", "—"))
    midi_in = config.get("midi_in_port", "—")
    banks = config.get("banks", {})
    n_buttons = len(banks.get(current_bank, {}).get("buttons", {}))
    n_feedback = len(config.get("midi_feedback", []))
    se_on = config.get("screen_elements", {}).get("enabled", False)
    kf_on = config.get("knob_fix", {}).get("enabled", False)

    print()


def _copy_config_to_clipboard(config):
    """Dump config as YAML and copy to clipboard."""
    text = yaml.dump(config, default_flow_style=False, sort_keys=False)
    try:
        proc = subprocess.Popen(["powershell", "-Command", "Set-Clipboard -Value $input"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        proc.communicate(input=text.encode("utf-8"), timeout=5)
        if proc.returncode == 0:
            print(f"✅ Config copied to clipboard ({len(text)} chars)")
        else:
            print("❌ Failed to copy to clipboard")
    except Exception as e:
        print(f"❌ Clipboard error: {e}")


def _validate_config(data):
    """Basic structural validation of a config dict.

    Returns (ok: bool, errors: list[str]).
    """
    errors = []
    if not isinstance(data, dict):
        return False, ["Not a YAML mapping/dict"]

    # Must have banks
    banks = data.get("banks")
    if banks is not None and not isinstance(banks, dict):
        errors.append("'banks' must be a dict")
    elif banks:
        for bname, bval in banks.items():
            if not isinstance(bval, dict):
                errors.append(f"Bank '{bname}' must be a dict")
            elif "buttons" in bval and not isinstance(bval["buttons"], dict):
                errors.append(f"Bank '{bname}' → 'buttons' must be a dict")

    # Feedback rules
    fb = data.get("midi_feedback")
    if fb is not None:
        if not isinstance(fb, list):
            errors.append("'midi_feedback' must be a list")
        else:
            for i, rule in enumerate(fb):
                if not isinstance(rule, dict):
                    errors.append(f"Feedback rule {i+1} must be a dict")
                elif "match" not in rule:
                    errors.append(f"Feedback rule {i+1} has no 'match'")

    # Screen elements
    se = data.get("screen_elements")
    if se is not None and not isinstance(se, dict):
        errors.append("'screen_elements' must be a dict")

    # Knob fix
    kf = data.get("knob_fix")
    if kf is not None and not isinstance(kf, dict):
        errors.append("'knob_fix' must be a dict")

    return len(errors) == 0, errors


def _paste_config_from_clipboard(config):
    """Read YAML from clipboard, validate, and overwrite config in-place.

    Returns True if config was replaced.
    """
    try:
        result = subprocess.run(
            ["powershell", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, timeout=5,
        )
        clip_text = result.stdout.strip()
    except Exception as e:
        print(f"❌ Clipboard read error: {e}")
        return False

    if not clip_text:
        print("❌ Clipboard is empty")
        return False

    # Parse YAML
    try:
        new_config = yaml.safe_load(clip_text)
    except yaml.YAMLError as e:
        print(f"❌ Invalid YAML:\n{e}")
        return False

    # Validate structure
    ok, errors = _validate_config(new_config)
    if not ok:
        print("❌ Config validation failed:")
        for err in errors:
            print(f"   • {err}")
        return False

    # Show diff summary
    old_banks = len(config.get("banks", {}))
    new_banks = len(new_config.get("banks", {}))
    old_fb = len(config.get("midi_feedback", []))
    new_fb = len(new_config.get("midi_feedback", []))
    print(f"\n  Current: {old_banks} banks, {old_fb} feedback rules")
    print(f"  New:     {new_banks} banks, {new_fb} feedback rules")
    print(f"  MIDI OUT: {new_config.get('midi_out_port', '—')}")
    print(f"  MIDI IN:  {new_config.get('midi_in_port', '—')}")

    try:
        if not inquirer.confirm(message="⚠️  Overwrite current config with clipboard?",
                                default=False).execute():
            print("⏭️  Cancelled")
            return False
    except KeyboardInterrupt:
        return False

    config.clear()
    config.update(new_config)
    print("✅ Config replaced from clipboard (not saved yet — use 💾 Save)")
    return True
    print("─" * 50)
    print(f"  📦 Bank: {current_bank}  ({len(banks)} banks)")
    print(f"  🎹 OUT: {midi_out}")
    print(f"  📥 IN:  {midi_in}")
    print(f"  🔘 {n_buttons} buttons  │  📥 {n_feedback} feedback rules"
          f"  │  🖥️ {'ON' if se_on else 'OFF'}  │  🔧 {'ON' if kf_on else 'OFF'}")
    print("─" * 50)


def _config_submenu_feedback(config):
    """Feedback rules sub-menu."""
    rules = config.get("midi_feedback", [])
    while True:
        # Show current rules inline
        if rules:
            print(f"\n  📥 Feedback Rules ({len(rules)}):")
            for i, r in enumerate(rules):
                print(f"     {i+1}. {_fmt_match(r.get('match', {}))} → {_fmt_feedback_actions(r)}")
        else:
            print("\n  📥 No feedback rules yet.")

        try:
            action = inquirer.select(
                message="Feedback rules",
                choices=[
                    Choice("add", "➕ Add rule"),
                    Choice("edit", "✏️  Edit rule") if rules else Choice("edit", "✏️  Edit rule (none)"),
                    Choice("remove", "🗑️  Remove rule") if rules else Choice("remove", "🗑️  Remove rule (none)"),
                    Separator(),
                    Choice("back", "⬅️  Back"),
                ],
            ).execute()
        except KeyboardInterrupt:
            break

        if action == "back":
            break
        elif action == "add":
            add_feedback_rule(config)
            rules = config.get("midi_feedback", [])
        elif action == "edit" and rules:
            edit_feedback_rule(config)
        elif action == "remove" and rules:
            remove_feedback_rule(config)
            rules = config.get("midi_feedback", [])


def _edit_bank_vars(config, current_bank):
    """Edit variables for the current bank."""
    banks = config.get("banks", {})
    bank = banks.setdefault(current_bank, {"buttons": {}})
    v = bank.setdefault("vars", {})

    while True:
        if v:
            print(f"\n  📐 Variables for bank '{current_bank}':")
            for k, val in v.items():
                print(f"     {k} = {val}")
        else:
            print(f"\n  📐 No variables in bank '{current_bank}'.")

        try:
            action = inquirer.select(
                message="Bank variables",
                choices=[
                    Choice("add", "➕ Add / edit variable"),
                    Choice("remove", "🗑️  Remove variable") if v else Choice("remove", "🗑️  Remove (none)"),
                    Separator(),
                    Choice("back", "⬅️  Back"),
                ],
            ).execute()
        except KeyboardInterrupt:
            break

        if action == "back":
            break
        elif action == "add":
            try:
                name = inquirer.text(
                    message="Variable name (letters, digits, _)",
                    default="",
                    validate=lambda x: bool(re.match(r"^[a-zA-Z_]\w*$", x)),
                ).execute().strip()
                current_val = str(v.get(name, ""))
                val = inquirer.text(
                    message=f"Value for '{name}'",
                    default=current_val,
                ).execute().strip()
                # Auto-convert to int if possible
                try:
                    val = int(val)
                except ValueError:
                    pass
                v[name] = val
                print(f"  ✅ {name} = {val}")
            except KeyboardInterrupt:
                pass
        elif action == "remove" and v:
            try:
                key = inquirer.select(
                    message="Remove variable",
                    choices=[Choice(k, f"{k} = {val}") for k, val in v.items()],
                ).execute()
                del v[key]
                print(f"  ✅ Removed '{key}'")
            except KeyboardInterrupt:
                pass


def _edit_bank_scenes(config, current_bank):
    """Edit scenes (named presets) for the current bank."""
    banks = config.get("banks", {})
    bank = banks.setdefault(current_bank, {"buttons": {}})
    scenes = bank.setdefault("scenes", {})

    while True:
        if scenes:
            print(f"\n  🎬 Scenes for bank '{current_bank}':")
            for name, props in scenes.items():
                parts = []
                for k, v in props.items():
                    parts.append(f"{k}={v}")
                print(f"     {name}: {', '.join(parts)}")
        else:
            print(f"\n  🎬 No scenes in bank '{current_bank}'.")
        print("  Scenes define reusable screen/LED states (color, width, height, x, y).")
        print("  Reference in value maps as: scene:<name>")

        try:
            action = inquirer.select(
                message="Scenes",
                choices=[
                    Choice("add", "➕ Add / edit scene"),
                    *([ Choice("edit", "✏️  Edit scene") ] if scenes else []),
                    *([ Choice("remove", "🗑️  Remove scene") ] if scenes else []),
                    Separator(),
                    Choice("back", "⬅️  Back"),
                ],
            ).execute()
        except KeyboardInterrupt:
            break

        if action == "back":
            break
        elif action in ("add", "edit"):
            try:
                if action == "edit" and scenes:
                    name = inquirer.select(
                        message="Edit which scene?",
                        choices=[Choice(n, f"{n}: {', '.join(f'{k}={v}' for k,v in p.items())}")
                                 for n, p in scenes.items()],
                    ).execute()
                    existing = scenes[name]
                else:
                    name = inquirer.text(
                        message="Scene name",
                        default="",
                        validate=lambda x: bool(re.match(r"^[a-zA-Z_]\w*$", x)),
                    ).execute().strip()
                    existing = scenes.get(name, {})

                print(f"\n  Editing scene '{name}' — set properties (leave empty to skip)")
                if _bank_vars_hint:
                    print(f"  Vars: {', '.join(f'${k}={v}' for k,v in _bank_vars_hint.items())}  (Tab to complete)")
                props = {}

                # Build completer for scene properties
                _scene_comp = {}
                for k, v in _bank_vars_hint.items():
                    _scene_comp[f"${k}"] = None

                # Color
                d_color = existing.get("color", "")
                color_str = inquirer.text(
                    message="Color (#RRGGBB or $var, empty to skip)",
                    default=d_color,
                    completer=_color_completer(),
                ).execute().strip()
                if color_str:
                    props["color"] = color_str

                # Width
                d_w = str(existing.get("width", ""))
                w = inquirer.text(message="Width (empty to skip)", default=d_w,
                                  completer=_scene_comp if _scene_comp else None).execute().strip()
                if w:
                    try:
                        props["width"] = int(w)
                    except ValueError:
                        props["width"] = w

                # Height
                d_h = str(existing.get("height", ""))
                h = inquirer.text(message="Height (empty to skip)", default=d_h,
                                  completer=_scene_comp if _scene_comp else None).execute().strip()
                if h:
                    try:
                        props["height"] = int(h)
                    except ValueError:
                        props["height"] = h

                # X
                d_x = str(existing.get("x", ""))
                x = inquirer.text(message="X position (empty to skip)", default=d_x,
                                  completer=_scene_comp if _scene_comp else None).execute().strip()
                if x:
                    try:
                        props["x"] = int(x)
                    except ValueError:
                        props["x"] = x

                # Y
                d_y = str(existing.get("y", ""))
                y = inquirer.text(message="Y position (empty to skip)", default=d_y,
                                  completer=_scene_comp if _scene_comp else None).execute().strip()
                if y:
                    try:
                        props["y"] = int(y)
                    except ValueError:
                        props["y"] = y

                if props:
                    scenes[name] = props
                    parts = ", ".join(f"{k}={v}" for k, v in props.items())
                    print(f"  ✅ Scene '{name}': {parts}")
                else:
                    print("  ❌ No properties set — scene not saved")
            except KeyboardInterrupt:
                pass
        elif action == "remove" and scenes:
            try:
                key = inquirer.select(
                    message="Remove which scene?",
                    choices=[Choice(n, n) for n in scenes],
                ).execute()
                del scenes[key]
                print(f"  ✅ Removed scene '{key}'")
            except KeyboardInterrupt:
                pass


def _config_submenu_banks(config, current_bank):
    """Banks sub-menu. Returns (potentially changed) current_bank."""
    while True:
        banks = config.get("banks", {})
        bank_list = ", ".join(f"[{b}]" if b == current_bank else b for b in banks)
        bank_vars = banks.get(current_bank, {}).get("vars", {})
        bank_scenes = banks.get(current_bank, {}).get("scenes", {})
        n_vars = len(bank_vars)
        n_scenes = len(bank_scenes)
        print(f"\n  📦 Banks: {bank_list}")
        if bank_vars:
            print(f"  📐 Vars: {', '.join(f'{k}={v}' for k, v in bank_vars.items())}")
        if bank_scenes:
            print(f"  🎬 Scenes: {', '.join(bank_scenes.keys())}")

        try:
            action = inquirer.select(
                message="Banks",
                choices=[
                    Choice("select", "📦 Switch bank"),
                    Choice("vars", f"📐 Bank variables ({n_vars})"),
                    Choice("scenes", f"🎬 Scenes ({n_scenes})"),
                    Choice("create", "➕ Create bank"),
                    Choice("rename", "✏️  Rename bank"),
                    Choice("delete", "🗑️  Delete bank"),
                    Separator(),
                    Choice("back", "⬅️  Back"),
                ],
            ).execute()
        except KeyboardInterrupt:
            break

        if action == "back":
            break
        elif action == "select":
            current_bank = select_bank(config)
        elif action == "vars":
            _edit_bank_vars(config, current_bank)
        elif action == "scenes":
            _edit_bank_scenes(config, current_bank)
        elif action == "create":
            create_bank(config)
        elif action == "rename":
            current_bank = rename_bank(config, current_bank)
        elif action == "delete":
            current_bank = delete_bank(config, current_bank)

    return current_bank


def _config_submenu_midi_ports(config):
    """MIDI ports sub-menu."""
    while True:
        midi_out = config.get("midi_out_port", config.get("midi_port", "—"))
        midi_in = config.get("midi_in_port", "—")
        print(f"\n  🎹 OUT: {midi_out}")
        print(f"  📥 IN:  {midi_in}")

        try:
            action = inquirer.select(
                message="MIDI ports",
                choices=[
                    Choice("out", "🎹 MIDI OUT port"),
                    Choice("in", "📥 MIDI IN port (feedback)"),
                    Separator(),
                    Choice("back", "⬅️  Back"),
                ],
            ).execute()
        except KeyboardInterrupt:
            break

        if action == "back":
            break
        elif action == "out":
            select_midi_out_port(config)
        elif action == "in":
            select_midi_in_port(config)


def config_editor(live_instance=None):
    """Interactive v2 config editor.

    If live_instance is a running SayoMIDI, saves will hot-reload into it.
    """
    if not CONFIG_FILE.exists():
        print(f"❌ No config file at {CONFIG_FILE}")
        try:
            if inquirer.confirm(message="Create one now with the setup wizard?", default=True).execute():
                setup_wizard()
            else:
                return
        except KeyboardInterrupt:
            return
        if not CONFIG_FILE.exists():
            return

    with open(CONFIG_FILE, "r") as f:
        config = yaml.safe_load(f) or {}

    # Ensure banks structure
    if "banks" not in config:
        config["banks"] = {"default": {
            "buttons": config.pop("buttons", {}),
        }}
        config.setdefault("default_bank", "default")

    current_bank = config.get("default_bank", list(config["banks"].keys())[0])

    if live_instance:
        print("\n" + "=" * 50)
        print("  🎮 SAYO-MIDI CONFIG EDITOR  (🟢 engine running)")
        print("=" * 50)
    else:
        print("\n" + "=" * 50)
        print("  🎮 SAYO-MIDI CONFIG EDITOR")
        print("=" * 50)

    while True:
        # Update global hints so color/value_map prompts can show vars & scenes
        _bank_vars_hint.clear()
        _bank_scenes_hint.clear()
        bank_data = config.get("banks", {}).get(current_bank, {})
        _bank_vars_hint.update(bank_data.get("vars", {}))
        _bank_scenes_hint.update(bank_data.get("scenes", {}))

        _config_status(config, current_bank)

        n_buttons = len(config.get("banks", {}).get(current_bank, {}).get("buttons", {}))
        n_feedback = len(config.get("midi_feedback", []))
        n_banks = len(config.get("banks", {}))

        try:
            action = inquirer.select(
                message="What do you want to do?",
                choices=[
                    Choice("quick_wire", "⚡ Quick Wire (button + feedback)"),
                    Choice("add_button", f"🔘 Buttons ({n_buttons} mapped)"),
                    Choice("feedback", f"📥 Feedback rules ({n_feedback})"),
                    Choice("ports", "🎹 MIDI ports"),
                    Choice("screen", "🖥️  Screen & LEDs"),
                    Choice("banks", f"📦 Banks ({n_banks})"),
                    Choice("knob", "🔧 Knob fix"),
                    Separator(),
                    Choice("view", "📋 View full config"),
                    Choice("test", "🧪 Test device"),
                    Choice("midi_mon", "📥 Monitor MIDI IN"),
                    Separator(),
                    Choice("clip_copy", "📋 Copy config to clipboard"),
                    Choice("clip_paste", "📋 Paste config from clipboard"),
                    Separator(),
                    *([
                        Choice("save_apply", "💾 Save & apply live"),
                        Choice("save_exit", "💾 Save & exit editor"),
                        Choice("exit", "🚪 Exit editor"),
                    ] if live_instance else [
                        Choice("save", "💾 Save and exit"),
                        Choice("exit", "🚪 Exit without saving"),
                    ]),
                ],
                default="quick_wire",
            ).execute()
        except KeyboardInterrupt:
            if live_instance:
                print("\n🚪 Editor closed. Engine still running.")
            else:
                print("\n👋 Exiting without saving.")
            break

        try:
            if action == "quick_wire":
                quick_wire(config, current_bank)
            elif action == "add_button":
                _config_submenu_buttons(config, current_bank)
            elif action == "feedback":
                _config_submenu_feedback(config)
            elif action == "ports":
                _config_submenu_midi_ports(config)
            elif action == "screen":
                edit_screen_layout(config)
            elif action == "banks":
                current_bank = _config_submenu_banks(config, current_bank)
            elif action == "knob":
                edit_knob_fix(config)
            elif action == "view":
                view_config(config, current_bank)
            elif action == "test":
                test_device()
            elif action == "midi_mon":
                monitor_midi_in(config)
            elif action == "clip_copy":
                _copy_config_to_clipboard(config)
            elif action == "clip_paste":
                if _paste_config_from_clipboard(config):
                    # Re-derive current_bank from potentially new config
                    current_bank = config.get("default_bank",
                                              list(config.get("banks", {"default": {}}).keys())[0])
            elif action == "save":
                with open(CONFIG_FILE, "w") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
                print(f"\n✅ Saved to {CONFIG_FILE}")
                break
            elif action == "save_apply":
                with open(CONFIG_FILE, "w") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
                print(f"\n✅ Saved to {CONFIG_FILE}")
                if live_instance:
                    warnings = live_instance.reload_config()
                    print("🔄 Config reloaded into running engine")
                    if warnings:
                        for w in warnings:
                            print(f"  ⚠️  {w}")
                    else:
                        print("  ✅ All changes applied live")
            elif action == "save_exit":
                with open(CONFIG_FILE, "w") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
                print(f"\n✅ Saved to {CONFIG_FILE}")
                if live_instance:
                    warnings = live_instance.reload_config()
                    print("🔄 Config reloaded into running engine")
                    for w in warnings:
                        print(f"  ⚠️  {w}")
                print("🚪 Editor closed. Engine still running.")
                break
            elif action == "exit":
                if live_instance:
                    print("\n🚪 Editor closed. Engine still running.")
                    break
                try:
                    if inquirer.confirm(message="Exit without saving?", default=False).execute():
                        print("\n👋 Bye.")
                        break
                except KeyboardInterrupt:
                    pass
        except KeyboardInterrupt:
            print("\n⬆️  Back to menu")
            continue


# ═══════════════════════════════════════════════════════════════
#  Setup wizard
# ═══════════════════════════════════════════════════════════════

def setup_wizard():
    """Create a new config file interactively."""
    print("\n" + "=" * 60)
    print("🎮 SAYO-MIDI — SETUP WIZARD")
    print("=" * 60 + "\n")

    # 1. SayoDevice
    try:
        if not SayoDevice:
            print("❌ sayodevice library not installed!")
            return
        sayo_dev = SayoDevice.open()
        info = sayo_dev.get_info()
        print(f"✅ SayoDevice connected (FW v{info.firmware_version})")
        print(f"   Inputs: {', '.join(BUTTON_NAMES.keys())}\n")
    except Exception as e:
        print(f"❌ SayoDevice error: {e}")
        return

    # 2. MIDI OUT
    out_ports = mido.get_output_names()  # pyright: ignore[reportAttributeAccessIssue]
    if not out_ports:
        print("❌ No MIDI output ports! Create a virtual port first.")
        return
    try:
        midi_out = inquirer.select(message="🎹 MIDI OUT port", choices=out_ports).execute()
    except KeyboardInterrupt:
        return
    print(f"✅ MIDI OUT: {midi_out}\n")

    # 3. MIDI IN (optional)
    in_ports = mido.get_input_names()  # pyright: ignore[reportAttributeAccessIssue]
    midi_in = None
    if in_ports:
        try:
            wire_in = inquirer.confirm(message="📥 Configure MIDI IN port for feedback?", default=True).execute()
            if wire_in:
                midi_in = inquirer.select(message="📥 MIDI IN port", choices=in_ports).execute()
                print(f"✅ MIDI IN: {midi_in}\n")
        except KeyboardInterrupt:
            pass

    # 4. Quick-map buttons
    print("=" * 60)
    print("📝 MAP BUTTONS (press Ctrl+C when done)")
    print("=" * 60 + "\n")

    button_map = {}
    seen = set()

    try:
        test_port = mido.open_output(midi_out)  # pyright: ignore[reportAttributeAccessIssue]
        print("🎧 Listening for SayoDevice buttons... (b = done)\n")

        detected = [None]  # mutable container for closure
        det_event = threading.Event()

        def on_btn(event):
            if event.pressed:
                btn = event.button
                if btn in BUTTON_NAMES and btn not in seen:
                    detected[0] = btn
                    det_event.set()

        listener = DeviceListener(sayo_dev, poll_interval_ms=10)
        listener.on_button(on_btn)
        listener.start()

        while True:
            while not det_event.is_set():
                if msvcrt.kbhit():
                    key = msvcrt.getch()
                    if key in (b'b', b'B'):
                        print("  ⬅️  Done mapping")
                        raise KeyboardInterrupt
                det_event.wait(timeout=0.05)
            det_event.clear()
            code = detected[0]
            detected[0] = None
            if code in seen:
                continue
            seen.add(code)
            friendly = BUTTON_NAMES.get(code, code)
            print(f"\n🔘 Detected: {code} ({friendly})")

            map_type = inquirer.select(
                message="Map to",
                choices=[
                    Choice("note", "🎵 Note (momentary)"),
                    Choice("cc", "🎛️  CC"),
                    Choice("skip", "⏭️  Skip"),
                ],
            ).execute()

            if map_type == "note":
                note = _ask_int("Note (0-127)", 60, 0, 127)
                vel = _ask_int("Velocity (0-127)", 127, 0, 127)
                ch = _ask_int("Channel (0-15)", 0, 0, 15)
                button_map[code] = {
                    "on_press": {"type": "note_on", "note": note, "velocity": vel, "channel": ch},
                    "on_release": {"type": "note_off", "note": note, "channel": ch},
                }
                test_port.send(mido.Message("note_on", note=note, velocity=vel, channel=ch))
                time.sleep(0.1)
                test_port.send(mido.Message("note_off", note=note, channel=ch))
                print(f"✅ → Note {note} ch{ch}")
            elif map_type == "cc":
                cc = _ask_int("CC (0-127)", 1, 0, 127)
                val = _ask_int("Value (0-127)", 127, 0, 127)
                ch = _ask_int("Channel (0-15)", 0, 0, 15)
                button_map[code] = {
                    "on_press": {"type": "cc", "cc": cc, "value": val, "channel": ch},
                    "on_release": {"type": "cc", "cc": cc, "value": 0, "channel": ch},
                }
                test_port.send(mido.Message("control_change", control=cc, value=val, channel=ch))
                print(f"✅ → CC {cc}={val} ch{ch}")
            else:
                print("⏭️  Skipped")

        listener.stop()  # pragma: no cover
        test_port.close()  # pragma: no cover
    except KeyboardInterrupt:
        print("\n\n✅ Mapping complete!")
        if 'listener' in dir():
            listener.stop()
        sayo_dev.close()

    # 5. Build config
    config = {
        "midi_out_port": midi_out,
        "device_name": "SayoDevice O3C",
        "default_bank": "default",
        "banks": {
            "default": {
                "buttons": button_map,
            }
        },
        "screen_elements": {"enabled": False, "elements": {}},
        "midi_feedback": [],
        "knob_fix": {
            "enabled": False,
            "left_button": "knob_left",
            "right_button": "knob_right",
            "debounce_ms": 50,
            "test_mode": False,
            "left_midi": {"type": "note", "note": 62, "velocity": 127, "channel": 1},
            "right_midi": {"type": "note", "note": 61, "velocity": 127, "channel": 1},
        },
    }
    if midi_in:
        config["midi_in_port"] = midi_in

    with open(CONFIG_FILE, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\n✅ Configuration saved to: {CONFIG_FILE}")
    print("\n💡 Run:  python sayo_midi.py")
    print("💡 Edit: python sayo_midi.py --config")
    print("💡 Use --config → ⚡ Quick Wire to map buttons + visual feedback together.\n")


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main():
    global CONFIG_FILE

    parser = argparse.ArgumentParser(description="🎮 SayoDevice O3C → MIDI")
    parser.add_argument("--run", action="store_true",
                        help="Run the MIDI engine")
    parser.add_argument("--setup", action="store_true",
                        help="Run setup wizard to create a new config")
    parser.add_argument("--config", action="store_true",
                        help="Open interactive config editor")
    parser.add_argument("--file", type=str, default=str(CONFIG_FILE),
                        help="Path to config file")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show all button events and MIDI IN messages")
    parser.add_argument("--list-ports", action="store_true",
                        help="List available MIDI ports and exit")

    args = parser.parse_args()

    if args.list_ports:
        print("MIDI OUT:", mido.get_output_names())  # pyright: ignore[reportAttributeAccessIssue]
        print("MIDI IN: ", mido.get_input_names())  # pyright: ignore[reportAttributeAccessIssue]
        return

    if args.file != str(CONFIG_FILE):
        CONFIG_FILE = Path(args.file)

    if args.setup:
        setup_wizard()
        return

    if args.run and args.config:
        # Engine in background thread + config editor in foreground
        gm = SayoMIDI(CONFIG_FILE, verbose=args.verbose)
        engine_thread = threading.Thread(target=gm.run, daemon=True)
        engine_thread.start()
        time.sleep(0.5)  # let engine connect and print status
        print("\n" + "-" * 50)
        print("  Config editor ready. Engine running in background.")
        print("  Button mappings, feedback rules, screen elements,")
        print("  bank vars and knob fix are applied live on save.")
        print("  MIDI port changes require a restart.")
        print("-" * 50)
        try:
            config_editor(live_instance=gm)
        except KeyboardInterrupt:
            pass
        # After editor exits, keep engine running until Ctrl+C
        if gm.running:
            print("\n🎮 MIDI engine still running. Ctrl+C to stop.")
            try:
                while gm.running:
                    time.sleep(0.1)
            except KeyboardInterrupt:
                print("\n\n👋 Shutting down...")
                gm.stop()
                time.sleep(0.3)  # let cleanup finish
        return

    if args.config:
        config_editor()
        return

    # Default or --run: run the engine
    gm = SayoMIDI(CONFIG_FILE, verbose=args.verbose)
    gm.run()


if __name__ == "__main__":
    main()

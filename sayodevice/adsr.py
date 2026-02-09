"""
SayoDevice O3C - ADSR envelope generator.

Provides configurable Attack-Decay-Sustain-Release envelopes with
multiple curve types. Each EnvelopeGenerator tracks its own gate
state and produces amplitude values from 0.0 to 1.0.

Usage::

    from sayodevice.adsr import ADSREnvelope, EnvelopeGenerator, CurveType

    env = ADSREnvelope(
        attack_ms=50, decay_ms=100, sustain=0.8, release_ms=200,
        attack_curve=CurveType.LINEAR,
        decay_curve=CurveType.EXPONENTIAL,
        release_curve=CurveType.EXPONENTIAL,
    )

    gen = EnvelopeGenerator(env)
    gen.gate_on()
    # ... poll gen.get_value() in your loop ...
    gen.gate_off()
    # ... continue polling until gen.is_active is False ...
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum


class CurveType(Enum):
    """Envelope curve shape."""
    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    LOGARITHMIC = "logarithmic"


# Curve shaping constants
_EXP_K = 5.0     # steepness for exponential curves
_LOG_K = 10.0    # steepness for logarithmic curves


def _apply_curve(t: float, curve: CurveType) -> float:
    """Apply curve shaping to a normalized time value (0.0 to 1.0).

    Args:
        t: Normalized time, clamped to [0, 1].
        curve: Which curve shape to apply.

    Returns:
        Shaped value in [0, 1].
    """
    t = max(0.0, min(1.0, t))
    if curve == CurveType.LINEAR:
        return t
    elif curve == CurveType.EXPONENTIAL:
        # Slow start, fast end
        return (math.exp(_EXP_K * t) - 1.0) / (math.exp(_EXP_K) - 1.0)
    elif curve == CurveType.LOGARITHMIC:
        # Fast start, slow end
        return math.log(1.0 + _LOG_K * t) / math.log(1.0 + _LOG_K)
    return t


def _apply_curve_inverted(t: float, curve: CurveType) -> float:
    """Apply inverted curve (1.0 at t=0, 0.0 at t=1) for decay/release.

    Args:
        t: Normalized time (0.0 to 1.0).
        curve: Curve shape.

    Returns:
        Shaped value in [0, 1], decreasing from 1 to 0.
    """
    return 1.0 - _apply_curve(t, curve)


@dataclass
class ADSREnvelope:
    """ADSR envelope configuration.

    Attributes:
        attack_ms: Attack time in milliseconds (0-2000).
        decay_ms: Decay time in milliseconds (0-2000).
        sustain: Sustain level (0.0-1.0).
        release_ms: Release time in milliseconds (0-5000).
        attack_curve: Curve shape for attack phase.
        decay_curve: Curve shape for decay phase.
        release_curve: Curve shape for release phase.
    """
    attack_ms: float = 50.0
    decay_ms: float = 100.0
    sustain: float = 0.8
    release_ms: float = 200.0
    attack_curve: CurveType = CurveType.LINEAR
    decay_curve: CurveType = CurveType.EXPONENTIAL
    release_curve: CurveType = CurveType.EXPONENTIAL

    def to_dict(self) -> dict:
        return {
            'attack_ms': self.attack_ms,
            'decay_ms': self.decay_ms,
            'sustain': self.sustain,
            'release_ms': self.release_ms,
            'attack_curve': self.attack_curve.value,
            'decay_curve': self.decay_curve.value,
            'release_curve': self.release_curve.value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ADSREnvelope:
        return cls(
            attack_ms=d.get('attack_ms', 50.0),
            decay_ms=d.get('decay_ms', 100.0),
            sustain=d.get('sustain', 0.8),
            release_ms=d.get('release_ms', 200.0),
            attack_curve=CurveType(d.get('attack_curve', 'linear')),
            decay_curve=CurveType(d.get('decay_curve', 'exponential')),
            release_curve=CurveType(d.get('release_curve', 'exponential')),
        )


class EnvelopeStage(Enum):
    """Current stage of the envelope generator."""
    IDLE = "idle"
    ATTACK = "attack"
    DECAY = "decay"
    SUSTAIN = "sustain"
    RELEASE = "release"


class EnvelopeGenerator:
    """Per-voice envelope generator that tracks gate state.

    Call gate_on() to start the envelope, gate_off() to begin release.
    Poll get_value() in your main loop to get the current amplitude.

    Args:
        envelope: The ADSR configuration to use.

    Example::

        gen = EnvelopeGenerator(ADSREnvelope(attack_ms=10, sustain=0.7))
        gen.gate_on()
        while gen.is_active:
            val = gen.get_value()  # 0.0 to 1.0
            velocity = int(val * 127)
            time.sleep(0.001)
        gen.gate_off()
    """

    def __init__(self, envelope: ADSREnvelope):
        self.envelope = envelope
        self._stage = EnvelopeStage.IDLE
        self._stage_start: float = 0.0
        self._release_level: float = 0.0  # amplitude when release was triggered
        self._current_value: float = 0.0

    def gate_on(self) -> None:
        """Trigger the attack phase. Resets the envelope from the start."""
        self._stage = EnvelopeStage.ATTACK
        self._stage_start = time.monotonic()
        self._current_value = 0.0

    def gate_off(self) -> None:
        """Begin the release phase from whatever the current level is."""
        if self._stage == EnvelopeStage.IDLE:
            return
        self._release_level = self._current_value
        self._stage = EnvelopeStage.RELEASE
        self._stage_start = time.monotonic()

    def get_value(self) -> float:
        """Calculate and return the current envelope amplitude (0.0 to 1.0).

        Should be called frequently (e.g., every frame or every few ms)
        to get smooth envelope tracking.
        """
        if self._stage == EnvelopeStage.IDLE:
            self._current_value = 0.0
            return 0.0

        now = time.monotonic()
        elapsed_ms = (now - self._stage_start) * 1000.0
        env = self.envelope

        if self._stage == EnvelopeStage.ATTACK:
            if env.attack_ms <= 0:
                self._current_value = 1.0
                self._stage = EnvelopeStage.DECAY
                self._stage_start = now
            else:
                t = elapsed_ms / env.attack_ms
                if t >= 1.0:
                    self._current_value = 1.0
                    self._stage = EnvelopeStage.DECAY
                    self._stage_start = now
                else:
                    self._current_value = _apply_curve(t, env.attack_curve)

        if self._stage == EnvelopeStage.DECAY:
            if env.decay_ms <= 0:
                self._current_value = env.sustain
                self._stage = EnvelopeStage.SUSTAIN
                self._stage_start = now
            else:
                elapsed_ms = (now - self._stage_start) * 1000.0
                t = elapsed_ms / env.decay_ms
                if t >= 1.0:
                    self._current_value = env.sustain
                    self._stage = EnvelopeStage.SUSTAIN
                    self._stage_start = now
                else:
                    # Decay from 1.0 down to sustain
                    shaped = _apply_curve_inverted(t, env.decay_curve)
                    self._current_value = env.sustain + (1.0 - env.sustain) * shaped

        if self._stage == EnvelopeStage.SUSTAIN:
            self._current_value = env.sustain

        if self._stage == EnvelopeStage.RELEASE:
            if env.release_ms <= 0:
                self._current_value = 0.0
                self._stage = EnvelopeStage.IDLE
            else:
                elapsed_ms = (now - self._stage_start) * 1000.0
                t = elapsed_ms / env.release_ms
                if t >= 1.0:
                    self._current_value = 0.0
                    self._stage = EnvelopeStage.IDLE
                else:
                    # Release from release_level down to 0
                    shaped = _apply_curve_inverted(t, env.release_curve)
                    self._current_value = self._release_level * shaped

        return max(0.0, min(1.0, self._current_value))

    @property
    def is_active(self) -> bool:
        """True if the envelope is producing non-zero output."""
        return self._stage != EnvelopeStage.IDLE

    @property
    def stage(self) -> str:
        """Current envelope stage name."""
        return self._stage.value

    def reset(self) -> None:
        """Reset to idle state."""
        self._stage = EnvelopeStage.IDLE
        self._current_value = 0.0
        self._release_level = 0.0


# ============================================================
# Visualization helpers (for rendering ADSR on device screen)
# ============================================================

def envelope_to_bars(env: ADSREnvelope, height: int = 80) -> list[int]:
    """Convert ADSR parameters to bar heights for visualization.

    Returns 4 bar heights [A, D, S, R] scaled to the given height.
    A, D, R are scaled by time (max 2000ms for A/D, 5000ms for R).
    S is scaled by level (0.0-1.0).
    """
    a_bar = int((min(env.attack_ms, 2000) / 2000) * height)
    d_bar = int((min(env.decay_ms, 2000) / 2000) * height)
    s_bar = int(env.sustain * height)
    r_bar = int((min(env.release_ms, 5000) / 5000) * height)
    return [a_bar, d_bar, s_bar, r_bar]


# ADSR bar colors
ADSR_COLORS = {
    'A': '#00FF00',  # green
    'D': '#FFFF00',  # yellow
    'S': '#3399FF',  # blue
    'R': '#FF3300',  # red
}

ADSR_COLORS_DIM = {
    'A': '#005500',
    'D': '#555500',
    'S': '#113355',
    'R': '#551100',
}

ADSR_LABELS = ['A', 'D', 'S', 'R']

ADSR_PARAM_NAMES = ['attack_ms', 'decay_ms', 'sustain', 'release_ms']

ADSR_PARAM_RANGES = {
    'attack_ms': (0, 2000, 50),    # min, max, step
    'decay_ms': (0, 2000, 50),
    'sustain': (0.0, 1.0, 0.05),
    'release_ms': (0, 5000, 100),
}

CURVE_NAMES = ['attack_curve', 'decay_curve', 'release_curve']
CURVE_TYPES = list(CurveType)

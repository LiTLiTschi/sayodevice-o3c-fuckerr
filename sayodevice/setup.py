"""
SayoDevice O3C - Named device setups (save/load/apply).

Define reusable device configurations as named presets::

    from sayodevice import DeviceSetup, ScreenElement, save_setup, load_setup

    setup = DeviceSetup(name="seq-gate", screen_elements=[
        ScreenElement(x=0, y=0, width=160, height=80, color="#1295FF", element_index=14),
        ScreenElement(x=0, y=0, width=40, height=40, color="#000000", element_index=15),
    ])
    save_setup(setup)

    with SayoDevice.open() as dev:
        load_setup("seq-gate").apply(dev)
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

SETUPS_DIR = Path.home() / ".sayodevice" / "setups"


# ============================================================
# Data classes
# ============================================================

@dataclass
class ScreenElement:
    """One screen element to be sent via SCREEN_MAIN (CMD 0x22).

    Args:
        x: X-Position in Pixeln.
        y: Y-Position in Pixeln.
        width: Breite in Pixeln.
        height: Höhe in Pixeln.
        color: '#RRGGBB' String oder RGB565 int.
        element_type: 1 = Pure Color.
        element_index: Layer / FN index.
    """
    x: int = 0
    y: int = 0
    width: int = 40
    height: int = 40
    color: str = "#FFFFFF"
    element_type: int = 1
    element_index: int = 0x0F


@dataclass
class KeyConfig:
    """One key configuration to be sent via KEY (CMD 0x10).

    Args:
        arg0: V0 script parameter (0-255).
        arg1: V1 parameter (not yet confirmed).
        arg2: V2 parameter (not yet confirmed).
        arg3: V3 parameter (not yet confirmed).
        key_index: Which key to configure.
    """
    arg0: int = 0
    arg1: int | None = None
    arg2: int | None = None
    arg3: int | None = None
    key_index: int = 0


@dataclass
class DeviceSetup:
    """A named collection of screen elements and key configs.

    Create a setup, save it, and apply it to a device::

        setup = DeviceSetup(
            name="my-setup",
            screen_elements=[ScreenElement(color="#FF0000", element_index=14)],
        )
        save_setup(setup)
        setup.apply(dev)
    """
    name: str = ""
    description: str = ""
    created: float = 0.0
    screen_elements: list[ScreenElement] = field(default_factory=list)
    key_configs: list[KeyConfig] = field(default_factory=list)
    save_to_flash: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "name": self.name,
            "description": self.description,
            "created": self.created or time.time(),
            "save_to_flash": self.save_to_flash,
            "screen_elements": [asdict(el) for el in self.screen_elements],
            "key_configs": [asdict(kc) for kc in self.key_configs],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeviceSetup:
        """Deserialize from a dict (e.g. loaded from JSON)."""
        return cls(
            name=d.get("name", ""),
            description=d.get("description", ""),
            created=d.get("created", 0.0),
            save_to_flash=d.get("save_to_flash", False),
            screen_elements=[
                ScreenElement(**el) for el in d.get("screen_elements", [])
            ],
            key_configs=[
                KeyConfig(**kc) for kc in d.get("key_configs", [])
            ],
        )

    def apply(self, device) -> None:
        """Send all screen elements and key configs to the device.

        Screen elements are sent without refresh, then a single
        ``refresh_display()`` is called at the end. Key configs are
        sent without save; if ``save_to_flash`` is True, a single
        ``device.save()`` is called at the very end.

        Args:
            device: An opened SayoDevice instance.
        """
        for el in self.screen_elements:
            device.set_screen_element(
                x=el.x,
                y=el.y,
                width=el.width,
                height=el.height,
                color=el.color,
                element_type=el.element_type,
                element_index=el.element_index,
                refresh=False,
            )

        for kc in self.key_configs:
            device.set_key_config(
                arg0=kc.arg0,
                arg1=kc.arg1,
                arg2=kc.arg2,
                arg3=kc.arg3,
                key_index=kc.key_index,
                save=False,
            )

        if self.screen_elements:
            device.refresh_display()

        if self.save_to_flash:
            device.save()


# ============================================================
# CRUD functions
# ============================================================

def _sanitize_name(name: str) -> str:
    """Sanitize setup name to a safe filename component."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip())
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    if not sanitized:
        raise ValueError(f"Invalid setup name: {name!r}")
    return sanitized


def _setup_path(name: str) -> Path:
    """Return the file path for a named setup."""
    return SETUPS_DIR / f"{_sanitize_name(name)}.json"


def save_setup(setup: DeviceSetup) -> Path:
    """Save a DeviceSetup to ``~/.sayodevice/setups/<name>.json``.

    Args:
        setup: The setup to save. ``setup.name`` must be non-empty.

    Returns:
        Path to the saved JSON file.
    """
    if not setup.name:
        raise ValueError("Setup must have a name")
    if not setup.created:
        setup.created = time.time()
    SETUPS_DIR.mkdir(parents=True, exist_ok=True)
    path = _setup_path(setup.name)
    path.write_text(json.dumps(setup.to_dict(), indent=2))
    return path


def load_setup(name: str) -> DeviceSetup:
    """Load a named setup from disk.

    Args:
        name: Setup name (without .json extension).

    Returns:
        DeviceSetup instance.

    Raises:
        FileNotFoundError: If the setup doesn't exist.
    """
    path = _setup_path(name)
    if not path.exists():
        raise FileNotFoundError(f"Setup not found: {path}")
    data = json.loads(path.read_text())
    return DeviceSetup.from_dict(data)


def list_setups() -> list[str]:
    """List all saved setup names, sorted alphabetically."""
    if not SETUPS_DIR.exists():
        return []
    return sorted(p.stem for p in SETUPS_DIR.glob("*.json"))


def delete_setup(name: str) -> bool:
    """Delete a saved setup.

    Returns:
        True if deleted, False if not found.
    """
    path = _setup_path(name)
    if path.exists():
        path.unlink()
        return True
    return False

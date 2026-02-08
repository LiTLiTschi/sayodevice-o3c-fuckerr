"""
SayoDevice O3C - Event listener for input detection.

Polls the device in a background thread and fires callbacks
when state changes are detected (FN layer switches, key status, etc.).

Usage::

    from sayodevice import SayoDevice, DeviceListener

    with SayoDevice.open() as dev:
        listener = DeviceListener(dev)
        listener.on_fn_change(lambda e: print(f"FN: {e.old_fn} -> {e.new_fn}"))
        listener.on_raw_packet(lambda e: print(f"Raw: {e.data.hex(' ')}"))
        listener.start()

        # ... your main loop ...

        listener.stop()

Manual polling (no background thread)::

    listener = DeviceListener(dev)
    while True:
        events = listener.poll()
        for event in events:
            print(event)
        time.sleep(0.05)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .device import SayoDevice, DeviceInfo


# ============================================================
# Event types
# ============================================================

@dataclass
class FnChangeEvent:
    """Fired when the FN layer changes."""
    timestamp: float
    old_fn: int
    new_fn: int

    def __str__(self) -> str:
        return f"FnChange({self.old_fn} -> {self.new_fn})"


@dataclass
class InfoUpdateEvent:
    """Fired on every successful info poll (for tracking uptime, battery, etc.)."""
    timestamp: float
    info: DeviceInfo

    def __str__(self) -> str:
        return f"InfoUpdate(fn={self.info.fn}, battery={self.info.battery})"


@dataclass
class RawPacketEvent:
    """Fired when an unsolicited packet is received from the device."""
    timestamp: float
    data: bytes

    def __str__(self) -> str:
        preview = self.data[:16].hex(" ") if self.data else "(empty)"
        return f"RawPacket({len(self.data)}B: {preview})"


# Union type for all events
DeviceEvent = FnChangeEvent | InfoUpdateEvent | RawPacketEvent


# ============================================================
# Listener
# ============================================================

class DeviceListener:
    """
    Background listener for SayoDevice state changes.

    Polls ``get_info()`` at a configurable interval and detects:
    - FN layer changes (``on_fn_change``)
    - General info updates (``on_info_update``)
    - Unsolicited raw packets (``on_raw_packet``)

    Args:
        device: An opened SayoDevice instance.
        poll_interval_ms: How often to poll in milliseconds (default: 100).
        read_unsolicited: Also call ``receive()`` to catch unsolicited packets
            between polls (default: True).

    Example::

        listener = DeviceListener(dev, poll_interval_ms=50)
        listener.on_fn_change(lambda e: print(e))
        listener.start()
    """

    def __init__(
        self,
        device: SayoDevice,
        poll_interval_ms: int = 100,
        read_unsolicited: bool = True,
    ):
        self._device = device
        self._poll_interval = poll_interval_ms / 1000.0
        self._read_unsolicited = read_unsolicited

        # State tracking
        self._last_fn: int | None = None
        self._last_info: DeviceInfo | None = None

        # Callbacks
        self._fn_callbacks: list[Callable[[FnChangeEvent], None]] = []
        self._info_callbacks: list[Callable[[InfoUpdateEvent], None]] = []
        self._raw_callbacks: list[Callable[[RawPacketEvent], None]] = []

        # Event queue for manual polling mode
        self._event_queue: list[DeviceEvent] = []
        self._queue_lock = threading.Lock()

        # Thread control
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ---- Callback registration ----

    def on_fn_change(self, callback: Callable[[FnChangeEvent], None]) -> None:
        """Register a callback for FN layer changes."""
        self._fn_callbacks.append(callback)

    def on_info_update(self, callback: Callable[[InfoUpdateEvent], None]) -> None:
        """Register a callback for info poll updates."""
        self._info_callbacks.append(callback)

    def on_raw_packet(self, callback: Callable[[RawPacketEvent], None]) -> None:
        """Register a callback for unsolicited raw packets."""
        self._raw_callbacks.append(callback)

    # ---- Background thread mode ----

    def start(self) -> None:
        """Start the background polling thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- Manual polling mode ----

    def poll(self) -> list[DeviceEvent]:
        """
        Poll once and return any events detected.

        Use this instead of start()/stop() if you want to control
        the polling loop yourself.

        Returns:
            List of events detected in this poll cycle.
        """
        events: list[DeviceEvent] = []
        self._poll_once(events)
        return events

    def drain_events(self) -> list[DeviceEvent]:
        """
        Drain all queued events from the background thread.

        When using start(), events are still queued even if no callbacks
        are registered. Call this to retrieve them.
        """
        with self._queue_lock:
            events = self._event_queue.copy()
            self._event_queue.clear()
        return events

    # ---- Internal ----

    def _poll_loop(self) -> None:
        """Background polling loop."""
        while not self._stop_event.is_set():
            events: list[DeviceEvent] = []
            try:
                self._poll_once(events)
            except Exception:
                pass  # Device disconnected or read error — keep trying

            # Queue events for drain_events()
            if events:
                with self._queue_lock:
                    self._event_queue.extend(events)

            self._stop_event.wait(self._poll_interval)

    def _poll_once(self, events: list[DeviceEvent]) -> None:
        """Single poll cycle: query info + read unsolicited."""
        now = time.time()

        # 1. Poll get_info() for FN state
        try:
            info = self._device.get_info()
            self._last_info = info

            # Info update event
            evt_info = InfoUpdateEvent(timestamp=now, info=info)
            events.append(evt_info)
            for cb in self._info_callbacks:
                cb(evt_info)

            # FN change detection
            if self._last_fn is not None and info.fn != self._last_fn:
                evt_fn = FnChangeEvent(
                    timestamp=now, old_fn=self._last_fn, new_fn=info.fn,
                )
                events.append(evt_fn)
                for cb in self._fn_callbacks:
                    cb(evt_fn)
            self._last_fn = info.fn

        except Exception:
            pass  # Device may be busy

        # 2. Read unsolicited packets
        if self._read_unsolicited:
            try:
                data = self._device.receive(timeout_ms=10)
                if data:
                    evt_raw = RawPacketEvent(timestamp=now, data=data)
                    events.append(evt_raw)
                    for cb in self._raw_callbacks:
                        cb(evt_raw)
            except Exception:
                pass

    @property
    def last_fn(self) -> int | None:
        """Last observed FN layer number, or None if not yet polled."""
        return self._last_fn

    @property
    def last_info(self) -> DeviceInfo | None:
        """Last observed DeviceInfo, or None if not yet polled."""
        return self._last_info

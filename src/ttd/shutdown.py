"""Graceful shutdown coordination for long-running fetches.

The coordinator installs SIGINT/SIGTERM handlers that set a shared stop
flag. Workers check the flag between chunks and finish their current
work before exiting, so no in-flight chunk is lost mid-request.

A second Ctrl+C is treated as a hard kill: the handler re-raises
``KeyboardInterrupt`` so the user always has an escape hatch.
"""

from __future__ import annotations

import signal
import threading
from typing import Any, Callable


class ShutdownCoordinator:
    """Install signal handlers that set a shared stop flag.

    Usage::

        coordinator = ShutdownCoordinator(on_shutdown=lambda sig: print(...))
        coordinator.install()
        try:
            run_fetch(coordinator=coordinator)
        finally:
            coordinator.restore()
    """

    def __init__(
        self,
        *,
        on_shutdown: Callable[[str], None] | None = None,
    ) -> None:
        self._stop = threading.Event()
        self._signal_name = ""
        self._on_shutdown = on_shutdown
        self._previous: dict[int, Any] = {}
        self._installed = False
        self._lock = threading.Lock()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def signal_name(self) -> str:
        return self._signal_name

    def install(self) -> None:
        with self._lock:
            if self._installed:
                return
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    self._previous[sig] = signal.getsignal(sig)
                    signal.signal(sig, self._handle)
                except (ValueError, OSError):
                    # Signal not available on this platform.
                    pass
            self._installed = True

    def restore(self) -> None:
        with self._lock:
            if not self._installed:
                return
            for sig, handler in self._previous.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass
            self._previous.clear()
            self._installed = False

    def reset(self) -> None:
        self._stop.clear()
        self._signal_name = ""

    def _handle(self, signum: int, frame: Any) -> None:
        name = signal.Signals(signum).name
        if self._signal_name:
            # Second signal: force an immediate exit.
            raise KeyboardInterrupt()
        self._signal_name = name
        self._stop.set()
        if self._on_shutdown is not None:
            try:
                self._on_shutdown(name)
            except Exception:
                pass
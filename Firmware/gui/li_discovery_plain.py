"""
li_discovery_plain.py — stdlib-only mDNS hostname resolution for Li-6800.

Tries a small list of well-known LI-COR hostnames via socket.gethostbyname().
Works on macOS/Linux natively and on Windows 10+ with mDNS enabled. Does NOT
depend on the zeroconf package, so this module stays importable even when
zeroconf is missing.

Duck-typed discovery protocol (kept in sync with li_discovery.LiDiscovery):
    host_found = Signal(str, str)   # (hostname, ip)
    finished   = Signal(int)        # number of hosts found
    def start(self, duration_s: float) -> None
    def stop(self) -> None
"""

from __future__ import annotations

import socket
import threading

from PySide6.QtCore import QObject, Signal

LICOR_HOSTNAMES = ("licor.local", "licor-6800.local")


class PlainMdnsResolver(QObject):
    host_found = Signal(str, str)
    finished = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._running = False
        self._count = 0

    def start(self, duration_s: float = 5.0) -> None:
        if self._running:
            return
        self._running = True
        self._count = 0
        self._thread = threading.Thread(
            target=self._run, name="PlainMdnsResolver", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        try:
            for name in LICOR_HOSTNAMES:
                if not self._running:
                    break
                try:
                    ip = socket.gethostbyname(name)
                except (socket.gaierror, OSError):
                    continue
                self._count += 1
                self.host_found.emit(name, ip)
        finally:
            self._running = False
            self.finished.emit(self._count)

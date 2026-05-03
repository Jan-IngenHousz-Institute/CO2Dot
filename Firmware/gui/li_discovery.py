"""
li_discovery.py — zeroconf-based mDNS browser for Li-6800 discovery.

Imports `zeroconf` at module top so that a missing package raises ImportError
at `from li_discovery import LiDiscovery` time (caught on the main thread in
_init_li_control / _on_li_scan), not later on a worker thread.

If zeroconf is missing, callers fall back to li_discovery_plain.PlainMdnsResolver
directly. Both classes duck-type the same interface:
    host_found = Signal(str, str)
    finished   = Signal(int)
    start(duration_s: float) -> None
    stop() -> None
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

from li_discovery_plain import PlainMdnsResolver

LICOR_SERVICE_TYPES = (
    "_ssh._tcp.local.",
    "_sftp-ssh._tcp.local.",
    "_http._tcp.local.",
    "_workstation._tcp.local.",
)

LICOR_NAME_HINTS = ("licor", "li-6800", "li6800")


def _looks_like_licor(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in LICOR_NAME_HINTS)


class LiDiscovery(QObject):
    host_found = Signal(str, str)
    finished = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._zc: Zeroconf | None = None
        self._browsers: list[ServiceBrowser] = []
        self._plain = PlainMdnsResolver(self)
        self._plain.host_found.connect(self._relay_host)
        self._plain.finished.connect(self._on_plain_finished)
        self._stop_timer = QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.timeout.connect(self._on_stop_timer)
        self._seen: set[str] = set()
        self._running = False
        self._plain_done = False
        self._zeroconf_done = False

    def start(self, duration_s: float = 5.0) -> None:
        if self._running:
            return
        self._running = True
        self._seen.clear()
        self._plain_done = False
        self._zeroconf_done = False

        # Plain resolver path (stdlib mDNS)
        self._plain.start(duration_s)

        # Zeroconf multi-service browse
        try:
            self._zc = Zeroconf()
            for svc in LICOR_SERVICE_TYPES:
                b = ServiceBrowser(
                    self._zc, svc, handlers=[self._on_service]
                )
                self._browsers.append(b)
        except Exception:
            self._zc = None
            self._browsers = []
            self._zeroconf_done = True

        self._stop_timer.start(int(duration_s * 1000))

    def stop(self) -> None:
        self._stop_timer.stop()
        self._plain.stop()
        self._close_zeroconf()
        self._running = False

    def _close_zeroconf(self) -> None:
        for b in self._browsers:
            try:
                b.cancel()
            except Exception:
                pass
        self._browsers = []
        if self._zc is not None:
            try:
                self._zc.close()
            except Exception:
                pass
            self._zc = None

    def _on_service(self, zeroconf: Zeroconf, service_type: str, name: str,
                    state_change: ServiceStateChange) -> None:
        if state_change is not ServiceStateChange.Added:
            return
        try:
            info = zeroconf.get_service_info(service_type, name, timeout=1500)
        except Exception:
            return
        if info is None:
            return
        server = (info.server or "").rstrip(".")
        if not _looks_like_licor(server) and not _looks_like_licor(name):
            return
        # Pick the first IPv4 or IPv6 address if available
        ip = ""
        try:
            addrs = info.parsed_addresses()
            if addrs:
                ip = addrs[0]
        except Exception:
            pass
        self._relay_host(server or name, ip)

    def _relay_host(self, name: str, ip: str) -> None:
        key = f"{name}|{ip}"
        if key in self._seen:
            return
        self._seen.add(key)
        self.host_found.emit(name, ip)

    def _on_plain_finished(self, _count: int) -> None:
        self._plain_done = True
        self._maybe_finish()

    def _on_stop_timer(self) -> None:
        self._zeroconf_done = True
        self._close_zeroconf()
        self._maybe_finish()

    def _maybe_finish(self) -> None:
        if not self._running:
            return
        if self._plain_done and self._zeroconf_done:
            self._running = False
            self.finished.emit(len(self._seen))

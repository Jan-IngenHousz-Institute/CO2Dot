"""
pyro_worker.py — QThread that owns the Pyroscience serial port.

`import serial` is at module top so a missing pyserial fails fast at
`from pyro_worker import PyroWorker`, which MainWindow catches when the
optional feature is toggled on.

Run loop uses a short serial read timeout (200 ms) and paces sample
requests with a sleep loop that polls the abort event — so close_port()
returns within ~100 ms regardless of the configured sample interval.
"""

from __future__ import annotations

import threading
import time

import serial
from PySide6.QtCore import QThread, Signal

import pyro_protocol


IDNR_TIMEOUT_S = 2.0
SERIAL_READ_TIMEOUT_S = 0.2
PARSE_FAIL_REPORT_EVERY = 5


class PyroWorker(QThread):
    connected       = Signal(dict)   # {"port": str, "idnr": str}
    disconnected    = Signal()
    sample_received = Signal(dict)   # {"timestamp", "channel", **fields}
    error_received  = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._port = ""
        self._channel = 1
        self._interval_s = 1.0
        self._ser: serial.Serial | None = None
        self._abort = threading.Event()
        self._streaming = threading.Event()  # gate around MEA polling

    # ---- Public API (main thread) -------------------------------------
    def open_port(self, port: str, channel: int = 1, interval_s: float = 1.0) -> None:
        """Open the port and IDNR-verify. Does NOT begin polling — call
        start_streaming() to begin emitting sample_received signals."""
        self._port = port
        self._channel = max(1, int(channel))
        self._interval_s = max(0.1, float(interval_s))
        self._abort.clear()
        self._streaming.clear()
        self.start()

    def close_port(self) -> None:
        self._abort.set()
        self._streaming.clear()
        if not self.wait(2000):
            self.terminate()
            self.wait(1000)

    def start_streaming(self, interval_s: float | None = None) -> None:
        """Begin (or resume) polling MEA at `interval_s`."""
        if interval_s is not None:
            self._interval_s = max(0.1, float(interval_s))
        self._streaming.set()

    def stop_streaming(self) -> None:
        """Stop polling but keep the port open."""
        self._streaming.clear()

    # ---- Thread run loop ----------------------------------------------
    def run(self) -> None:
        try:
            self._ser = serial.Serial(
                self._port,
                pyro_protocol.BAUD,
                timeout=SERIAL_READ_TIMEOUT_S,
                write_timeout=1.0,
            )
            self._ser.reset_input_buffer()
        except (serial.SerialException, OSError) as exc:
            self.error_received.emit(f"Pyro: cannot open {self._port}: {exc}")
            self.disconnected.emit()
            return

        idnr = self._do_idnr()
        if idnr is None:
            self._close_handle()
            self.disconnected.emit()
            return

        self.connected.emit({"port": self._port, "idnr": idnr})

        consecutive_fails = 0
        while not self._abort.is_set():
            # Idle while not streaming — keep the port open but don't poll
            if not self._streaming.is_set():
                time.sleep(0.1)
                continue

            cycle_start = time.monotonic()
            ts = time.time()
            try:
                self._ser.write(pyro_protocol.cmd_measure(self._channel))
                raw = self._ser.readline()
            except (serial.SerialException, OSError) as exc:
                self.error_received.emit(f"Pyro: serial error: {exc}")
                break

            if raw:
                line = raw.decode("utf-8", errors="replace")
                sample = pyro_protocol.parse_meas_line(line)
                if sample is None:
                    consecutive_fails += 1
                    if consecutive_fails == PARSE_FAIL_REPORT_EVERY:
                        snippet = line.strip()[:60]
                        self.error_received.emit(
                            f"Pyro: {PARSE_FAIL_REPORT_EVERY} bad lines "
                            f"(e.g. '{snippet}') — wrong port or baud?"
                        )
                else:
                    consecutive_fails = 0
                    payload = {"timestamp": ts, "channel": self._channel}
                    payload.update(sample)
                    self.sample_received.emit(payload)

            # Pace the loop with abort/stop-aware sleep
            remaining = self._interval_s - (time.monotonic() - cycle_start)
            while (remaining > 0
                   and not self._abort.is_set()
                   and self._streaming.is_set()):
                step = min(0.1, remaining)
                time.sleep(step)
                remaining -= step

        self._close_handle()
        self.disconnected.emit()

    # ---- Helpers ------------------------------------------------------
    def _do_idnr(self) -> str | None:
        try:
            self._ser.write(pyro_protocol.CMD_IDNR)
        except (serial.SerialException, OSError) as exc:
            self.error_received.emit(f"Pyro: write IDNR failed: {exc}")
            return None
        deadline = time.monotonic() + IDNR_TIMEOUT_S
        while time.monotonic() < deadline and not self._abort.is_set():
            try:
                raw = self._ser.readline()
            except (serial.SerialException, OSError) as exc:
                self.error_received.emit(f"Pyro: read IDNR failed: {exc}")
                return None
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            if pyro_protocol.is_idnr_response(line):
                return pyro_protocol.extract_idnr(line)
        self.error_received.emit(f"Pyro: no IDNR response from {self._port}")
        return None

    def _close_handle(self) -> None:
        if self._ser is not None and self._ser.is_open:
            try:
                self._ser.close()
            except (serial.SerialException, OSError):
                pass
        self._ser = None

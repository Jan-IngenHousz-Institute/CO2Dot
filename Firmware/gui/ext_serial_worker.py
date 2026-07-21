"""
ext_serial_worker.py — QThread owning the external (scripting) serial port.

Unlike SerialWorker/PyroWorker, this worker opens the port with DTR/RTS
de-asserted BEFORE open(): the target ESP32 wires DTR→IO0 and RTS→EN, so a
default open() auto-resets the board mid-experiment. For the same reason the
port is never hello-probed — connection is by explicit user selection only.

Lifecycle follows PyroWorker (threading.Event abort); command sending follows
SerialWorker (thread-safe outgoing queue.Queue). send_command() raises
RuntimeError when the port is dead so a running script fails loudly instead
of silently queueing into nowhere.
"""

from __future__ import annotations

import queue
import threading
import time

import serial
from PySide6.QtCore import QThread, Signal

LINE_ENDING = "\r\n"       # the ESP32 CLI expects CRLF
DEFAULT_BAUD = 115200
SERIAL_READ_TIMEOUT_S = 0.1


class ExtSerialWorker(QThread):
    connected      = Signal(dict)        # {"port": str, "baud": int}
    disconnected   = Signal()
    line_sent      = Signal(float, str)  # timestamp, command echoed
    line_received  = Signal(float, str)  # timestamp, stripped reply line
    error_received = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._port = ""
        self._baud = DEFAULT_BAUD
        self._ser: serial.Serial | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._abort = threading.Event()
        self._alive = threading.Event()   # set while the port is usable

    @property
    def port(self) -> str:
        return self._port

    # ---- Public API (main thread / script thread) ----------------------

    def open_port(self, port: str, baud: int = DEFAULT_BAUD) -> None:
        self._port = port
        self._baud = int(baud)
        self._abort.clear()
        with self._queue.mutex:
            self._queue.queue.clear()
        self.start()

    def close_port(self) -> None:
        self._abort.set()
        self._alive.clear()
        if not self.wait(2000):
            # Thread stuck in a blocking pyserial read; terminate is
            # tolerable here (C code, no Python state to corrupt).
            self.terminate()
            self.wait(1000)

    def send_command(self, cmd: str) -> None:
        """Thread-safe enqueue. Raises RuntimeError if the port is not open
        so scripts fail loudly instead of writing into the void."""
        if not self._alive.is_set():
            raise RuntimeError(
                f"serial not connected ({self._port or 'no port selected'})")
        self._queue.put(str(cmd))

    # ---- Thread run loop -----------------------------------------------

    def run(self) -> None:
        try:
            ser = serial.Serial()          # two-step open: nothing asserted yet
            ser.port = self._port
            ser.baudrate = self._baud
            ser.timeout = SERIAL_READ_TIMEOUT_S
            ser.write_timeout = 1.0
            ser.dtr = False                # don't drive IO0
            ser.rts = False                # don't drive EN → no ESP32 auto-reset
            ser.open()
            ser.reset_input_buffer()       # device already running; drop stale bytes
            self._ser = ser
        except (serial.SerialException, OSError) as exc:
            text = str(exc)
            if "PermissionError" in text or "Access is denied" in text:
                self.error_received.emit(
                    f"Serial: {self._port} is in use — close the PlatformIO "
                    "monitor / other terminal first")
            else:
                self.error_received.emit(
                    f"Serial: cannot open {self._port}: {exc}")
            self.disconnected.emit()
            return

        self._alive.set()
        self.connected.emit({"port": self._port, "baud": self._baud})

        while not self._abort.is_set():
            try:
                # --- outgoing ---
                while not self._queue.empty():
                    try:
                        cmd = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    ser.write((cmd + LINE_ENDING).encode())
                    self.line_sent.emit(time.time(), cmd)
                # --- incoming (100 ms timeout paces the loop) ---
                raw = ser.readline()
            except (serial.SerialException, OSError) as exc:
                self._alive.clear()
                self.error_received.emit(f"Serial: port error: {exc}")
                break

            if raw:
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.line_received.emit(time.time(), text)

        self._alive.clear()
        self._close_handle()
        self.disconnected.emit()

    def _close_handle(self) -> None:
        if self._ser is not None and self._ser.is_open:
            try:
                self._ser.close()
            except (serial.SerialException, OSError):
                pass
        self._ser = None

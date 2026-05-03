"""
serial_worker.py — QThread that owns the serial port and dispatches parsed data.

The worker runs a tight loop:
  1. Drain any outgoing commands from the queue.
  2. Read one line from serial (non-blocking via timeout).
  3. Classify and emit the appropriate Qt signal.
"""

import queue
import time
import serial

from PySide6.QtCore import QThread, Signal

from protocol import classify_line, CMD_STATUS
from device_manager import BAUD_RATE

HELLO_TIMEOUT = 5.0  # seconds to wait for hello response


class SerialWorker(QThread):
    # Signals emitted on the Qt main thread via queued connections
    spec_received    = Signal(dict)   # {"model": str, "channels": dict}
    bme_received     = Signal(dict)   # {"T": float, "P": float, "RH": float, "Gas": int}
    status_received  = Signal(dict)   # {"spectrometer": {...}, "bme": {...}}
    spec_config_received = Signal(dict)  # {"atime": int, "astep": int, "gain": int}
    error_received   = Signal(str)    # human-readable error string
    connected        = Signal(str)    # port name when hello confirmed
    disconnected     = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._port: str = ""
        self._ser: serial.Serial | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._running = False

    # ------------------------------------------------------------------
    # Public API (called from the main thread)
    # ------------------------------------------------------------------

    def open_port(self, port: str) -> None:
        """Set the port and start the thread."""
        self._port = port
        self._running = True
        self.start()

    def close_port(self) -> None:
        """Signal the run loop to stop and wait for the thread to finish."""
        self._running = False
        if not self.wait(2000):
            # Thread didn't exit in time (stuck in blocking read); terminate
            self.terminate()
            self.wait(1000)

    def send_command(self, cmd: str) -> None:
        """Enqueue a command to be sent over serial."""
        self._queue.put(cmd)

    # ------------------------------------------------------------------
    # Thread run loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            self._ser = serial.Serial(
                self._port, BAUD_RATE,
                timeout=0.1,          # 100 ms readline timeout
                write_timeout=1.0,
            )
            self._ser.reset_input_buffer()
            # Confirm device presence
            self._ser.write(b"hello\n")
        except serial.SerialException as exc:
            self.error_received.emit(f"Cannot open {self._port}: {exc}")
            self._running = False
            return

        hello_confirmed = False
        hello_deadline = time.monotonic() + HELLO_TIMEOUT

        while self._running:
            # --- hello timeout check ---
            if not hello_confirmed and time.monotonic() > hello_deadline:
                self.error_received.emit(
                    f"No response from {self._port} (timeout after {HELLO_TIMEOUT:.0f}s)"
                )
                self._running = False
                break

            # --- outgoing ---
            while not self._queue.empty():
                try:
                    cmd = self._queue.get_nowait()
                    self._ser.write((cmd + "\n").encode())
                except queue.Empty:
                    break
                except serial.SerialException as exc:
                    self.error_received.emit(f"Write error: {exc}")
                    self._running = False
                    break

            if not self._running:
                break

            # --- incoming ---
            try:
                raw = self._ser.readline()
            except serial.SerialException as exc:
                self.error_received.emit(f"Read error: {exc}")
                self._running = False
                break

            if not raw:
                continue

            line = raw.decode("utf-8", errors="replace")
            kind, parsed = classify_line(line)

            if kind == "hello":
                if not hello_confirmed:
                    hello_confirmed = True
                    self.connected.emit(self._port)
                    # Request initial status
                    self._queue.put(CMD_STATUS)

            elif kind == "status":
                if parsed:
                    self.status_received.emit(parsed)

            elif kind in ("spec", "spec_flash"):
                if parsed:
                    self.spec_received.emit(parsed)

            elif kind == "bme":
                if parsed:
                    self.bme_received.emit(parsed)

            elif kind in ("spec_config", "led_set"):
                if parsed:
                    self.spec_config_received.emit(parsed)

            elif kind == "error":
                self.error_received.emit(str(parsed))

        # Cleanup
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None
        self.disconnected.emit()

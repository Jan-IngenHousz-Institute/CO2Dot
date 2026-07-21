"""
port_probe.py — safe serial-port autodetection for the CO2Dot/MiniPAR, the
Pyroscience optode, and the ambyte ESP32 (Serial Scripting target).

Every probe opens the port with DTR/RTS de-asserted BEFORE open(), so a
device that auto-resets on a normal open (ESP32-style USB bridges — both
the ambyte and the CO2Dot) is never rebooted by the probe itself.

The probe runs two phases so that every known device receives only
COMPLETELY TERMINATED command lines and is left with a clean input buffer
(an unterminated leftover like 'hello\\n' would corrupt the Pyroscience
device's next real '#IDNR\\r' command and make the subsequent connect fail):

    phase 1: '#IDNR\\r'   FireSting framing (CR) → answers '#IDNR <serial>';
                          the ambyte console also executes on CR
    phase 2: '\\nhello\\n' CO2Dot framing (LF); the leading \\n turns phase
                          1's bytes into a complete (ignored) line, then
                          'hello' → {"device": "CO2Dot", ...}

Ports that are already open elsewhere raise on open() and are skipped.
"""

from __future__ import annotations

import time

import serial
from PySide6.QtCore import QThread, Signal

PROBE_BAUD = 115200
PROBE_READ_TIMEOUT_S = 0.15
PROBE_PHASE_S = 0.6            # read window per phase

PYRO_MARKER = "#IDNR"
AMBYTE_MARKER = "ambyte"
CO2DOT_MARKERS = ("CO2Dot", "MiniPAR")


def probe_port(port: str) -> tuple[str, str] | None:
    """Probe one port. Returns ("pyro", idnr), ("ambyte", ""),
    ("co2dot", device_name) or None."""
    try:
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = PROBE_BAUD
        ser.timeout = PROBE_READ_TIMEOUT_S
        ser.write_timeout = 0.5
        ser.dtr = False        # don't drive IO0
        ser.rts = False        # don't drive EN → no auto-reset
        ser.open()
    except (serial.SerialException, OSError):
        return None            # in use or gone — skip
    try:
        ser.reset_input_buffer()
        seen = ""

        def listen(duration: float):
            nonlocal seen
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace")
                stripped = line.strip()
                if stripped.startswith(PYRO_MARKER):
                    parts = stripped.split()
                    return ("pyro", parts[1] if len(parts) > 1 else "")
                for dev in CO2DOT_MARKERS:
                    if dev in line:
                        return ("co2dot", dev)
                seen += line.lower()
                if AMBYTE_MARKER in seen:
                    return ("ambyte", "")
            return None

        # Phase 1 — Pyroscience framing (CR terminator, fully terminated).
        ser.write(PYRO_MARKER.encode() + b"\r")
        res = listen(PROBE_PHASE_S)
        if res is not None:
            return res
        # Phase 2 — CO2Dot framing (LF terminator). A FireSting would have
        # answered in phase 1, so it never sees these unterminated-for-CR
        # bytes.
        ser.write(b"\nhello\n")
        return listen(PROBE_PHASE_S)
    except (serial.SerialException, OSError):
        return None
    finally:
        try:
            ser.close()
        except (serial.SerialException, OSError):
            pass


class PortProbeThread(QThread):
    """Sweep a list of ports off the GUI thread."""

    port_checked  = Signal(str, str)   # port, kind ("pyro"|"ambyte"|"co2dot"|"")
    finished_scan = Signal(dict)       # {"co2dot": (port, device)|None,
                                       #  "pyro": (port, idnr)|None,
                                       #  "ambyte": port|None}

    def __init__(self, ports, parent=None):
        super().__init__(parent)
        self._ports = list(ports)

    def run(self) -> None:
        found = {"co2dot": None, "pyro": None, "ambyte": None}
        for port in self._ports:
            if all(v is not None for v in found.values()):
                break
            res = probe_port(port)
            self.port_checked.emit(port, res[0] if res else "")
            if res is None:
                continue
            kind, info = res
            if kind == "co2dot" and found["co2dot"] is None:
                found["co2dot"] = (port, info)
            elif kind == "pyro" and found["pyro"] is None:
                found["pyro"] = (port, info)
            elif kind == "ambyte" and found["ambyte"] is None:
                found["ambyte"] = port
        self.finished_scan.emit(found)

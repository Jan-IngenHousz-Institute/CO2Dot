"""
co2dot.py — minimal synchronous serial client for the CO2Dot device (USB).

Mirrors the protocol used by the CO2Dot firmware/GUI
(git-repo/CO2Dot/Firmware/gui/protocol.py and src/app/commands.cpp):
  115200 baud, 8N1, text commands terminated with '\\n', one JSON object per
  response line. Device is identified by a `hello` greeting whose "device"
  field is "CO2Dot" (or "MiniPAR").

    dot = CO2Dot.connect()      # autodetect over USB serial (opens once, stays open)
    dot.status()                # {'spectrometer': {...}, 'bme': {...}} settings
    dot.spec_flash(1)           # {'dark':..., 'lit':..., 'diff':...} spectra
    dot.env()                   # {'T','P','RH','Gas'} environment (BME688)
    dot.close()

Implementation notes:
  - Opening a USB-CDC serial port resets the MCU, and the firmware waits ~2 s on
    boot, so connect() opens the port ONCE, waits out the boot, confirms with a
    `hello`, and keeps the same connection open. (Opening twice — once to detect,
    once to use — double-resets the device and races the boot.)
  - Responses are read by accumulating bytes and splitting on '\\n', which is
    robust to a JSON line arriving in several USB chunks (plain readline() with a
    short timeout can return a partial, unparseable fragment).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import serial
import serial.tools.list_ports

BAUD = 115200
KNOWN_DEVICES = ("CO2Dot", "MiniPAR")
BOOT_SETTLE_S = 2.5      # USB-CDC reset + firmware delay(2000) on boot
HELLO_TIMEOUT_S = 6.0


def list_ports() -> List[str]:
    """Names of all available serial ports."""
    return [p.device for p in serial.tools.list_ports.comports()]


def _read_json_objects(ser: "serial.Serial", timeout_s: float):
    """Yield parsed JSON objects from serial until timeout.

    Accumulates bytes and splits on newlines so a response split across several
    reads is reassembled before parsing; non-JSON lines are skipped.
    """
    deadline = time.monotonic() + timeout_s
    buf = bytearray()
    while time.monotonic() < deadline:
        n = ser.in_waiting
        chunk = ser.read(n if n else 1)   # drain what's there, else block briefly for 1 byte
        if not chunk:
            continue
        buf.extend(chunk)
        while b"\n" in buf:
            raw, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("{"):
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def _find_error(obj: Dict[str, Any]) -> Optional[str]:
    """Return an error string if the response is a top-level or nested error."""
    if "error" in obj:
        return str(obj["error"])
    for v in obj.values():
        if isinstance(v, dict) and "error" in v:
            return str(v["error"])
    return None


def detect_co2dot(timeout_s: float = HELLO_TIMEOUT_S) -> Tuple[str, str]:
    """Probe serial ports (open/close each); return (port, device) of the first CO2Dot.

    Note: this opens and closes ports, which resets USB-CDC devices. Prefer
    CO2Dot.connect(), which opens once and keeps the connection.
    """
    ports = list_ports()
    for port in ports:
        try:
            with serial.Serial(port, BAUD, timeout=0.2) as ser:
                time.sleep(BOOT_SETTLE_S)
                ser.reset_input_buffer()
                ser.write(b"hello\n")
                for obj in _read_json_objects(ser, timeout_s):
                    if obj.get("device") in KNOWN_DEVICES:
                        return port, obj["device"]
        except (serial.SerialException, OSError):
            continue
    raise RuntimeError(f"No CO2Dot found on any serial port. Ports seen: {ports}")


class CO2Dot:
    """Synchronous USB-serial handle to a CO2Dot (opened once, kept open)."""

    def __init__(self, ser: "serial.Serial", device: str, port: str):
        self._ser = ser
        self.device = device
        self.port = port

    @classmethod
    def connect(cls, port: Optional[str] = None, *, settle_s: float = BOOT_SETTLE_S,
                hello_timeout_s: float = HELLO_TIMEOUT_S) -> "CO2Dot":
        """Open the CO2Dot once and keep it open, autodetecting the port if not given.

        Tries the given port, or every serial port, sending `hello` and keeping
        the first connection that returns a known device greeting.
        """
        candidates = [port] if port else list_ports()
        last = "no serial ports found"
        for p in candidates:
            try:
                ser = serial.Serial(p, BAUD, timeout=0.2, write_timeout=2.0)
            except (serial.SerialException, OSError) as exc:
                last = f"{p}: {exc}"
                continue
            try:
                time.sleep(settle_s)            # wait out USB-CDC reset + firmware boot
                ser.reset_input_buffer()
                ser.write(b"hello\n")
                device = None
                for obj in _read_json_objects(ser, hello_timeout_s):
                    if obj.get("device") in KNOWN_DEVICES:
                        device = obj["device"]
                        break
                if device:
                    return cls(ser, device, p)
                ser.close()
                last = f"{p}: opened but no CO2Dot hello"
            except (serial.SerialException, OSError) as exc:
                try:
                    ser.close()
                except Exception:
                    pass
                last = f"{p}: {exc}"
        raise RuntimeError(f"No CO2Dot found. Last: {last}")

    def _command(self, cmd: str, expect_keys, timeout_s: float = 6.0,
                 retries: int = 1) -> Dict[str, Any]:
        """Send a command and return the first JSON response containing any expect_keys.

        expect_keys may be a single key or a tuple of acceptable keys — the device's
        response shape has varied across firmware versions, so callers pass every
        variant. Resends up to `retries` times if no matching response arrives
        (covers a command lost while the device was still settling).
        """
        keys = (expect_keys,) if isinstance(expect_keys, str) else tuple(expect_keys)
        for _ in range(retries + 1):
            self._ser.reset_input_buffer()
            self._ser.write((cmd + "\n").encode())
            for obj in _read_json_objects(self._ser, timeout_s):
                if any(k in obj for k in keys):
                    return obj
                err = _find_error(obj)
                if err:
                    raise RuntimeError(f"CO2Dot error for '{cmd}': {err}")
        raise TimeoutError(f"No {keys} response to '{cmd}' within {timeout_s:.0f}s")

    # -- commands ---------------------------------------------------------- #
    def hello(self, timeout_s: float = 4.0) -> Dict[str, Any]:
        """Round-trip a `hello` on the open connection (handshake / liveness check)."""
        return self._command("hello", "device", timeout_s)

    def status(self, timeout_s: float = 4.0) -> Dict[str, Any]:
        """Device parameters: spectrometer (model, atime, astep, gain, led) + BME status.

        Returns {'spectrometer': {...}, 'bme': {...}}. (Accepts both this firmware's
        `spectrometer`/`bme` keys and the newer `spectrometer_status`/`bme_status`.)
        """
        obj = self._command("status", ("spectrometer", "spectrometer_status"), timeout_s)
        return {"spectrometer": obj.get("spectrometer") or obj.get("spectrometer_status"),
                "bme": obj.get("bme") or obj.get("bme_status")}

    def spec_flash(self, led_current: int = 1, timeout_s: float = 8.0) -> Dict[str, Any]:
        """Flash measurement: dark, lit (LED on), and their per-channel difference.

        Sends `spec_flash,<led_current>`; returns
        {'led_current', 'model', 'dark', 'lit', 'diff'} where each spectrum is a
        {channel: count} dict. Accepts both this firmware's flat
        {model, dark, lit, diff} shape and the newer spectrometer_dark/lit/diff one.
        """
        obj = self._command(f"spec_flash,{led_current}",
                            ("diff", "spectrometer_diff"), timeout_s)

        def channels(dev_key: str, repo_key: str) -> Dict[str, Any]:
            if dev_key in obj:                        # this firmware: dark/lit/diff are channel dicts
                return obj[dev_key]
            d = obj.get(repo_key, {}) or {}           # newer firmware: {model, channels}
            return d.get("channels", {})

        model = obj.get("model") or (obj.get("spectrometer_diff", {}) or {}).get("model")
        return {"led_current": led_current, "model": model,
                "dark": channels("dark", "spectrometer_dark"),
                "lit": channels("lit", "spectrometer_lit"),
                "diff": channels("diff", "spectrometer_diff")}

    def env(self, timeout_s: float = 4.0) -> Dict[str, Any]:
        """Environment from the onboard BME688: {'T','P','RH','Gas'}.

        Accepts both this firmware's top-level T/P/RH/Gas and the newer
        {'bme_read': {...}} wrapper.
        """
        obj = self._command("env", ("T", "bme_read"), timeout_s)
        if "bme_read" in obj:
            return obj["bme_read"]
        return {k: obj.get(k) for k in ("T", "P", "RH", "Gas")}

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass

    def __enter__(self) -> "CO2Dot":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


if __name__ == "__main__":
    dot = CO2Dot.connect()
    print(f"connected to {dot.device} on {dot.port}")
    print("status:", dot.status())
    print("env:", dot.env())
    print("spec_flash:", dot.spec_flash(1))
    dot.close()

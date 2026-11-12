"""
device_manager.py — Serial port enumeration, opening and autodetection for
CO2Dot / MiniPAR.

Everything about a serial port that differs between Windows, Linux and macOS
is handled here, so the workers get the same behaviour on all three:

    list_ports()            ports worth offering, in natural order
    open_serial()           exclusive open, optional no-reset DTR/RTS handling
    describe_open_error()   why an open failed, in words the user can act on

Qt-free on purpose: this module is imported by worker threads and panels alike.
"""

import errno
import json
import os
import re
import sys

import serial
import serial.tools.list_ports


BAUD_RATE = 115200
HELLO_CMD = b"hello\n"
# Known device identifiers returned in the hello JSON "device" field
KNOWN_DEVICES = ("CO2Dot", "MiniPAR")
DETECT_TIMEOUT = 2.0  # seconds to wait for hello response

# macOS lists these next to real adapters. None is an instrument, and opening
# the Bluetooth one can block for seconds — long enough to stall an
# autodetect sweep.
_MACOS_PSEUDO_PORTS = ("Bluetooth-Incoming-Port", "debug-console", "wlan-debug")


class PortOpenError(Exception):
    """A port could not be opened; str() is a ready-to-show explanation."""


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def _natural_key(info):
    """COM3 before COM10, ttyACM2 before ttyACM10."""
    return [int(tok) if tok.isdigit() else tok.lower()
            for tok in re.split(r"(\d+)", info.device)]


def _comports():
    ports = serial.tools.list_ports.comports()
    if sys.platform == "darwin":
        ports = [p for p in ports
                 if not any(tag in p.device for tag in _MACOS_PSEUDO_PORTS)]
    return sorted(ports, key=_natural_key)


def list_ports() -> list[str]:
    """Return a list of available serial port names."""
    return [p.device for p in _comports()]


def list_ports_info() -> list[tuple[str, str]]:
    """Return (device, description) tuples for all serial ports."""
    return [(p.device, p.description or "") for p in _comports()]


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------

def open_serial(port: str, baud: int = BAUD_RATE, *, timeout: float,
                write_timeout: float = 1.0,
                quiet_lines: bool = False) -> serial.Serial:
    """Open `port` with the same semantics on every OS.

    Raises serial.SerialException / OSError exactly as pyserial does; pass the
    exception to describe_open_error() for a message.

    Exclusive access: Windows refuses a second open of a COM port by itself.
    POSIX does not — two programs can hold the same tty and silently eat each
    other's bytes — so pyserial's advisory lock is requested. Other pyserial
    tools (PlatformIO monitor, esptool, a second copy of this app) take the
    same lock, which gives Linux and macOS the "port is in use" error too.

    quiet_lines: keep DTR/RTS de-asserted, for boards that wire them to reset
    (ESP32: DTR→IO0, RTS→EN) and must not reboot because a port was opened.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = timeout
    ser.write_timeout = write_timeout
    ser.exclusive = True

    if not quiet_lines:
        ser.open()
        return ser

    if os.name == "nt":
        # Windows applies the line state as part of opening the port, so set
        # beforehand the lines never rise at all.
        ser.dtr = False
        ser.rts = False
        ser.open()
        return ser

    # POSIX: the kernel raises both lines inside open() and nothing can stop
    # it; pyserial then applies the requested state, DTR first. Asking for
    # both low therefore passes through "DTR low, RTS high" — exactly the
    # ESP32 reset state. Keeping DTR up across the open and dropping it last
    # goes both-high → RTS low → both low, which never asserts reset.
    ser.rts = False
    ser.open()
    try:
        ser.dtr = False
    except OSError:
        pass    # no modem-control lines on this device (pty, some adapters)
    return ser


def is_permission_denied(exc: BaseException) -> bool:
    """True when the OS refused an open for lack of rights.

    POSIX only. Windows reports a port that is merely in use the same way, so
    there the answer is always False."""
    return (os.name != "nt"
            and getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM))


def _permission_hint(port: str) -> str:
    if not sys.platform.startswith("linux"):
        return "check the access rights of the device"
    # Serial devices belong to a group ("dialout" on Debian/Ubuntu/Fedora,
    # "uucp" on Arch); ask the device itself rather than guessing.
    group = "dialout"
    try:
        import grp
        group = grp.getgrgid(os.stat(port).st_gid).gr_name
    except (ImportError, KeyError, OSError):
        pass
    return (f"add your user to the '{group}' group "
            f"(sudo usermod -aG {group} $USER), then log out and back in")


def describe_open_error(port: str, exc: BaseException) -> str:
    """One line saying why `port` could not be opened and what to do."""
    text = str(exc)
    code = getattr(exc, "errno", None)
    in_use = (f"{port} is in use by another program — close the serial "
              "monitor / PlatformIO / other copy of this app and retry")
    missing = (f"{port} is not there any more — replug the device and "
               "refresh the port list")

    if os.name == "nt":
        # pyserial folds the Win32 error into the text and sets no errno.
        if "PermissionError" in text or "Access is denied" in text:
            return in_use
        if "FileNotFoundError" in text:
            return missing
    else:
        if is_permission_denied(exc):
            return f"{port}: permission denied — {_permission_hint(port)}"
        if (code in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK)
                or "exclusively lock" in text):
            return in_use
        if code in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
            return missing
    return f"cannot open {port}: {exc}"


# ---------------------------------------------------------------------------
# Autodetection
# ---------------------------------------------------------------------------

def check_port(port: str) -> str | None:
    """
    Try opening `port` at 115200 baud, send 'hello', and check for a
    known device greeting.  Returns the device type string (e.g. "CO2Dot",
    "MiniPAR") on success, or None if no known device responds.

    Raises PortOpenError when the port cannot be opened at all (in use, no
    permission, unplugged): that is a different problem from "opened fine,
    nothing answered" and the user needs to be told which one it is.
    """
    try:
        ser = open_serial(port, timeout=DETECT_TIMEOUT)
    except (serial.SerialException, OSError) as exc:
        raise PortOpenError(describe_open_error(port, exc)) from exc
    try:
        with ser:
            ser.reset_input_buffer()
            ser.write(HELLO_CMD)
            while True:
                line = ser.readline().decode("utf-8", errors="replace")
                if not line:
                    break
                for dev in KNOWN_DEVICES:
                    if dev in line:
                        return dev
                # Also try parsing JSON hello: {"device":"MiniPAR",...}
                try:
                    obj = json.loads(line.strip())
                    device_name = obj.get("device", "")
                    if device_name in KNOWN_DEVICES:
                        return device_name
                except (json.JSONDecodeError, AttributeError):
                    pass
    except (serial.SerialException, OSError):
        pass
    return None

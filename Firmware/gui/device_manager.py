"""
device_manager.py — Serial port enumeration and autodetection for CO2Dot / MiniPAR.
"""

import json
import serial
import serial.tools.list_ports


BAUD_RATE = 115200
HELLO_CMD = b"hello\n"
# Known device identifiers returned in the hello JSON "device" field
KNOWN_DEVICES = ("CO2Dot", "MiniPAR")
DETECT_TIMEOUT = 2.0  # seconds to wait for hello response


def list_ports() -> list[str]:
    """Return a list of available serial port names."""
    return [p.device for p in serial.tools.list_ports.comports()]


def check_port(port: str) -> str | None:
    """
    Try opening `port` at 115200 baud, send 'hello', and check for a
    known device greeting.  Returns the device type string (e.g. "CO2Dot",
    "MiniPAR") on success, or None if no known device responds.
    """
    try:
        with serial.Serial(port, BAUD_RATE, timeout=DETECT_TIMEOUT) as ser:
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



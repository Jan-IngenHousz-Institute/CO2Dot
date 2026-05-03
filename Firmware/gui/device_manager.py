"""
device_manager.py — Serial port enumeration and autodetection for  CO2Dot.
"""

import serial
import serial.tools.list_ports


BAUD_RATE = 115200
HELLO_CMD = b"hello\n"
HELLO_RESPONSE = "Hello CO2 meter ready"
DETECT_TIMEOUT = 2.0  # seconds to wait for hello response


def list_ports() -> list[str]:
    """Return a list of available serial port names."""
    return [p.device for p in serial.tools.list_ports.comports()]


def check_port(port: str) -> bool:
    """
    Try opening `port` at 115200 baud, send 'hello', and check for the
    expected greeting. Returns True if the device responds correctly.
    """
    try:
        with serial.Serial(port, BAUD_RATE, timeout=DETECT_TIMEOUT) as ser:
            ser.reset_input_buffer()
            ser.write(HELLO_CMD)
            # Read lines for up to DETECT_TIMEOUT seconds
            deadline = ser.timeout
            while True:
                line = ser.readline().decode("utf-8", errors="replace")
                if not line:
                    break
                if HELLO_RESPONSE in line:
                    return True
    except (serial.SerialException, OSError):
        pass
    return False



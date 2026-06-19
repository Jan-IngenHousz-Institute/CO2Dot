"""Raw diagnostic: dump exactly what the CO2Dot sends for spec commands."""
import time
import serial

ser = serial.Serial("COM15", 115200, timeout=0.2)
time.sleep(2.5)              # boot
ser.reset_input_buffer()

for cmd in ["spec", "spec_flash,1"]:
    ser.reset_input_buffer()
    ser.write((cmd + "\n").encode())
    t0 = time.time()
    buf = b""
    while time.time() - t0 < 5.0:
        buf += ser.read(ser.in_waiting or 1)
    print(f"--- {cmd!r}: {len(buf)} bytes ---")
    print(repr(buf[:1500]))

ser.close()

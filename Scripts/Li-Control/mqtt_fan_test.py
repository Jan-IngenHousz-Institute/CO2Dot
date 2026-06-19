"""One-off: verify the MQTT control-WRITE path on a benign control (fan rpm).

Reads the current fan setpoint, nudges it down, confirms the live Fan_speed
responds, then restores the original setpoint. Fan speed is recoverable and
does not change the gas-exchange setpoints (CO2/H2O/temp/light).
"""
import time

from li_mqtt import LI6800, SCRIPTS

FAN_CONSTANTS = f"{SCRIPTS}/fan_control/constants"

li = LI6800.connect()
li._client.subscribe(FAN_CONSTANTS)   # also watch the controller's own state
time.sleep(1.5)

before = li.raw(FAN_CONSTANTS)
orig = before.get("SetPoint")
print(f"BEFORE  Fan_speed={li.get('Fan_speed')}  setpoint={orig}  "
      f"target={before.get('Target')}  auto={before.get('Auto')}")

if not isinstance(orig, (int, float)):
    print("Could not read current fan setpoint; aborting (won't write an unknown restore).")
    li.close()
    raise SystemExit(1)

target = max(6000, int(orig) - 3000)
print(f"NUDGE   set fan_rpm -> {target}")
li.set(fan_rpm=target)
for _ in range(15):
    time.sleep(1)
    fs = li.get("Fan_speed")
    print(f"        Fan_speed={fs}")
    if isinstance(fs, (int, float)) and abs(fs - target) < 300:
        print("        -> reached nudge target")
        break

print(f"RESTORE set fan_rpm -> {int(orig)}")
li.set(fan_rpm=int(orig))
for _ in range(15):
    time.sleep(1)
    fs = li.get("Fan_speed")
    print(f"        Fan_speed={fs}")
    if isinstance(fs, (int, float)) and abs(fs - orig) < 300:
        print("        -> restored")
        break

after = li.raw(FAN_CONSTANTS)
print(f"AFTER   Fan_speed={li.get('Fan_speed')}  setpoint={after.get('SetPoint')}")
li.close()

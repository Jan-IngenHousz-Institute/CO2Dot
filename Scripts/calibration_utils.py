"""calibration_utils.py - one-file toolbox for the CO2Dot calibration jig (used by calibration_jig_demo.ipynb).

Devices
    CO2Dot              the sensor under calibration (USB serial, one JSON line per reply)
    LI6800              LI-COR LI-6800 over its internal MQTT bus: chamber CO2, H2O, flow, temperature
    IRLamp              IR source that heats the device (RP2040 + MCP4018 digipot, USB serial)
    Kiprim              bench supply that drives the Peltier cooler (USB serial)

Helpers
    connect()           autodetect everything; a missing device prints "cannot find ..." and is skipped
    TemperatureControl  PID on the CO2Dot board temperature: the lamp heats, the Peltier cools
    wait_stable()       block until a reading has stayed within a tolerance of its target
    openjii()           optional: send records to an openJII experiment

    import calibration_utils as cu
    jig = cu.connect()
    jig.dot.setup(led_ma=4, atime=100, astep=999, gain=5); print(jig.dot.measure())
    temp = cu.TemperatureControl(jig); temp.set(27.0); cu.wait_stable(temp.read, 27.0, tol_pct=1)
    jig.close()

Self-contained on purpose: the device code is ported from the hardware-tested modules of the calibration
campaigns (co2dot.py, li_mqtt.py, li_connect.py, optron.py, kiprim.py, film_control.py, openjii_stream.py).

Needs pyserial; paho-mqtt for the LI-6800 and openJII; requests for openJII only.

Safety: the lamp firmware switches itself off after 30 s without a command. The Kiprim has NO such watchdog:
if this Python process is killed, the Peltier keeps its last current until someone sets it to 0.
"""
from __future__ import annotations

import atexit
import json
import math
import os
import queue
import re
import socket
import ssl
import statistics
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import serial
from serial.tools import list_ports

ESP_VID = 0x303A        # CO2Dot: ESP32-C3 native USB
RP2040_VID = 0x2E8A     # IR lamp controller (MicroPython CDC)


class NotConnected:
    """Stands in for a device that was not found: falsy, and any use raises one clear error."""

    def __init__(self, name: str):
        self._name = name

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"<{self._name}: not connected>"

    def __getattr__(self, attr: str):
        if attr.startswith("_"):                # leave Python / IPython introspection alone
            raise AttributeError(attr)
        raise RuntimeError(f"cannot find {self._name}: connect it and re-run the autodetect cell")


def _finite(v) -> Optional[float]:
    """A finite number, else None."""
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    return float(v) if ok else None


def _num(v) -> Optional[float]:
    """A plausible temperature / humidity, else None (the LI-6800 reports 999.9 for an unplugged thermocouple)."""
    v = _finite(v)
    return v if (v is not None and v < 500) else None


def _short(exc: BaseException, n: int = 140) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:n]


# ----------------------------------------------------------------------------------------------------------- #
# psychrometrics
# ----------------------------------------------------------------------------------------------------------- #
def psat_kpa(t_c: float) -> float:
    """Saturation vapour pressure over water (Magnus), kPa."""
    return 0.61094 * math.exp(17.625 * t_c / (t_c + 243.04))


def dew_point_c(t_c: float, rh_pct: float) -> float:
    e = max(rh_pct, 0.1) / 100.0 * psat_kpa(t_c)
    a = math.log(e / 0.61094)
    return 243.04 * a / (17.625 - a)


def dew_point_from_h2o(h2o_mmol_mol: float, press_kpa: float) -> float:
    e = max(h2o_mmol_mol, 1e-3) / 1000.0 * press_kpa
    a = math.log(e / 0.61094)
    return 243.04 * a / (17.625 - a)


# ----------------------------------------------------------------------------------------------------------- #
# CO2Dot
# ----------------------------------------------------------------------------------------------------------- #
CO2DOT_BAUD = 115200
BOOT_SETTLE_S = 2.5      # opening the USB-CDC port resets the MCU, and the firmware waits ~2 s on boot


def led_ma_actual(requested: int) -> int:
    """LED current the firmware actually drives: 0 = off, else 4 mA minimum, 2 mA steps, 20 mA maximum."""
    if requested <= 0:
        return 0
    r = min(max(int(requested), 4), 20)
    return 4 + ((r - 4) // 2) * 2


def _read_json_objects(ser: "serial.Serial", timeout_s: float):
    """Yield the JSON objects that arrive until the timeout.

    Bytes are accumulated and split on newlines, so a reply that arrives in several USB chunks is put back
    together before parsing (readline() with a short timeout can return an unparseable fragment)."""
    deadline = time.monotonic() + timeout_s
    buf = bytearray()
    while time.monotonic() < deadline:
        n = ser.in_waiting
        chunk = ser.read(n if n else 1)
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
    if "error" in obj:
        return str(obj["error"])
    for v in obj.values():
        if isinstance(v, dict) and "error" in v:
            return str(v["error"])
    return None


class CO2Dot:
    """USB-serial handle to a CO2Dot. The port is opened once and kept open."""

    def __init__(self, ser: "serial.Serial", port: str):
        self._ser = ser
        self.port = port
        self.id = f"port:{port}"            # USB serial number (the chip MAC) when the port reports one
        self.firmware: Dict[str, Any] = {}
        self.gas: Optional[bool] = None     # BME gas heater state; None = this firmware cannot switch it
        self.led_ma = 4                     # LED current used by measure() / spec_flash()
        self._lock = threading.RLock()      # the temperature loop and the notebook share this port

    @classmethod
    def connect(cls, port: Optional[str] = None, *, exclude=(), gas: Optional[bool] = False,
                settle_s: float = BOOT_SETTLE_S, hello_timeout_s: float = 4.0) -> "CO2Dot":
        """Open the CO2Dot on the given port, or on the first Espressif USB port that answers `hello`.

        Only ports with the Espressif USB id are tried, so no other instrument is opened or written to; a
        CO2Dot behind a USB-UART adapter needs its port named.
        gas=False switches the BME gas heater off (firmware >= 1.1): its 320 C plate warms the board at every
        reading. gas=True switches it on, gas=None leaves the firmware default."""
        infos = {p.device: p for p in list_ports.comports()}
        if port:
            candidates = [port]
        else:
            candidates = sorted(p.device for p in infos.values() if p.vid == ESP_VID and p.device not in exclude)
        if not candidates:
            raise RuntimeError("no Espressif USB port (id 303A) present")
        last = ""
        for name in candidates:
            try:
                ser = serial.Serial(name, CO2DOT_BAUD, timeout=0.2, write_timeout=2.0)
            except (serial.SerialException, OSError) as exc:
                last = f"{name}: {_short(exc, 60)}"
                continue
            try:
                time.sleep(settle_s)
                ser.reset_input_buffer()
                ser.write(b"hello\n")
                hello = next((o for o in _read_json_objects(ser, hello_timeout_s) if o.get("device") == "CO2Dot"),
                             None)
                if hello is None:
                    ser.close()
                    last = f"{name}: no CO2Dot hello"
                    continue
            except (serial.SerialException, OSError) as exc:
                try:
                    ser.close()
                except Exception:
                    pass
                last = f"{name}: {_short(exc, 60)}"
                continue
            dot = cls(ser, name)
            dot.firmware = hello
            info = infos.get(name)
            if info is not None and info.serial_number:
                dot.id = info.serial_number
            if gas is not None:
                try:
                    dot.gas = dot.gas_heater(gas)
                except Exception as exc:    # firmware 1.0 has no bme_gas: the plate heats at every reading
                    print(f"  {name}: WARNING BME gas heater not settable ({_short(exc, 60)})")
            return dot
        raise RuntimeError(f"no CO2Dot answered ({last})")

    def _command(self, cmd: str, expect_keys, timeout_s: float = 6.0, retries: int = 1) -> Dict[str, Any]:
        """Send a command; return the first JSON reply that contains any of expect_keys."""
        keys = (expect_keys,) if isinstance(expect_keys, str) else tuple(expect_keys)
        with self._lock:
            for _ in range(retries + 1):
                self._ser.reset_input_buffer()
                self._ser.write((cmd + "\n").encode())
                for obj in _read_json_objects(self._ser, timeout_s):
                    if any(k in obj for k in keys):
                        return obj
                    err = _find_error(obj)
                    if err:
                        raise RuntimeError(f"CO2Dot error for '{cmd}': {err}")
        raise TimeoutError(f"no {keys} reply to '{cmd}' within {timeout_s:.0f} s")

    # -- the two calls of the demo ---------------------------------------------------------------------------- #
    def setup(self, led_ma: Optional[int] = None, atime: Optional[int] = None, astep: Optional[int] = None,
              gain: Optional[int] = None) -> Dict[str, Any]:
        """LED current, integration time and gain in one call; returns the settings now in force.

        led_ma  LED current for the flash (driven as 4..20 mA in 2 mA steps)
        atime   integration steps 0-255, astep step size 0-65534: time = (atime+1) x (astep+1) x 2.78 us
        gain    0=0.5x 1=1x 2=2x 3=4x 4=8x 5=16x 6=32x 7=64x 8=128x 9=256x 10=512x"""
        if led_ma is not None:
            self.led_ma = int(led_ma)
        if atime is not None or astep is not None or gain is not None:
            self.set_spectrometer(atime=atime, astep=astep, gain=gain)
        st = self.spec_status()
        out = {"led_ma": led_ma_actual(self.led_ma), "atime": st.get("atime"), "astep": st.get("astep"),
               "gain": st.get("gain"), "integration_ms": None}
        if isinstance(out["atime"], int) and isinstance(out["astep"], int):
            out["integration_ms"] = round((out["atime"] + 1) * (out["astep"] + 1) * 2.78e-3, 1)
        return out

    def measure(self, led_ma: Optional[int] = None) -> Dict[str, Any]:
        """One measurement: LED flash (lit minus dark, per channel) and the board environment."""
        flash = self.spec_flash(led_ma)
        env = self.env()
        out = {"diff": flash["diff"], "T": env.get("T"), "RH": env.get("RH"), "P": env.get("P")}
        if env.get("Gas") is not None:
            out["Gas"] = env["Gas"]
        return out

    # -- commands --------------------------------------------------------------------------------------------- #
    def hello(self, timeout_s: float = 4.0) -> Dict[str, Any]:
        return self._command("hello", "device", timeout_s)

    def spec_status(self, timeout_s: float = 4.0) -> Dict[str, Any]:
        """{model, available, atime, astep, gain, led}."""
        return self._command("spec_status", ("available", "model"), timeout_s)

    def spec_flash(self, led_ma: Optional[int] = None, timeout_s: float = 8.0) -> Dict[str, Any]:
        """Dark reading, LED-on reading and their difference: {'led_current', 'model', 'dark', 'lit', 'diff'}."""
        ma = self.led_ma if led_ma is None else int(led_ma)
        obj = self._command(f"spec_flash,{ma}", "diff", timeout_s)
        return {"led_current": ma, "model": obj.get("model"),
                "dark": obj.get("dark", {}), "lit": obj.get("lit", {}), "diff": obj.get("diff", {})}

    def set_spectrometer(self, *, atime: Optional[int] = None, astep: Optional[int] = None,
                         gain: Optional[int] = None, timeout_s: float = 4.0) -> Dict[str, Any]:
        if atime is not None and not 0 <= atime <= 255:
            raise ValueError(f"atime must be 0-255, got {atime}")
        if astep is not None and not 0 <= astep <= 65534:
            raise ValueError(f"astep must be 0-65534, got {astep}")
        if gain is not None and not 0 <= gain <= 12:
            raise ValueError(f"gain must be 0-12, got {gain}")
        keys = ("atime", "astep", "gain")
        config: Dict[str, Any] = {}
        for cmd, val in (("spec_set_atime", atime), ("spec_set_astep", astep), ("spec_set_gain", gain)):
            if val is not None:
                config = self._command(f"{cmd},{int(val)}", keys, timeout_s)
        return {k: config.get(k) for k in keys}

    def env(self, n: Optional[int] = None, timeout_s: float = 4.0) -> Dict[str, Any]:
        """Board environment {'T', 'P', 'RH', 'Gas'}; firmware >= 1.2 returns the mean of n readings
        (device default 3). 'Gas' is None while the gas heater is off."""
        obj = self._command("env" if n is None else f"env,{int(n)}", "T", timeout_s)
        return {k: obj.get(k) for k in ("T", "P", "RH", "Gas")}

    def gas_heater(self, enable: Optional[bool] = None, timeout_s: float = 4.0) -> bool:
        """BME gas measurement on/off (None only asks). Returns the device state."""
        cmd = "bme_gas" if enable is None else f"bme_gas,{1 if enable else 0}"
        self.gas = bool(self._command(cmd, "gas_heater", timeout_s)["gas_heater"])
        return self.gas

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------------------------------------- #
# LI-6800: discovery on the wired link (Windows) and the MQTT bus of the sensor head
# ----------------------------------------------------------------------------------------------------------- #
BROKER_PORT = 1883
LI_BASE = "licor/li6850"                              # the head publishes everything under this prefix
LI_SCRIPTS = f"{LI_BASE}/scripts"
TOPIC_DATA = f"{LI_BASE}/output/DATA"                 # measured values (CO2_r, H2O_s, Tleaf, ...)
TOPIC_DATACOMP = f"{LI_BASE}/computed/DATACOMP"       # computed gas exchange (A, gsw, Ci, RHcham, ...)
TOPIC_DATASTAT = f"{LI_BASE}/output/DATASTAT"
LI_DATA_TOPICS = [TOPIC_DATA, TOPIC_DATACOMP, TOPIC_DATASTAT]


def _li_update(name: str) -> str:
    return f"{LI_SCRIPTS}/{name}/constants/update"


# keyword -> (topic, base payload, key that carries the value). A value of None switches that controller off.
LI_CONTROLS: Dict[str, Tuple[str, Dict[str, Any], str]] = {
    "co2_r":    (_li_update("co2_control"),  {"Active": True, "Auto": True, "Scrub": "auto", "Target": "CO2_r"}, "SetPoint"),
    "co2_s":    (_li_update("co2_control"),  {"Active": True, "Auto": True, "Scrub": "auto", "Target": "CO2_s"}, "SetPoint"),
    "co2_pct":  (_li_update("co2_control"),  {"Active": True, "Auto": False, "Scrub": "auto"}, "Percent"),
    "flow":     (_li_update("flow_control"), {"Active": True, "Auto": True}, "SetPoint"),
    "flow_pct": (_li_update("flow_control"), {"Active": True, "Auto": False}, "Percent"),
    "txchg":    (_li_update("temp_control"), {"Active": True, "Target": "Txchg", "Auto": True}, "SetPoint"),
    "tair":     (_li_update("temp_control"), {"Active": True, "Target": "Tair", "Auto": True}, "SetPoint"),
    "tleaf":    (_li_update("temp_control"), {"Active": True, "Target": "Tleaf", "Auto": True}, "SetPoint"),
    "h2o_r":    (_li_update("h2o_control"),  {"Active": True, "Target": "H2O_r", "Type": "Fully-Auto"}, "SetPoint"),
    "h2o_s":    (_li_update("h2o_control"),  {"Active": True, "Target": "H2O_s", "Type": "Fully-Auto"}, "SetPoint"),
    "vpd_leaf": (_li_update("h2o_control"),  {"Active": True, "Target": "VPD_leaf", "Type": "Fully-Auto"}, "SetPoint"),
    "sd_air":   (_li_update("h2o_control"),  {"Active": True, "Target": "SD_air", "Type": "Fully-Auto"}, "SetPoint"),
    "rh_air":   (_li_update("h2o_control"),  {"Active": True, "Target": "RH_air", "Type": "Fully-Auto"}, "SetPoint"),
    "fan_rpm":  (_li_update("fan_control"),  {"Active": True, "Target": "RPM", "Auto": True}, "SetPoint"),
}


def _paho():
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        raise RuntimeError("the paho-mqtt package is not installed (pip install paho-mqtt)") from None
    return mqtt


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _wired_iface(interface_hint: str) -> Tuple[int, str]:
    """(ifindex, name) of the first connected network interface whose name contains the hint."""
    r = _run(["netsh", "interface", "ipv6", "show", "interfaces"])
    if r.returncode != 0:
        raise RuntimeError(f"netsh failed: {r.stderr or r.stdout}")
    for line in r.stdout.splitlines():
        m = re.match(r"^\s*(\d+)\s+\d+\s+\d+\s+(\w+)\s+(.+?)\s*$", line)
        if not m:
            continue
        idx, state, name = int(m.group(1)), m.group(2).lower(), m.group(3).strip()
        if interface_hint.lower() in name.lower() and state == "connected":
            return idx, name
    raise RuntimeError(f"no connected network interface named like '{interface_hint}' (cable plugged in?)")


def _tcp_open(addr: str, ifindex: int, port: int, timeout: float = 2.0) -> bool:
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((addr, port, 0, ifindex))
        return True
    except OSError:
        return False
    finally:
        s.close()


def discover_brokers(interface_hint: str = "Ethernet") -> List[str]:
    """'fe80::..%<ifindex>' of every link-local neighbour on the wired interface with an open MQTT port.

    The LI-6800 is cabled straight to the PC and only has a link-local IPv6 address. More than one of its
    nodes listens on 1883 (head, fluorometer); LI6800.connect() keeps the one that streams data."""
    if os.name != "nt":
        raise RuntimeError("automatic discovery needs Windows (netsh); pass the broker address instead")
    idx, name = _wired_iface(interface_hint)
    _run(["ping", "-6", "-n", "2", "-w", "1000", f"ff02::1%{idx}"])     # fill the neighbour cache
    time.sleep(1.5)
    addrs: List[str] = []
    for line in _run(["netsh", "interface", "ipv6", "show", "neighbors", f"interface={idx}"]).stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower().startswith("fe80::"):
            if parts[-1].lower() not in ("unreachable", "incomplete") and parts[0] not in addrs:
                addrs.append(parts[0])
    hosts = [f"{a}%{idx}" for a in sorted(addrs) if _tcp_open(a, idx, BROKER_PORT)]
    if not hosts:
        raise RuntimeError(f"no MQTT broker among the neighbours on '{name}' (instrument on and cabled?)")
    return hosts


class LI6800:
    """MQTT handle to a LI-6800.

        li.read()                        every live value as a dict
        li.get("CO2_s")                  one value
        li.set(co2_s=400, tair=25)       setpoints; they drive the instrument immediately
    """

    def __init__(self, broker: str, port: int = BROKER_PORT):
        mqtt = _paho()
        self.broker, self.port = broker, port
        # a unique client id per handle: the broker drops an existing session when a second client connects
        # with the same id, so two handles sharing one id would keep kicking each other offline
        client_id = f"li-pc-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:                                                    # paho-mqtt 2.x
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id, protocol=mqtt.MQTTv31)
        except (AttributeError, TypeError):                     # paho-mqtt 1.x
            self._client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv31)
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._topics = list(LI_DATA_TOPICS)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    @classmethod
    def connect(cls, interface_hint: str = "Ethernet", *, broker: Optional[str] = None,
                settle_s: float = 15.0) -> "LI6800":
        """Find the broker (unless given) and wait until data flows, at most settle_s per candidate."""
        _paho()                                                 # fail before the network search, not after
        candidates = [broker] if broker else discover_brokers(interface_hint)
        last = "no candidates"
        for host in candidates:
            self = cls(host)
            try:
                self._client.connect(self.broker, self.port, keepalive=30)
            except OSError as e:
                last = f"{host}: {e}"
                continue
            self._client.loop_start()
            t0 = time.time()
            while time.time() - t0 < settle_s:
                if self._latest:
                    return self
                time.sleep(0.1)
            self.close()
            last = f"{host}: connected but no data within {settle_s:.0f} s"
        raise RuntimeError(f"no LI-6800 data broker. Last: {last}")

    def _on_connect(self, client, userdata, flags, rc, *a):
        for t in self._topics:
            client.subscribe(t)

    def _on_message(self, client, userdata, msg):
        try:
            self._latest[msg.topic] = json.loads(msg.payload.decode("utf-8", "replace"))
        except Exception:
            pass

    def close(self) -> None:
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            pass

    # -- reading ---------------------------------------------------------------------------------------------- #
    def read(self) -> Dict[str, Any]:
        """Every live value: measured (DATA) merged with computed (DATACOMP).

        RHcham is recomputed the way the LI-6800 does when the console does not publish its computed stream;
        'RHcham_recomputed' marks that case."""
        r: Dict[str, Any] = {}
        r.update(self._latest.get(TOPIC_DATA, {}))
        r.update(self._latest.get(TOPIC_DATACOMP, {}))
        if r.get("RHcham") is None and all(isinstance(r.get(k), (int, float)) for k in ("H2O_s", "Press", "Tchamber")):
            t = r["Tchamber"]
            vp = r["H2O_s"] * (r["Press"] + (r.get("Pchamber") or 0.0)) / 1000.0
            r["RHcham"] = 100.0 * vp / (0.61365 * math.exp(17.502 * t / (240.97 + t)))
            r["RHcham_recomputed"] = True
        return r

    def get(self, key: str, default: Any = None) -> Any:
        return self.read().get(key, default)

    # -- control ---------------------------------------------------------------------------------------------- #
    def set(self, **setpoints: Any) -> None:
        """Setpoints: co2_r, co2_s, co2_pct, flow, flow_pct, txchg, tair, tleaf, h2o_r, h2o_s, vpd_leaf,
        sd_air, rh_air, fan_rpm. None switches that controller off. Example: li.set(co2_s=400, rh_air=50)."""
        for key, value in setpoints.items():
            if key not in LI_CONTROLS:
                raise KeyError(f"unknown control {key!r}; valid: {sorted(LI_CONTROLS)}")
            topic, template, value_key = LI_CONTROLS[key]
            payload = dict(template)
            if value is None:
                payload["Active"] = False
            else:
                payload[value_key] = value
            self._client.publish(topic, json.dumps(payload))

    def controller(self, name: str, wait_s: float = 2.0) -> Dict[str, Any]:
        """State the instrument publishes for a controller ('co2', 'h2o', 'flow', 'temp', 'fan'); {} if none."""
        topic = f"{LI_SCRIPTS}/{name}_control/constants"
        if topic not in self._topics:
            self._topics.append(topic)
            self._client.subscribe(topic)
            t0 = time.time()
            while topic not in self._latest and time.time() - t0 < wait_s:
                time.sleep(0.1)
        return self._latest.get(topic, {})

    def safe_state(self, mode: str = "off", *, flow: float = 400.0, block_c: float = 25.0,
                   co2_ppm: float = 420.0, rh_pct: float = 50.0, verbose: bool = True) -> Dict[str, Any]:
        """Leave the instrument safe: flow on (no condensation) and the block at block_c.

        mode "off"      CO2 and H2O control off. If the instrument then still reports one of them active,
                        "ambient" is applied instead.
        mode "ambient"  CO2 at co2_ppm, RH at rh_pct."""
        say = print if verbose else (lambda *a, **k: None)
        out: Dict[str, Any] = {"mode": mode}
        self.controller("co2"), self.controller("h2o")          # start listening before the change
        if mode == "off":
            self.set(co2_s=None)
            self.set(rh_air=None)
        else:
            self.set(co2_s=co2_ppm, rh_air=rh_pct)
        now = _finite(self.get("Flow"))
        if now is None or now < 50.0:
            self.set(flow=flow)
            out["flow"] = f"was {'unknown' if now is None else round(now)}, set to {flow:.0f} umol/s"
        else:
            out["flow"] = f"on ({now:.0f} umol/s, left as it is)"
        self.set(txchg=block_c)
        if mode == "off":
            time.sleep(2.0)                                      # let the controllers publish their new state
            active = {n: self.controller(n).get("Active") for n in ("co2", "h2o")}
            if any(v is True for v in active.values()):
                self.set(co2_s=co2_ppm, rh_air=rh_pct)
                out["mode"] = "ambient"
                out["why"] = f"still active after the off command: {active}"
            else:
                out["confirmed"] = all(v is False for v in active.values())
        if out["mode"] == "off":
            note = "confirmed" if out.get("confirmed") else "sent, not confirmed by the instrument"
            say(f"LI-6800: CO2 and H2O control off ({note}); flow {out['flow']}; block {block_c:.0f} C")
        else:
            why = f" ({out['why']})" if "why" in out else ""
            say(f"LI-6800: CO2 {co2_ppm:.0f} ppm, RH {rh_pct:.0f} %{why}; flow {out['flow']}; block {block_c:.0f} C")
        return out


# ----------------------------------------------------------------------------------------------------------- #
# IR lamp (RP2040 + MCP4018 digipot; firmware optron-control/main.py)
# ----------------------------------------------------------------------------------------------------------- #
# One command per line, one JSON reply per line:
#   ping -> {"ok":true,"pong":true,"fw":"optron 1.0","max":25}
#   set <0-25> | off | get -> {"ok":true,"target":N,"wiper":N,"max":25,"wd":false,...}   (wiper = read back)
# The firmware clamps the wiper to 25 of 127 (20 %), ramps one step per 125 ms, and switches the lamp off after
# 30 s without a command.
LAMP_HEARTBEAT_S = 10.0


class LampError(RuntimeError):
    pass


class LampCommandError(LampError):
    """The firmware answered ok=false (bad command or argument); not retried."""


class IRLamp:
    def __init__(self, port: Optional[str] = None, *, exclude_ports=(), timeout_s: float = 1.0):
        if port is None:
            port = next((p.device for p in list_ports.comports()
                         if p.vid == RP2040_VID and p.device not in exclude_ports), None)
        if port is None:
            raise LampError("no RP2040 (USB VID 0x2E8A) on any serial port")
        self.port = port
        self._ser = serial.Serial(port, baudrate=115200, timeout=timeout_s, write_timeout=timeout_s)
        self._lock = threading.RLock()
        self._target = 0
        time.sleep(0.2)
        self._ser.reset_input_buffer()
        try:
            info = self._cmd("ping")
        except Exception:
            self._ser.close()
            raise
        self.fw = info.get("fw")
        self.max_raw = int(info.get("max", 25))
        # re-send the target so the firmware watchdog stays quiet while the lamp is on; if this process dies
        # the heartbeat stops and the firmware turns the lamp off by itself
        self._hb_stop = threading.Event()
        threading.Thread(target=self._heartbeat, name="lamp-heartbeat", daemon=True).start()

    def _cmd(self, line: str, retries: int = 2) -> Dict[str, Any]:
        with self._lock:
            for attempt in range(retries + 1):
                try:
                    self._ser.reset_input_buffer()
                    self._ser.write((line.strip() + "\n").encode())
                    deadline = time.monotonic() + self._ser.timeout * 2
                    while time.monotonic() < deadline:
                        raw = self._ser.readline()
                        if not raw:
                            break
                        s = raw.decode(errors="replace").strip()
                        if not s.startswith("{"):
                            continue                            # MicroPython banner or a stray print
                        obj = json.loads(s)
                        if not obj.get("ok", False):
                            raise LampCommandError(obj.get("error", "firmware error"))
                        return obj
                    raise LampError(f"no reply to {line!r}")
                except LampCommandError:
                    raise
                except (serial.SerialException, LampError, json.JSONDecodeError):
                    if attempt == retries:
                        raise
                    time.sleep(0.1)
        raise LampError("unreachable")

    def _heartbeat(self) -> None:
        while not self._hb_stop.wait(LAMP_HEARTBEAT_S):
            try:
                with self._lock:
                    self._cmd(f"set {self._target}" if self._target > 0 else "get")
            except Exception:
                pass                                            # the firmware watchdog is the safety net

    def set_raw(self, value: int) -> Dict[str, Any]:
        """Lamp power as the raw wiper position, 0..max_raw."""
        v = int(round(value))
        if not 0 <= v <= self.max_raw:
            raise ValueError(f"lamp {v} outside 0..{self.max_raw}")
        with self._lock:
            self._target = v
            return self._cmd(f"set {v}")

    def get(self) -> Dict[str, Any]:
        return self._cmd("get")

    def off(self) -> Dict[str, Any]:
        with self._lock:
            self._target = 0
            return self._cmd("off")

    @property
    def target(self) -> int:
        return self._target

    def close(self) -> None:
        self._hb_stop.set()
        try:
            self.off()
        except Exception:
            pass
        try:
            self._ser.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------------------------------------- #
# Peltier supply (Kiprim DC310Pro)
# ----------------------------------------------------------------------------------------------------------- #
# Plain text at 115200 baud: "voltage 7.000\r\n", "current 1.250\r\n" (no reply), "*IDN?\n" -> "KIPRIM,...".
# The Peltier cools at 7 V with up to 2 A, so the voltage limit is set once and the CURRENT is modulated
# (the supply is in constant-current mode over that range). Safe state = current 0.
KIPRIM_BAUD = 115200
V_LIMIT = 7.0           # volts; hard clamp
I_MAX = 2.0             # amps; hard clamp
KIPRIM_HEARTBEAT_S = 10.0


class KiprimError(RuntimeError):
    pass


def _as_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.strip().split()[0].rstrip("VA"))
    except ValueError:
        return None


def _kiprim_on(port: str, timeout_s: float = 1.0) -> bool:
    try:
        with serial.Serial(port, baudrate=KIPRIM_BAUD, timeout=timeout_s, write_timeout=timeout_s) as ser:
            ser.reset_input_buffer()
            ser.write(b"*IDN?\n")
            return "KIPRIM" in ser.readline().decode(errors="replace").upper()
    except (serial.SerialException, OSError):
        return False


class Kiprim:
    #: the DC310Pro drops a command that arrives while it processes the previous one ("voltage 7" directly
    #: followed by "current 2.5" left the current unchanged), so commands are spaced out
    CMD_GAP_S = 0.3

    def __init__(self, port: Optional[str] = None, *, exclude_ports=(), v_limit: float = V_LIMIT,
                 i_max: float = I_MAX, timeout_s: float = 1.0):
        if port is None:    # never probe the lamp controller or a CO2Dot with *IDN?
            port = next((p.device for p in list_ports.comports()
                         if p.device not in exclude_ports and p.vid not in (RP2040_VID, ESP_VID)
                         and _kiprim_on(p.device)), None)
        if port is None:
            raise KiprimError("no supply answered *IDN? with KIPRIM")
        self.port = port
        self.v_limit = min(float(v_limit), V_LIMIT)
        self.i_max = min(float(i_max), I_MAX)
        self._ser = serial.Serial(port, baudrate=KIPRIM_BAUD, timeout=timeout_s, write_timeout=timeout_s)
        self._lock = threading.RLock()
        self._last_cmd_t = 0.0
        self._amps = 0.0
        time.sleep(0.2)
        self.idn = self._query("*IDN?\n") or ""
        if "KIPRIM" not in self.idn.upper():
            self._ser.close()
            raise KiprimError(f"{port}: not a Kiprim ({self.idn!r})")
        # optional read-back: used only if this unit answers it (OUTPut is never sent blindly)
        self._meas_v = _as_float(self._query("MEASure:VOLTage?\n")) is not None
        self._meas_i = _as_float(self._query("MEASure:CURRent?\n")) is not None
        self.set_current(0.0)                   # safe state first, then the voltage limit
        self.set_voltage(self.v_limit)
        # the supply has no link watchdog; re-sending the setpoint only covers a dropped command
        self._hb_stop = threading.Event()
        threading.Thread(target=self._heartbeat, name="kiprim-heartbeat", daemon=True).start()

    def _space(self) -> None:
        wait = self.CMD_GAP_S - (time.monotonic() - self._last_cmd_t)
        if wait > 0:
            time.sleep(wait)
        self._last_cmd_t = time.monotonic()

    def _write(self, cmd: str) -> None:
        with self._lock:
            try:
                self._space()
                self._ser.write(cmd.encode())
                self._ser.flush()
            except serial.SerialException as e:
                raise KiprimError(str(e)) from e

    def _query(self, cmd: str) -> Optional[str]:
        with self._lock:
            try:
                self._space()
                self._ser.reset_input_buffer()
                self._ser.write(cmd.encode())
                return self._ser.readline().decode(errors="replace").strip() or None
            except serial.SerialException as e:
                raise KiprimError(str(e)) from e

    def _heartbeat(self) -> None:
        while not self._hb_stop.wait(KIPRIM_HEARTBEAT_S):
            try:
                self._write(f"current {self._amps:.3f}\r\n")
            except Exception:
                pass

    def set_voltage(self, volts: float) -> None:
        v = max(0.0, min(float(volts), self.v_limit))
        self._write(f"voltage {v:.3f}\r\n")

    def set_current(self, amps: float) -> None:
        """Peltier current in amps, clamped to 0..i_max. 0 = not cooling."""
        a = max(0.0, min(float(amps), self.i_max))
        self._amps = a
        self._write(f"current {a:.3f}\r\n")

    def off(self) -> None:
        self.set_current(0.0)

    @property
    def amps(self) -> float:
        return self._amps

    def measure(self) -> Dict[str, Optional[float]]:
        """{'volt', 'amps'} measured at the output; None where the supply has no read-back."""
        out: Dict[str, Optional[float]] = {"volt": None, "amps": None}
        if self._meas_v:
            out["volt"] = _as_float(self._query("MEASure:VOLTage?\n"))
        if self._meas_i:
            out["amps"] = _as_float(self._query("MEASure:CURRent?\n"))
        return out

    def close(self) -> None:
        self._hb_stop.set()
        try:
            self.off()
        except Exception:
            pass
        try:
            self._ser.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------------------------------------- #
# the jig: autodetect, one full record, safe close
# ----------------------------------------------------------------------------------------------------------- #
class Jig:
    """What connect() returns: jig.dot, jig.lamp, jig.peltier, jig.li (each a device or a placeholder)."""

    def __init__(self):
        self.dot: Any = NotConnected("CO2Dot")
        self.lamp: Any = NotConnected("IR lamp")
        self.peltier: Any = NotConnected("Peltier supply")
        self.li: Any = NotConnected("LI-6800")
        self.temp: Optional["TemperatureControl"] = None     # set by TemperatureControl(jig)
        self.stream: Any = None                             # set by openjii(..., jig)
        self._cycle = 0
        self._closed = False
        atexit.register(self._heat_cool_off)                # lamp and Peltier never outlive the process

    def __repr__(self) -> str:
        return (f"Jig(dot={self.dot!r}, lamp={self.lamp!r}, peltier={self.peltier!r}, li={self.li!r})")

    def snapshot(self, led_ma: Optional[int] = None) -> Dict[str, Any]:
        """One reading of everything connected, in the layout of a calibration-campaign cycle:
        {ts, t_mono, cycle, phase, licor, devices: {id: {port, spec_flash, env}}, film}."""
        self._cycle += 1
        line: Dict[str, Any] = {"ts": datetime.now().isoformat(timespec="milliseconds"),
                                "t_mono": time.monotonic(), "cycle": self._cycle, "phase": {"kind": "demo"},
                                "licor": self.li.read() if self.li else {}, "devices": {}}
        if self.dot:
            rec: Dict[str, Any] = {"port": self.dot.port}
            try:
                rec["spec_flash"] = self.dot.spec_flash(led_ma)
                rec["env"] = self.dot.env()
            except Exception as e:
                rec["error"] = _short(e)
            line["devices"][self.dot.id] = rec
        if self.temp is not None:
            line["film"] = self.temp.status()
        return line

    def _heat_cool_off(self) -> None:
        if self.temp is not None:
            try:
                self.temp.off()
            except Exception:
                pass
        for dev in (self.lamp, self.peltier):
            if dev:
                try:
                    dev.off()
                except Exception:
                    pass

    def close(self, licor: Optional[str] = "off") -> None:
        """Lamp and Peltier off (checked), LI-6800 left safe, ports closed.

        licor "off": CO2 and H2O control off, flow on, block 25 C (falls back to "ambient" if the instrument
        still reports them active); "ambient": CO2 420 ppm, RH 50 %, flow on, block 25 C; None: untouched."""
        if self._closed:
            return
        self._heat_cool_off()
        if self.lamp:
            try:
                st, t0 = self.lamp.get(), time.monotonic()
                while (st.get("target") or st.get("wiper")) and time.monotonic() - t0 < 5.0:
                    time.sleep(0.3)                         # the firmware ramps down one step per 125 ms
                    st = self.lamp.get()
                ok = not st.get("target") and st.get("wiper") == 0
                print(f"IR lamp {'off' if ok else 'NOT CONFIRMED OFF'} (target {st.get('target')}, wiper {st.get('wiper')})")
            except Exception as e:
                print(f"IR lamp: off sent, state not readable ({_short(e)})")
        if self.peltier:
            try:
                time.sleep(0.5)
                amps = self.peltier.measure().get("amps")
                if amps is None:
                    print("Peltier: 0 A sent, not confirmed (the supply has no read-back)")
                else:
                    print(f"Peltier {'off' if amps < 0.05 else 'NOT CONFIRMED OFF'} ({amps:.2f} A measured)")
            except Exception as e:
                print(f"Peltier: 0 A sent, state not readable ({_short(e)})")
        if self.li and licor is not None:
            try:
                self.li.safe_state(licor)
            except Exception as e:
                print(f"LI-6800: could not set the safe state ({_short(e)})")
        if self.stream:
            try:
                self.stream.close()
            except Exception as e:
                print(f"openJII: {_short(e)}")
        for name in ("lamp", "peltier", "dot", "li"):
            dev = getattr(self, name)
            if dev:
                try:
                    dev.close()
                except Exception:
                    pass
        self.dot, self.lamp = NotConnected("CO2Dot"), NotConnected("IR lamp")
        self.peltier, self.li = NotConnected("Peltier supply"), NotConnected("LI-6800")
        self._closed = True
        print("ports closed")


def connect(dot=None, lamp=None, peltier=None, li=None) -> Jig:
    """Find and open every device of the jig. One line is printed per device; a device that is not there
    prints "cannot find ..." and is skipped.

    Each argument: None = autodetect, False = skip, or the address to use
    (dot / lamp / peltier: serial port such as "COM24"; li: broker such as "fe80::21c:94ff:fe04:8dd4%7")."""
    jig = Jig()
    taken: set = set()

    def attempt(label: str, attr: str, arg, opener: Callable[[], Any], describe: Callable[[Any], str]) -> None:
        if arg is False:
            print(f"{label:<15} skipped")
            return
        try:
            dev = opener()
        except Exception as e:
            print(f"cannot find {label} ({_short(e)})")
            return
        setattr(jig, attr, dev)
        if isinstance(getattr(dev, "port", None), str):     # serial devices; the LI-6800's port is a TCP port
            taken.add(dev.port)
        print(f"{label:<15} {describe(dev)}")

    attempt("IR lamp", "lamp", lamp, lambda: IRLamp(lamp or None, exclude_ports=taken),
            lambda d: f"{d.port}  {d.fw}, max raw {d.max_raw}")
    attempt("CO2Dot", "dot", dot, lambda: CO2Dot.connect(dot or None, exclude=taken),
            lambda d: (f"{d.port}  firmware {d.firmware.get('version', '?')}, id {d.id}, gas sensing "
                       f"{'unknown' if d.gas is None else 'on' if d.gas else 'off'}"))
    attempt("Peltier supply", "peltier", peltier, lambda: Kiprim(peltier or None, exclude_ports=taken),
            lambda d: f"{d.port}  {d.idn}, limits {d.v_limit:g} V / {d.i_max:g} A, now {d.measure()}")
    attempt("LI-6800", "li", li, lambda: LI6800.connect(broker=li or None),
            lambda d: f"{d.broker}  {len(d.read())} live values")
    return jig


# ----------------------------------------------------------------------------------------------------------- #
# temperature control: the lamp heats, the Peltier cools, the CO2Dot board temperature is regulated
# ----------------------------------------------------------------------------------------------------------- #
class TemperatureControl:
    """PID on the CO2Dot board temperature (BME), running in a background thread.

    One output u in [-1, +1], split range: u > 0 drives the lamp, u < 0 the Peltier current, with a dead band
    so the two never run together.

        temp = TemperatureControl(jig, v_max=7.0, i_max=2.0, lamp_max=16, t_max=40.0)
        temp.pid(kp=0.15, ki=0.0006)       # change gains, also while running
        temp.set(27.0)                     # start (or move) the target, in C
        temp.status()                      # {'sp', 'pv', 'u', 'lamp', 'amps', 'trip', ...}
        temp.off()                         # stop: lamp 0, Peltier 0 A

    Everything is switched off (trip, kept until the next set()) when the board is above t_max or when there
    has been no valid temperature for pv_timeout_s. Cooling is held back while the board is within
    dew_margin_c of the dew point."""

    def __init__(self, jig: Jig, *, v_max: float = V_LIMIT, i_max: float = I_MAX, lamp_max: int = 16,
                 t_max: float = 40.0, period_s: float = 5.0, dew_margin_c: float = 4.0,
                 pv_timeout_s: float = 60.0):
        if not jig.dot:
            raise RuntimeError("cannot find CO2Dot: its board temperature is what gets regulated")
        if not jig.lamp and not jig.peltier:
            raise RuntimeError("cannot find IR lamp or Peltier supply: nothing to control the temperature with")
        self.jig = jig
        self.dot, self.lamp, self.peltier, self.li = jig.dot, jig.lamp, jig.peltier, jig.li
        self.w_max = int(self.lamp.max_raw) if self.lamp else 25      # u = 1 means the firmware's full range ...
        self.lamp_max = max(0, min(int(lamp_max), self.w_max))        # ... but the lamp never goes above this
        self.i_max = min(float(i_max), I_MAX)
        if self.peltier:
            self.peltier.i_max = self.i_max
            self.peltier.v_limit = min(float(v_max), V_LIMIT)
            self.peltier.set_voltage(self.peltier.v_limit)
        self.t_max, self.period_s = float(t_max), float(period_s)
        self.dew_margin_c, self.pv_timeout_s = float(dew_margin_c), float(pv_timeout_s)
        # gains tuned on the jig (film_gains.json, 30 Oct 2026). The lamp side is much slower than the Peltier
        # side (the holder heats: ~20 min against 3-5 min), hence its own, smaller gains.
        self.kp, self.ki, self.kd = 0.15, 0.0006, 0.0
        self.kp_heat, self.ki_heat = 0.03, 0.0002
        self.deadband = 0.01
        self.far_c = 2.0                    # further than this from the target, the integral is frozen
        self.lamp_up_s = 60.0               # while climbing, the lamp goes up one step per lamp_up_s
        self.sp: Optional[float] = None
        self.pv: Optional[float] = None
        self.rh: Optional[float] = None
        self.td: Optional[float] = None
        self.u = self.integral = 0.0
        self.lamp_raw, self.amps = 0, 0.0
        self.trip: Optional[str] = None
        self.flags: Dict[str, bool] = {}
        self.errors = 0
        self.last_error: Optional[str] = None
        self.history: deque = deque(maxlen=20000)       # (unix time, T, target, u, lamp, amps)
        self._hist: deque = deque()                     # (t_mono, T) of the last 30 min
        self._last_t: Optional[float] = None
        self._pv_seen_t: Optional[float] = None
        self._lamp_up_t = 0.0
        self._dither = 0.0
        self._cycles = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        jig.temp = self
        if not self.lamp:
            print("temperature control: no IR lamp, cooling only")
        if not self.peltier:
            print("temperature control: no Peltier supply, heating only")
        self._apply(0, 0.0, force=True)

    # -- what the notebook calls ------------------------------------------------------------------------------ #
    def pid(self, kp: Optional[float] = None, ki: Optional[float] = None, kd: Optional[float] = None,
            kp_heat: Optional[float] = None, ki_heat: Optional[float] = None) -> Dict[str, float]:
        """Change gains (only the ones given); returns all of them.

        kp       output per C of error; 1.0 of output is the full Peltier current (or the full lamp range)
        ki       output per C of error per second: removes the offset that kp alone leaves
        kd       output per C/s of temperature change: damps fast swings (0 = off)
        kp_heat, ki_heat   kp and ki while the lamp is the active side"""
        with self._lock:
            for name, v in (("kp", kp), ("ki", ki), ("kd", kd), ("kp_heat", kp_heat), ("ki_heat", ki_heat)):
                if v is not None:
                    setattr(self, name, float(v))
            return {n: getattr(self, n) for n in ("kp", "ki", "kd", "kp_heat", "ki_heat")}

    def set(self, target_c: float) -> None:
        """Regulate the board to target_c (C) from now on; starts the loop if it is not running."""
        t = float(target_c)
        if not 5.0 <= t < self.t_max:
            raise ValueError(f"target {t} C outside 5 .. {self.t_max} C")
        with self._lock:
            # an integral left over from the other side would hold the wrong actuator on for minutes
            if self.pv is not None and self.integral * (t - self.pv) < 0:
                self.integral = 0.0
            self.sp, self.trip = t, None
            self._pv_seen_t = None
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="temperature-control", daemon=True)
            self._thread.start()

    def off(self) -> None:
        """Stop the loop; lamp 0, Peltier 0 A."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.period_s + 10.0)
        with self._lock:
            self.sp = None
            self.u = self.integral = 0.0
            self._apply(0, 0.0, force=True)

    def read(self) -> Optional[float]:
        """Board temperature in C: the loop's latest reading, or a fresh one when the loop is not running."""
        if self._thread is not None and self._thread.is_alive():
            return self.pv
        return _num(self.dot.env().get("T"))

    def status(self) -> Dict[str, Any]:
        r = lambda v, n=3: None if v is None else round(v, n)
        in_band = self.sp is not None and self.pv is not None and abs(self.pv - self.sp) < 0.3
        return {"mode": "off" if self.sp is None else "auto", "sp": self.sp, "pv": r(self.pv), "rh": r(self.rh, 1),
                "td": r(self.td, 2), "u": r(self.u), "lamp": self.lamp_raw, "amps": r(self.amps),
                "i": r(self.integral, 4), "in_band": in_band, "settled": in_band and self.settled(0.3, 300.0),
                "trip": self.trip, "flags": [k for k, v in self.flags.items() if v] or None,
                "errors": self.errors or None, "err": self.last_error}

    def settled(self, tol_c: float = 0.3, window_s: float = 300.0) -> bool:
        """True once the board has stayed within tol_c of the target for the whole of the last window_s."""
        with self._lock:
            if self.sp is None or len(self._hist) < 3:
                return False
            now = self._hist[-1][0]
            pts = [(t, pv) for t, pv in self._hist if now - t <= window_s]
            if len(pts) < 3 or now - pts[0][0] < 0.9 * window_s:
                return False
            return all(abs(pv - self.sp) < tol_c for _, pv in pts)

    # -- the loop --------------------------------------------------------------------------------------------- #
    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                with self._lock:
                    self._step()
            except Exception as e:                  # a bug here must leave the actuators off, not stuck on
                self.last_error = f"controller: {_short(e)}"
                self.errors += 1
                self._apply(0, 0.0, force=True)
            self._stop.wait(max(0.2, self.period_s - (time.monotonic() - t0)))

    def _read_sensor(self) -> Tuple[Optional[float], Optional[float]]:
        try:
            env = self.dot.env()
        except Exception as e:
            self.last_error = f"sensor: {_short(e)}"
            return None, None
        return _num(env.get("T")), _num(env.get("RH"))

    def _dew_point(self) -> Optional[float]:
        """Dew point of the air at the board: from the LI-6800 sample air if connected, else from the board's
        own temperature and humidity."""
        if self.li:
            try:
                r = self.li.read()
                td = _num(r.get("Td_s"))
                if td is None and _num(r.get("H2O_s")) is not None and _num(r.get("Press")) is not None:
                    td = dew_point_from_h2o(r["H2O_s"], r["Press"])
                if td is not None:
                    return td
            except Exception:
                pass
        if self.pv is not None and self.rh is not None:
            return dew_point_c(self.pv, self.rh)
        return None

    def _slope(self, window_s: float = 60.0) -> Optional[float]:
        """Least-squares slope of the board temperature over the last window_s, in C/s."""
        if len(self._hist) < 4:
            return None
        now = self._hist[-1][0]
        pts = [(t, pv) for t, pv in self._hist if now - t <= window_s]
        if len(pts) < 4 or pts[-1][0] - pts[0][0] < 0.5 * window_s:
            return None
        mt = statistics.mean(t for t, _ in pts)
        mp = statistics.mean(pv for _, pv in pts)
        sxx = sum((t - mt) ** 2 for t, _ in pts)
        return sum((t - mt) * (pv - mp) for t, pv in pts) / sxx if sxx else 0.0

    def _step(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        dt = 0.0 if self._last_t is None else max(0.0, min(now - self._last_t, 120.0))
        self._last_t = now
        self._cycles += 1
        self.pv, rh = self._read_sensor()
        if rh is not None:
            self.rh = rh
        self.flags = {}
        if self._pv_seen_t is None:
            self._pv_seen_t = now               # the timeout counts from the first cycle after set()
        if self.pv is not None:
            self._pv_seen_t = now
            self._hist.append((now, self.pv))
            while self._hist and now - self._hist[0][0] > 1800.0:
                self._hist.popleft()
        self.td = self._dew_point()
        try:
            self._control(now, dt)
        finally:
            self.history.append((time.time(), self.pv, self.sp, round(self.u, 4), self.lamp_raw, round(self.amps, 3)))

    def _control(self, now: float, dt: float) -> None:
        if self.sp is None:
            self._apply(0, 0.0)
            return
        # trips: everything off until the next set()
        if self.pv is not None and self.pv > self.t_max:
            self.trip = f"board {self.pv:.1f} C above t_max {self.t_max:.0f} C"
        elif self.pv is None and now - self._pv_seen_t > self.pv_timeout_s:
            self.trip = f"no board temperature for {self.pv_timeout_s:.0f} s"
        if self.trip:
            self.integral = self.u = 0.0
            self._apply(0, 0.0)
            return
        if self.pv is None:                     # short gap: hold the last output
            self._apply(self.lamp_raw, self.amps)
            return

        e = self.sp - self.pv
        heat_side = (self.kp * e + self.integral) > 0
        kp = self.kp_heat if heat_side else self.kp
        ki = self.ki_heat if heat_side else self.ki
        slope = self._slope() if self.kd else None
        d_term = -self.kd * slope if slope is not None else 0.0
        lo, hi = (-1.0 if self.peltier else 0.0), (1.0 if self.lamp else 0.0)
        u_unsat = kp * e + self.integral + d_term
        # anti-windup: no integration into a limit, and none while still far from the target (the approach is
        # the proportional term's job; integrating through it only builds overshoot) - unless the approach
        # has stalled short of the target, where only the integral can close the gap
        saturated = (u_unsat >= hi and e > 0) or (u_unsat <= lo and e < 0)
        long_slope = self._slope(150.0)
        stalled = long_slope is not None and abs(long_slope) < 0.1 / 60.0
        if not saturated and (abs(e) <= self.far_c or stalled):
            self.integral = max(lo * 1.5, min(hi * 1.5, self.integral + ki * e * dt))
        u = max(lo, min(hi, kp * e + self.integral + d_term))

        # board near the dew point: never cool further
        if self.td is not None and self.pv - self.td < self.dew_margin_c and u < 0:
            self.flags["dew"] = True
            u = 0.0
            self.integral = max(self.integral, 0.0)
        self.u = u

        # split range. One lamp step moves the board by about 1 C, more than a useful tolerance, so the
        # fractional part of the lamp command is dithered between two adjacent steps; the board's lag of
        # several minutes averages it out.
        if u > self.deadband:
            exact = min(u * self.w_max, float(self.lamp_max))
            base = int(math.floor(exact))
            self._dither += exact - base
            extra = 0
            if self._dither >= 1.0:
                self._dither -= 1.0
                extra = 1
            target = min(base + extra, self.lamp_max)
        else:
            target, self._dither = 0, 0.0
        # the board lags the lamp by minutes: climb slowly, come down at one step per cycle
        step = max(-1, min(1, target - self.lamp_raw))
        if target > self.lamp_raw + 1:
            if now - self._lamp_up_t < self.lamp_up_s:
                step = 0
            else:
                self._lamp_up_t = now
        if u < -self.deadband:
            # cooling: the lamp goes straight to 0 (its firmware does the ramping) and the Peltier only comes
            # on in the cycle after that, so the two are never driven together
            amps = (-u) * self.i_max if self.lamp_raw == 0 else 0.0
            self._apply(0, amps)
        else:
            self._apply(self.lamp_raw + step, 0.0)

    def _apply(self, lamp_raw: int, amps: float, force: bool = False) -> None:
        lamp_raw = max(0, min(int(lamp_raw), self.lamp_max))
        amps = max(0.0, min(float(amps), self.i_max))
        if self.lamp and (force or lamp_raw != self.lamp_raw or self._cycles % 20 == 0):
            try:
                self.lamp.set_raw(lamp_raw)
            except Exception as e:
                self.errors += 1
                self.last_error = f"lamp: {_short(e)}"
        if self.peltier and (force or abs(amps - self.amps) > 0.005 or self._cycles % 20 == 0):
            try:
                self.peltier.set_current(amps)
            except Exception as e:
                self.errors += 1
                self.last_error = f"peltier: {_short(e)}"
        self.lamp_raw, self.amps = lamp_raw, amps


# ----------------------------------------------------------------------------------------------------------- #
# waiting for a reading to settle
# ----------------------------------------------------------------------------------------------------------- #
def wait_stable(read: Callable[[], Any], target: float, *, tol: Optional[float] = None,
                tol_pct: Optional[float] = None, hold_s: float = 60.0, timeout_s: float = 1800.0,
                poll_s: float = 2.0, verbose: bool = True) -> bool:
    """Block until read() has stayed within the tolerance of target for hold_s seconds.

    read      a function without arguments that returns the current value, e.g. temp.read or
              lambda: li.get("CO2_s")
    tol       tolerance in the units of the value, or
    tol_pct   tolerance in percent of the target (1 % of 27 C = 0.27 C)
    Returns True when stable, False after timeout_s. Ctrl-C only stops the waiting."""
    if (tol is None) == (tol_pct is None):
        raise ValueError("give exactly one of tol (absolute) or tol_pct (percent of the target)")
    band = float(tol) if tol is not None else abs(float(target)) * float(tol_pct) / 100.0
    if band <= 0:
        raise ValueError("the tolerance is zero: use tol= for a target of 0")
    t0 = time.monotonic()
    inside_since: Optional[float] = None
    last_print = -1e9
    say = print if verbose else (lambda *a, **k: None)
    while True:
        now = time.monotonic()
        try:
            v = _finite(read())
        except Exception:
            v = None
        if v is not None and abs(v - target) <= band:
            inside_since = now if inside_since is None else inside_since
            if now - inside_since >= hold_s:
                say(f"stable: {v:.3f} within +-{band:.3g} of {target:g} for {hold_s:.0f} s "
                    f"(after {(now - t0) / 60:.1f} min)")
                return True
        else:
            inside_since = None
        if now - t0 >= timeout_s:
            say(f"NOT stable after {timeout_s / 60:.0f} min: last value {v}, target {target:g} +-{band:.3g}")
            return False
        if now - last_print >= max(15.0, poll_s):
            held = "" if inside_since is None else f", in band for {now - inside_since:.0f} s"
            say(f"  {(now - t0) / 60:5.1f} min  value {'--' if v is None else format(v, '.3f')}  "
                f"target {target:g} +-{band:.3g}{held}")
            last_print = now
        time.sleep(poll_s)


# ----------------------------------------------------------------------------------------------------------- #
# openJII (optional): send records to an experiment
# ----------------------------------------------------------------------------------------------------------- #
# REST with a personal API key registers the CO2Dot as a device, issues its certificate once and onboards it
# to the experiment; the records then go over MQTT (mTLS) to
# experiment/data_ingest/v1/<experiment>/generic/<sensorVersion>/<sensorId>.
# Secrets never sit next to the code: the API key is read from ~/.openjii/api_key (or OPENJII_API_KEY) and the
# certificates live in ~/.openjii/devices/<serial>/.
OPENJII_API = "https://api.openjii.org"
OPENJII_STORE = Path(os.environ.get("OPENJII_STORE", Path.home() / ".openjii"))
OPENJII_ROOT_CA = "https://www.amazontrust.com/repository/AmazonRootCA1.pem"


class OpenJIIClient:
    def __init__(self, api_key: str, base_url: str = OPENJII_API, store: Path = OPENJII_STORE):
        import requests
        self._requests = requests
        self.base, self.store = base_url.rstrip("/"), Path(store)
        self.s = requests.Session()
        self.s.headers.update({"x-api-key": api_key, "accept": "application/json"})

    @classmethod
    def from_store(cls, store: Path = OPENJII_STORE) -> "OpenJIIClient":
        key = os.environ.get("OPENJII_API_KEY") or (Path(store) / "api_key").read_text(encoding="utf-8").strip()
        return cls(key, store=store)

    def _req(self, method: str, path: str, **kw) -> Any:
        r = self.s.request(method, self.base + path, timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def experiment(self, experiment_id: str) -> Dict[str, Any]:
        return self._req("GET", f"/api/v1/experiments/{experiment_id}")

    def provision(self, serial_number: str, experiment_id: str, name: Optional[str] = None,
                  new_certificate: bool = False) -> Dict[str, Any]:
        """Find or register the device, make sure its certificate is on this PC, onboard it to the experiment.

        A device that already has a certificate elsewhere is NOT given a new one unless new_certificate=True:
        a new certificate replaces the old one, which would stop the PC that holds it from sending."""
        d = self.store / "devices" / serial_number.replace(":", "")
        dev = next((x for x in self._req("GET", "/api/v1/devices") if x.get("serialNumber") == serial_number), None)
        if dev is None:
            dev = self._req("POST", "/api/v1/devices", json={
                "serialNumber": serial_number, "name": name or f"CO2Dot {serial_number[-5:]}", "deviceType": "generic"})
            print(f"openJII: registered device {dev['thingName']}")
        cert, key = d / "device.crt", d / "device.key"
        if not (cert.exists() and key.exists()):
            has_one = bool(dev.get("certificateId"))
            if has_one and not new_certificate:
                raise RuntimeError(f"no certificate for {serial_number} on this PC, and the device already has "
                                   "one elsewhere: pass new_certificate=True to replace it, or copy the folder "
                                   f"{d} from the PC that has it")
            creds = self._req("POST", f"/api/v1/devices/{dev['id']}/credentials" + ("/rotate" if has_one else ""))
            d.mkdir(parents=True, exist_ok=True)
            cert.write_text(creds["certificatePem"], encoding="utf-8")
            key.write_text(creds["privateKey"], encoding="utf-8")
            (d / "certificate_id.txt").write_text(creds["certificateId"], encoding="utf-8")
            print("openJII: certificate issued and stored on this PC (the platform shows it only once)")
        ca = self.store / "AmazonRootCA1.pem"
        if not ca.exists():
            ca.write_bytes(self._requests.get(OPENJII_ROOT_CA, timeout=30).content)
        cfg = self._req("POST", f"/api/v1/devices/{dev['id']}/onboard",
                        json={"experimentIds": [experiment_id], "includeWorkbook": False})
        cfg.update({"serial": serial_number, "deviceId": dev["id"], "cert": str(cert), "key": str(key), "ca": str(ca)})
        (d / "onboarding.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return cfg


def _local_timezone() -> str:
    try:
        import tzlocal
        return tzlocal.get_localzone_name()
    except Exception:
        return os.environ.get("OPENJII_TIMEZONE", "Europe/Amsterdam")


class OpenJIIStream:
    """Sends records to one experiment. send() only queues: a worker thread publishes at QoS 1, and if the
    broker is unreachable the oldest queued records are dropped rather than blocking the notebook."""

    def __init__(self, cfg: Dict[str, Any], experiment_id: str, *, device_name: str = "CO2Dot",
                 firmware: str = "1.0", sensor_version: str = "1.0", extra: Optional[Dict[str, Any]] = None,
                 test: bool = False, max_queue: int = 2000):
        mqtt = _paho()
        exp = next((e for e in cfg["experiments"] if e["experimentId"] == experiment_id), None)
        if exp is None:
            raise ValueError(f"experiment {experiment_id} is not in the onboarding reply")
        self.cfg, self.experiment_id = cfg, experiment_id
        self.sensor_id = cfg["serial"].replace(":", "")
        self.topic = f"{exp['topicPrefix']}/{sensor_version}/{self.sensor_id}"
        self.device_name, self.sensor_version, self.firmware = device_name, sensor_version, firmware
        self.extra, self.test = extra or {}, test
        self.tz = _local_timezone()
        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_queue)
        self.sent = self.dropped = self.failed = 0
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        if hasattr(mqtt, "CallbackAPIVersion"):                 # paho 2.x
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg["thingName"],
                                      clean_session=True, protocol=mqtt.MQTTv311)
        else:                                                   # paho 1.x
            self.client = mqtt.Client(client_id=cfg["thingName"], clean_session=True, protocol=mqtt.MQTTv311)
        self.client.tls_set(ca_certs=cfg["ca"], certfile=cfg["cert"], keyfile=cfg["key"],
                            tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.connected = threading.Event()
        self.client.on_connect = lambda c, u, f, rc, *a: (self.connected.set() if rc == 0 else None)
        self.client.on_disconnect = lambda c, u, rc, *a: self.connected.clear()

    def __bool__(self) -> bool:
        return True

    def connect(self, timeout_s: float = 20.0) -> bool:
        self.client.connect_async(self.cfg["endpoint"], 8883, keepalive=60)
        self.client.loop_start()
        ok = self.connected.wait(timeout_s)
        self._stop.clear()
        self._worker = threading.Thread(target=self._drain, name="openjii-stream", daemon=True)
        self._worker.start()
        return ok

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                info = self.client.publish(self.topic, json.dumps(msg), qos=1)
                info.wait_for_publish(timeout=20)
                if info.is_published():
                    self.sent += 1
                else:
                    self.failed += 1
            except Exception:
                self.failed += 1

    def message(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """The message for one record. A 'ts' field of the record (ISO time) becomes the message timestamp."""
        sample = {k: v for k, v in record.items() if k != "ts"}
        try:
            ts = datetime.fromisoformat(record["ts"])
        except Exception:
            ts = datetime.now()
        ts = ts.astimezone()
        if self.test:
            sample = {"test": True, **sample}
        return {"device_name": self.device_name, "device_version": self.sensor_version,
                "device_id": self.sensor_id, "device_firmware": self.firmware,
                "sample": json.dumps(sample, default=str), "timestamp": ts.isoformat(timespec="milliseconds"),
                "timezone": self.tz, **self.extra}

    def send(self, record: Dict[str, Any]) -> None:
        """Queue one record (any dict, e.g. jig.snapshot()) for the experiment."""
        msg = self.message(record)
        try:
            self.q.put_nowait(msg)
        except queue.Full:
            self.dropped += 1
            try:
                self.q.get_nowait()
                self.q.put_nowait(msg)
            except Exception:
                pass

    def close(self, flush_s: float = 10.0) -> None:
        t0 = time.monotonic()
        while not self.q.empty() and time.monotonic() - t0 < flush_s:
            time.sleep(0.2)
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=25)
        self.client.loop_stop()
        self.client.disconnect()
        print(f"openJII stream closed: sent {self.sent}, failed {self.failed}, dropped {self.dropped}")


class _NoStream:
    """What openjii() returns when streaming is off: send() and close() do nothing."""

    def __init__(self, why: str):
        self.why = why

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"<openJII stream off: {self.why}>"

    def send(self, record: Dict[str, Any]) -> None:
        pass

    def close(self) -> None:
        pass


def openjii(experiment_id: Optional[str], jig: Jig, *, device_name: Optional[str] = None,
            new_certificate: bool = False, test: bool = False):
    """Point the jig at an openJII experiment; returns the stream to send() records to.

    experiment_id None (or "") switches streaming off. Any failure prints "cannot connect to openJII (...)"
    and also returns a stream that does nothing, so the rest of a notebook runs unchanged.
    test=True marks every record with {"test": true}."""
    if not experiment_id:
        return _NoStream("no experiment id given")
    try:
        if not jig.dot:
            raise RuntimeError("cannot find CO2Dot: the records are sent in its name")
        serial_number = jig.dot.id
        if serial_number.startswith("port:"):
            raise RuntimeError("this CO2Dot reports no USB serial number to register it with")
        client = OpenJIIClient.from_store()
        name = client.experiment(experiment_id)["name"]
        cfg = client.provision(serial_number, experiment_id, name=device_name, new_certificate=new_certificate)
        stream = OpenJIIStream(cfg, experiment_id, device_name=device_name or f"CO2Dot {serial_number[-5:]}",
                               firmware=str(jig.dot.firmware.get("version", "?")),
                               extra={"output": "calibration jig demo"}, test=test)
        ok = stream.connect()
    except Exception as e:
        print(f"cannot connect to openJII ({_short(e, 400)})")
        return _NoStream(_short(e, 400))
    jig.stream = stream
    print(f"openJII         experiment '{name}', {'connected' if ok else 'NOT connected yet (will keep trying)'}, "
          f"topic {stream.topic}")
    return stream

"""
li_mqtt — native MQTT control of a LI-6800 over its internal message bus.

The LI-6800 is built on an MQTT bus: a Mosquitto broker runs on the sensor head
(port 1883) and every component — the touchscreen UI, the Python/BP engine, the
head, the fluorometer — talks only through it. Live data, control setpoints, and
background programs are all just MQTT topics. This module connects to that broker
directly from your PC over the same link-local IPv6 link, replacing the older
SSH + file-polling bridge (li_connect.py / RemoteEnvMeasure.py):

    li = LI6800.connect()                 # find the broker, subscribe to live data
    vals = li.read()                      # current measured + computed values (dict)
    print(li.get("CO2_r"), li.get("A"))
    li.set(co2_r=400, tair=25, rh_air=50) # change setpoints (drives the hardware)
    li.wait_stable("CO2_r", 400, tol=5)
    li.close()

read() is passive and instantaneous. set() changes the chamber environment
immediately — exactly like turning a knob on the console — so use it deliberately.

Discovered/verified on Bluestem (firmware ~1.4): node roles are li6860=console,
li6850=head (broker host), li6840=fluorometer. Topic strings can drift between
firmware versions; snoop the live bus with mqtt_sniff.py to re-verify if needed.
"""
from __future__ import annotations

import json
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt

from li_connect import find_li6800_candidates

BROKER_PORT = 1883

# --- topic layout (the head publishes/serves everything under licor/li6850) --- #
BASE = "licor/li6850"
SCRIPTS = f"{BASE}/scripts"

TOPIC_DATA = f"{BASE}/output/DATA"            # raw measured values (CO2_r, H2O_s, Tleaf, ...)
TOPIC_DATACOMP = f"{BASE}/computed/DATACOMP"  # computed gas exchange (A, gsw, Ci, E, ...)
TOPIC_DATASTAT = f"{BASE}/output/DATASTAT"    # status/diagnostic temps
TOPIC_NEWAP = f"{SCRIPTS}/newap/test"         # start/stop background programs
TOPIC_BKGD_ACTION = f"{SCRIPTS}/background/action"  # built-in background tasks

# Data streams kept live for read()/get(). DATACOMP overrides DATA on key clashes.
DATA_TOPICS = [TOPIC_DATA, TOPIC_DATACOMP, TOPIC_DATASTAT]

# --- control setpoint map (verified against comcon.py CCDict) --------------- #
# friendly kwarg -> (publish topic, base payload, key that carries the value)
# publishing {**payload, value_key: value} sets the setpoint; value=None turns
# the controller off (Active=False), mirroring the console's Val_Off() helper.
def _u(name: str) -> str:
    return f"{SCRIPTS}/{name}/constants/update"


CONTROLS: Dict[str, Tuple[str, Dict[str, Any], str]] = {
    "co2_r":    (_u("co2_control"),  {"Active": True, "Auto": True, "Scrub": "auto", "Target": "CO2_r"}, "SetPoint"),
    "co2_s":    (_u("co2_control"),  {"Active": True, "Auto": True, "Scrub": "auto", "Target": "CO2_s"}, "SetPoint"),
    "co2_pct":  (_u("co2_control"),  {"Active": True, "Auto": False, "Scrub": "auto"}, "Percent"),
    "flow":     (_u("flow_control"), {"Active": True, "Auto": True}, "SetPoint"),
    "flow_pct": (_u("flow_control"), {"Active": True, "Auto": False}, "Percent"),
    "txchg":    (_u("temp_control"), {"Active": True, "Target": "Txchg", "Auto": True}, "SetPoint"),
    "tair":     (_u("temp_control"), {"Active": True, "Target": "Tair", "Auto": True}, "SetPoint"),
    "tleaf":    (_u("temp_control"), {"Active": True, "Target": "Tleaf", "Auto": True}, "SetPoint"),
    "h2o_r":    (_u("h2o_control"),  {"Active": True, "Target": "H2O_r", "Type": "Fully-Auto"}, "SetPoint"),
    "h2o_s":    (_u("h2o_control"),  {"Active": True, "Target": "H2O_s", "Type": "Fully-Auto"}, "SetPoint"),
    "vpd_leaf": (_u("h2o_control"),  {"Active": True, "Target": "VPD_leaf", "Type": "Fully-Auto"}, "SetPoint"),
    "sd_air":   (_u("h2o_control"),  {"Active": True, "Target": "SD_air", "Type": "Fully-Auto"}, "SetPoint"),
    "rh_air":   (_u("h2o_control"),  {"Active": True, "Target": "RH_air", "Type": "Fully-Auto"}, "SetPoint"),
    "fan_rpm":  (_u("fan_control"),  {"Active": True, "Target": "RPM", "Auto": True}, "SetPoint"),
}


def _make_client(client_id: str) -> mqtt.Client:
    """Create a paho client that works on both paho-mqtt 1.x and 2.x."""
    try:  # paho-mqtt 2.x requires an explicit callback API version
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                           client_id=client_id, protocol=mqtt.MQTTv31)
    except (AttributeError, TypeError):  # paho-mqtt 1.x
        return mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv31)


def _tcp_open(addr: str, ifindex: int, port: int, timeout: float = 2.0) -> bool:
    """True if a TCP connection to a link-local IPv6 addr:port succeeds."""
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((addr, port, 0, ifindex))  # (host, port, flowinfo, scope_id)
        return True
    except OSError:
        return False
    finally:
        s.close()


def discover_brokers(interface_hint: str = "Ethernet") -> List[str]:
    """Scoped host strings ('fe80::..%<ifindex>') of link-local neighbors with
    an open MQTT port. More than one node may answer on 1883 (head, fluorometer);
    only the head actually streams data, so connect() validates each in turn.
    """
    idx, name, addrs = find_li6800_candidates(interface_hint, verify=False)
    hosts = [f"{a}%{idx}" for a in addrs if _tcp_open(a, idx, BROKER_PORT)]
    if not hosts:
        raise RuntimeError(
            f"No MQTT broker (port {BROKER_PORT}) found among link-local neighbors "
            f"on '{interface_hint}': {addrs}. Is the instrument powered on and cabled?"
        )
    return hosts


class LI6800:
    """Native MQTT handle to a LI-6800.

        li = LI6800.connect()        # discovers broker + subscribes to live data
        li.read()                    # -> {'CO2_r': .., 'A': .., 'gsw': .., ...}
        li.set(co2_r=400, tair=25)   # change setpoints
    """

    def __init__(self, broker: str, port: int = BROKER_PORT, *, client_id: str = "li-pc-client"):
        self.broker = broker
        self.port = port
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._client = _make_client(client_id)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # -- lifecycle --------------------------------------------------------- #
    @classmethod
    def connect(cls, interface_hint: str = "Ethernet", *, broker: Optional[str] = None,
                settle_s: float = 2.5, **kw) -> "LI6800":
        """Discover the broker (unless given), connect, and prime the live cache.

        Several nodes may listen on 1883; we keep the one that actually streams
        data within `settle_s`. Pass broker='fe80::..%<idx>' to skip discovery.
        """
        candidates = [broker] if broker else discover_brokers(interface_hint)
        last = "no candidates"
        for host in candidates:
            self = cls(host, **kw)
            try:
                self._client.connect(self.broker, self.port, keepalive=30)
            except OSError as e:
                last = f"{host}: {e}"
                continue
            self._client.loop_start()
            t0 = time.time()
            while time.time() - t0 < settle_s:
                if self._latest:           # real broker: data is flowing
                    return self
                time.sleep(0.1)
            self.close()
            last = f"{host}: connected but no data within {settle_s:.0f}s"
        raise RuntimeError(f"No LI-6800 data broker found. Last: {last}")

    def _on_connect(self, client, userdata, flags, rc, *a):
        for t in DATA_TOPICS:
            client.subscribe(t)

    def _on_message(self, client, userdata, msg):
        try:
            self._latest[msg.topic] = json.loads(msg.payload.decode("utf-8", "replace"))
        except Exception:
            pass  # ignore non-JSON / partial payloads

    def close(self) -> None:
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            pass

    def __enter__(self) -> "LI6800":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- reading ----------------------------------------------------------- #
    def read(self) -> Dict[str, Any]:
        """Current live values: raw measured (DATA) merged with computed (DATACOMP)."""
        merged: Dict[str, Any] = {}
        merged.update(self._latest.get(TOPIC_DATA, {}))
        merged.update(self._latest.get(TOPIC_DATACOMP, {}))
        return merged

    def get(self, key: str, default: Any = None) -> Any:
        """One live value by key (e.g. 'CO2_r', 'Tleaf', 'A', 'gsw', 'Ci')."""
        return self.read().get(key, default)

    def raw(self, topic: str) -> Dict[str, Any]:
        """Latest payload for any subscribed topic (debugging / advanced use)."""
        return self._latest.get(topic, {})

    # -- control ----------------------------------------------------------- #
    def set(self, **setpoints: Any) -> None:
        """Set one or more controller setpoints. Drives the chamber immediately.

        Keys: co2_r, co2_s, co2_pct, flow, flow_pct, txchg, tair, tleaf,
        h2o_r, h2o_s, vpd_leaf, sd_air, rh_air, fan_rpm. A value of None turns
        that controller off. Example: li.set(co2_r=400, tair=25, rh_air=50).
        """
        for key, value in setpoints.items():
            if key not in CONTROLS:
                raise KeyError(f"unknown control {key!r}; valid: {sorted(CONTROLS)}")
            topic, template, value_key = CONTROLS[key]
            payload = dict(template)
            if value is None:
                payload["Active"] = False
            else:
                payload[value_key] = value
            self._client.publish(topic, json.dumps(payload))

    def set_raw(self, topic: str, payload: Dict[str, Any]) -> None:
        """Publish an arbitrary JSON payload to a topic (for controls not mapped above)."""
        self._client.publish(topic, json.dumps(payload))

    def wait_stable(self, key: str, target: float, *, tol: float = 5.0,
                    timeout_s: float = 120.0, poll_s: float = 0.5) -> bool:
        """Block until live `key` is within `tol` of `target`. Returns True if reached."""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            v = self.get(key)
            if isinstance(v, (int, float)) and abs(v - target) < tol:
                return True
            time.sleep(poll_s)
        return False

    # -- background programs (experimental) -------------------------------- #
    def run_bp(self, path: str) -> None:
        """Start a saved background program by path (publishes to newap/test)."""
        self._client.publish(TOPIC_NEWAP, json.dumps({"run": path}))

    def run_steps(self, steps: list, name: int = 0, file: str = "pc") -> None:
        """Start a background program from inline bpdefs `steps`."""
        self._client.publish(TOPIC_NEWAP,
                             json.dumps({"action": "run", "name": name, "file": file, "steps": steps}))

    def stop_bp(self, name: int = 0) -> None:
        self._client.publish(TOPIC_NEWAP, json.dumps({"action": "stop", "name": name}))


if __name__ == "__main__":
    print(f"discovering broker ...")
    li = LI6800.connect()
    print(f"connected to broker {li.broker}:{li.port}")
    time.sleep(1.0)
    vals = li.read()
    print(f"{len(vals)} live values:")
    for k in sorted(vals):
        print(f"  {k:<12} {vals[k]}")
    li.close()

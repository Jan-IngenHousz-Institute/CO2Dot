"""
openjii_stream.py -- stream CO2Dot calibration cycles live into an openJII experiment over MQTT.

Two parts:
  OpenJIIClient     REST calls with a personal API key (x-api-key): find or register the CO2Dot as a device
                    (family `generic`), issue its X.509 credentials once, onboard it to the experiment, and
                    keep the one-time bundle + onboarding config in a private store outside the repo.
  OpenJIIPublisher  paho-mqtt client (mTLS, port 8883, client id = thingName) that turns every logger cycle
                    into one message on experiment/data_ingest/v1/{experimentId}/generic/{sensorVersion}/{sensorId}
                    and publishes it at QoS 1 from the logger thread without blocking it.

Payload (asyncapi ExperimentDataMessage, flat top-level fields):
    device_name, device_version, device_id, device_firmware, sample (JSON string), timestamp (ISO with offset),
    timezone (IANA). No protocol_id: the experiment has no workbook, the rows land unattributed in raw_data.

Secrets: the API key is read from ~/.openjii/api_key (or OPENJII_API_KEY); certificates live in
~/.openjii/devices/<serial>/. Nothing secret is ever written next to the code or the data.

    client = OpenJIIClient.from_store()
    cfg = client.provision("3C:DC:75:0D:FA:64", EXPERIMENT_ID, name="CO2Dot A")   # idempotent
    pub = OpenJIIPublisher(cfg, sensor_version="1.0"); pub.connect()
    run.on_cycle = pub.publish_cycle        # or Session(..., publisher=pub)
    ...
    pub.close()
"""
from __future__ import annotations

import json
import os
import queue
import ssl
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests

API_BASE = "https://api.openjii.org"
STORE = Path(os.environ.get("OPENJII_STORE", Path.home() / ".openjii"))
ROOT_CA_URL = "https://www.amazontrust.com/repository/AmazonRootCA1.pem"
FAMILY = "generic"


def local_timezone_name() -> str:
    try:
        import tzlocal
        return tzlocal.get_localzone_name()
    except Exception:
        return os.environ.get("OPENJII_TIMEZONE", "Europe/Amsterdam")


# ----------------------------------------------------------------------------- #
# REST: provisioning
# ----------------------------------------------------------------------------- #
class OpenJIIClient:
    def __init__(self, api_key: str, base_url: str = API_BASE, store: Path = STORE):
        self.base, self.store = base_url.rstrip("/"), Path(store)
        self.s = requests.Session(); self.s.headers.update({"x-api-key": api_key, "accept": "application/json"})

    @classmethod
    def from_store(cls, store: Path = STORE) -> "OpenJIIClient":
        key = os.environ.get("OPENJII_API_KEY") or (Path(store) / "api_key").read_text(encoding="utf-8").strip()
        return cls(key, store=store)

    def _req(self, method: str, path: str, **kw) -> Any:
        r = self.s.request(method, self.base + path, timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def experiment(self, experiment_id: str) -> Dict[str, Any]:
        return self._req("GET", f"/api/v1/experiments/{experiment_id}")

    def devices(self):
        return self._req("GET", "/api/v1/devices")

    def find_device(self, serial: str) -> Optional[Dict[str, Any]]:
        for d in self.devices():
            if d.get("serialNumber") == serial:
                return d
        return None

    def register_device(self, serial: str, name: str, device_type: str = FAMILY) -> Dict[str, Any]:
        return self._req("POST", "/api/v1/devices", json={"serialNumber": serial, "name": name, "deviceType": device_type})

    def issue_credentials(self, device_id: str, rotate: bool = False) -> Dict[str, Any]:
        path = f"/api/v1/devices/{device_id}/credentials" + ("/rotate" if rotate else "")
        return self._req("POST", path)

    def onboard(self, device_id: str, experiment_ids, include_workbook: bool = False) -> Dict[str, Any]:
        return self._req("POST", f"/api/v1/devices/{device_id}/onboard",
                         json={"experimentIds": list(experiment_ids), "includeWorkbook": include_workbook})

    # -- the one call a notebook needs --------------------------------------- #
    def provision(self, serial: str, experiment_id: str, name: Optional[str] = None,
                  device_type: str = FAMILY) -> Dict[str, Any]:
        """Find or register the device, make sure a credential bundle is stored locally (issue or rotate if not),
        onboard it to the experiment and store the onboarding config. Returns the config dict with local paths."""
        d = self.store / "devices" / serial.replace(":", "")
        d.mkdir(parents=True, exist_ok=True)
        dev = self.find_device(serial)
        if dev is None:
            dev = self.register_device(serial, name or f"CO2Dot {serial[-5:]}", device_type)
            print(f"registered device {dev['thingName']} ({dev['id']})")
        else:
            print(f"device already registered: {dev['thingName']} ({dev['id']}, status {dev.get('status')})")
        cert, key = d / "device.crt", d / "device.key"
        if not (cert.exists() and key.exists()):
            creds = self.issue_credentials(dev["id"], rotate=bool(dev.get("certificateId")))
            cert.write_text(creds["certificatePem"], encoding="utf-8"); key.write_text(creds["privateKey"], encoding="utf-8")
            (d / "certificate_id.txt").write_text(creds["certificateId"], encoding="utf-8")
            print("credentials issued and stored (shown once by the platform; kept only here)")
        ca = self.store / "AmazonRootCA1.pem"
        if not ca.exists():
            ca.write_bytes(requests.get(ROOT_CA_URL, timeout=30).content)
        cfg = self.onboard(dev["id"], [experiment_id], include_workbook=False)
        cfg.update({"serial": serial, "deviceId": dev["id"], "cert": str(cert), "key": str(key), "ca": str(ca)})
        (d / "onboarding.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        topics = [e["topicPrefix"] for e in cfg["experiments"]]
        print(f"onboarded to {len(cfg['experiments'])} experiment(s); endpoint {cfg['endpoint']}; topic prefix {topics}")
        return cfg

    @staticmethod
    def load_config(serial: str, store: Path = STORE) -> Dict[str, Any]:
        return json.loads((Path(store) / "devices" / serial.replace(":", "") / "onboarding.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- #
# MQTT: publishing
# ----------------------------------------------------------------------------- #
class OpenJIIPublisher:
    """Publishes logger cycles as ExperimentDataMessages. Non-blocking: publish_cycle() enqueues, a worker
    thread publishes; if the broker is unreachable the queue is bounded and the oldest messages are dropped
    (the local cycles.jsonl remains the complete record)."""

    def __init__(self, cfg: Dict[str, Any], experiment_id: Optional[str] = None, sensor_version: str = "1.0",
                 sensor_id: Optional[str] = None, device_name: str = "CO2Dot", firmware: str = "1.0",
                 max_queue: int = 2000, extra: Optional[Dict[str, Any]] = None, test: bool = False):
        import paho.mqtt.client as mqtt
        self.cfg = cfg
        exps = cfg["experiments"]
        exp = next((e for e in exps if experiment_id is None or e["experimentId"] == experiment_id), None)
        if exp is None:
            raise ValueError(f"experiment {experiment_id} not in the onboarding config; onboard first")
        self.experiment_id = exp["experimentId"]
        self.sensor_id = (sensor_id or cfg["serial"]).replace(":", "")
        self.topic = f"{exp['topicPrefix']}/{sensor_version}/{self.sensor_id}"
        self.device_name, self.sensor_version, self.firmware = device_name, sensor_version, firmware
        self.extra, self.test = extra or {}, test
        self.tz = local_timezone_name()
        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_queue)
        self.sent = self.dropped = self.failed = 0
        self._stop = threading.Event(); self._worker: Optional[threading.Thread] = None
        if hasattr(mqtt, "CallbackAPIVersion"):          # paho 2.x
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg["thingName"], clean_session=True, protocol=mqtt.MQTTv311)
        else:                                             # paho 1.x
            self.client = mqtt.Client(client_id=cfg["thingName"], clean_session=True, protocol=mqtt.MQTTv311)
        self.client.tls_set(ca_certs=cfg["ca"], certfile=cfg["cert"], keyfile=cfg["key"], tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.connected = threading.Event()
        self.client.on_connect = lambda c, u, f, rc, *a: (self.connected.set() if rc == 0 else None)
        self.client.on_disconnect = lambda c, u, rc, *a: self.connected.clear()

    # -- lifecycle ------------------------------------------------------------ #
    def connect(self, timeout_s: float = 20.0) -> bool:
        self.client.connect_async(self.cfg["endpoint"], 8883, keepalive=60)
        self.client.loop_start()
        ok = self.connected.wait(timeout_s)
        self._stop.clear()
        self._worker = threading.Thread(target=self._drain, name="openjii-publisher", daemon=True); self._worker.start()
        print(f"openJII publisher: {'connected' if ok else 'NOT connected yet'} to {self.cfg['endpoint']} as {self.cfg['thingName']}; topic {self.topic}")
        return ok

    def close(self, flush_s: float = 10.0) -> None:
        t0 = time.monotonic()
        while not self.q.empty() and time.monotonic() - t0 < flush_s:
            time.sleep(0.2)
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=5)
        self.client.loop_stop(); self.client.disconnect()
        print(f"openJII publisher closed: sent {self.sent}, failed {self.failed}, dropped {self.dropped}")

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

    # -- messages --------------------------------------------------------------- #
    def message(self, sample: Dict[str, Any], ts: Optional[datetime] = None) -> Dict[str, Any]:
        ts = ts or datetime.now().astimezone()
        if ts.tzinfo is None:
            ts = ts.astimezone()
        if self.test:
            sample = {"test": True, **sample}
        return {"device_name": self.device_name, "device_version": self.sensor_version, "device_id": self.sensor_id,
                "device_firmware": self.firmware, "sample": json.dumps(sample, default=str),
                "timestamp": ts.isoformat(timespec="milliseconds"), "timezone": self.tz, **self.extra}

    def publish(self, sample: Dict[str, Any], ts: Optional[datetime] = None) -> None:
        msg = self.message(sample, ts)
        try:
            self.q.put_nowait(msg)
        except queue.Full:
            self.dropped += 1
            try:
                self.q.get_nowait(); self.q.put_nowait(msg)
            except Exception:
                pass

    def publish_cycle(self, line: Dict[str, Any]) -> None:
        """Logger hook: one message per cycle, every device's readings in the sample (a burst would be the same
        reading repeated; one object per cycle keeps the timestamp semantics simple)."""
        sample = {"cycle": line.get("cycle"), "phase": line.get("phase"), "licor": line.get("licor"),
                  "devices": {d: {k: v for k, v in rec.items() if k in ("port", "spec_flash", "env", "spec", "error")}
                              for d, rec in line.get("devices", {}).items()}}
        try:
            ts = datetime.fromisoformat(line["ts"])
        except Exception:
            ts = None
        self.publish(sample, ts)


if __name__ == "__main__":
    import sys
    serial = sys.argv[1] if len(sys.argv) > 1 else "3C:DC:75:0D:FA:64"
    exp = sys.argv[2] if len(sys.argv) > 2 else "81cb1484-372c-417e-ab12-57b016f40f02"
    client = OpenJIIClient.from_store()
    print("experiment:", client.experiment(exp)["name"])
    cfg = client.provision(serial, exp, name=f"CO2Dot {serial[-5:]}")
    pub = OpenJIIPublisher(cfg, experiment_id=exp, test=True)
    pub.connect()
    pub.publish({"message": "connection test from openjii_stream.py", "host": os.environ.get("COMPUTERNAME", "")})
    time.sleep(3); pub.close()

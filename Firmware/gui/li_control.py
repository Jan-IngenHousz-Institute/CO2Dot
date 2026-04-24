"""
li_control.py — Qt-free protocol layer for the LI-COR LI-6800 Li-Control feature.

Mirrors the command/ack shape expected by Li-Control/RemoteEnvMeasure.py (the
on-device Basic Program running on the instrument). This module has no Qt or
paramiko dependency and is safe to import from anywhere.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

REMOTE_DIR = "/home/licor/apps/dynamic"
REMOTE_CMD = f"{REMOTE_DIR}/remote_cmd.json"
REMOTE_TMP = f"{REMOTE_DIR}/remote_cmd.json.tmp"
REMOTE_ACK = f"{REMOTE_DIR}/remote_ack.json"


@dataclass
class LiConfig:
    host: str
    username: str = "licor"
    port: int = 22
    key_filename: str | None = None
    poll_interval_s: float = 0.5
    ack_timeout_s: float = 600.0
    keepalive_s: int = 30
    host_key_policy: str = "auto_add"  # "auto_add" | "reject"


@dataclass
class LiSetpoints:
    co2_r: float | None = None
    qin: float | None = None
    flow: float | None = None
    tair: float | None = None
    rh_air: float | None = None
    fan_rpm: float | None = None
    pressure: float | None = None
    wait_for_co2: bool = False
    co2_tol: float = 2.0
    wait_s: float = 0.0
    log: bool = False

    def to_cmd(self, cmd_id: str) -> dict[str, Any]:
        """Build the JSON command dict consumed by RemoteEnvMeasure.py."""
        cmd: dict[str, Any] = {"action": "measure", "cmd_id": cmd_id}
        if self.co2_r is not None:
            cmd["co2_r"] = float(self.co2_r)
        if self.qin is not None:
            cmd["qin"] = float(self.qin)
        if self.flow is not None:
            cmd["flow"] = float(self.flow)
        if self.tair is not None:
            cmd["tair"] = float(self.tair)
        if self.rh_air is not None:
            cmd["rh_air"] = float(self.rh_air)
        if self.fan_rpm is not None:
            cmd["fan_rpm"] = float(self.fan_rpm)
        if self.pressure is not None:
            cmd["pressure"] = float(self.pressure)
        cmd["wait_for_co2"] = bool(self.wait_for_co2)
        cmd["co2_tol"] = float(self.co2_tol)
        cmd["wait_s"] = float(self.wait_s)
        cmd["log"] = bool(self.log)
        return cmd


LI_SETPOINT_FIELDS = frozenset({
    "co2_r", "qin", "flow", "tair", "rh_air", "fan_rpm", "pressure",
    "wait_for_co2", "co2_tol", "wait_s", "log",
})


def build_stop_cmd(cmd_id: str) -> dict[str, Any]:
    return {"action": "stop", "cmd_id": cmd_id}


def new_cmd_id() -> str:
    return uuid.uuid4().hex[:12]


# Raw-ack field aliases → canonical key. Firmware tweaks to RemoteEnvMeasure.py
# should only require edits to this table.
ACK_ALIASES: dict[str, tuple[str, ...]] = {
    "CO2_r":    ("CO2_r", "CO2r", "co2_r"),
    "CO2_s":    ("CO2_s", "CO2s", "co2_s"),
    "H2O_r":    ("H2O_r", "H2Or", "h2o_r"),
    "H2O_s":    ("H2O_s", "H2Os", "h2o_s"),
    "Tchamber": ("Tchamber", "Tcham", "T_chamber", "tchamber"),
    "Tleaf":    ("Tleaf", "tleaf", "T_leaf"),
    "RHcham":   ("RHcham", "RH_cham", "rhcham"),
    "PPFD_in":  ("PPFD_in", "PPFDin", "ppfd_in", "Qin"),
}


def normalize_ack(raw: dict) -> dict:
    """Map a raw ack dict (any firmware field spelling) to the canonical shape."""
    out: dict[str, Any] = {
        "cmd_id": str(raw.get("cmd_id", "")),
        "ts": float(raw.get("ts") or time.time()),
        "error": str(raw.get("error", "") or ""),
    }
    for canonical, aliases in ACK_ALIASES.items():
        val: Any = None
        for alias in aliases:
            if alias in raw and raw[alias] is not None:
                try:
                    val = float(raw[alias])
                except (TypeError, ValueError):
                    val = None
                break
        out[canonical] = val
    return out

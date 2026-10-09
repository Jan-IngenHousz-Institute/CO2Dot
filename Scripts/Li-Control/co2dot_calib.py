"""
co2dot_calib.py — multi-device CO2Dot calibration runs on the LI-6800.

Generates the dataset needed to refit the two-state indicator model
(end-member channel vectors E0/E1, per-device scale, alpha_ref(a_w), dH(a_w),
dye time constant). Three decoupled parts:

  LOGGER     fixed-cadence background thread: reads the LI-6800 and every CO2Dot,
             appends one JSON line per cycle to  <run>/cycles.jsonl
  SCHEDULER  walks the Protocol (RH blocks > T levels > CO2 steps), only changes
             setpoints and the phase tags the logger copies into each line, and
             appends events to <run>/events.jsonl. Checkpointed, resumable.
  LOADER     load_run(<run>) -> tidy pandas DataFrames for the analysis notebooks.

    dots  = connect_dots(labels={"3C:DC:75:0D:FA:64": "A"})   # identity = USB serial no.
    preflight(dots)                                            # LED / saturation / noise
    li    = LI6800.connect()
    run   = Run(outdir, dots, LicorLive(li), Protocol())
    run.run()                                                  # Ctrl-C = pause; run() again = resume

Dry run without hardware:  Run(outdir, mock_dots(2), MockLicor(), Protocol.quick()).run()

Design notes
  * Devices are keyed by the USB serial number (ESP32-C3 reports its MAC); COM
    ports change between sessions. The firmware `hello` has no id.
  * The CO2Dot is opened ONCE (USB-CDC open resets the MCU); on error the logger
    reconnects that port and the others keep going.
  * A dew-point guard refuses (T, RH) cells whose dew point comes within
    `dewpoint_margin_c` of the coldest surface (exchanger or device board), and
    watches the live H2O during dwells.
  * CO2 cartridge watchdog: if a setpoint above ambient is not reached while a
    lower one was, the run pauses (keeps logging) instead of burning cells.
  * Opt-in Protocol options (off by default): settle each cell at its first CO2
    setpoint, a separate 0 ppm tolerance, re-checking a tripped cell guard before
    skipping, and drying the air before a cool-down (the exchanger undershoots
    the new T by ~6 C, which tripped the guard on 2026-10-08).
  * The LED current is quantized by the AS7341 driver: 4 mA minimum, 2 mA steps,
    20 mA board maximum. `spec_flash,1` of the 2026-06 datasets was 4 mA. The default
    request is 1 mA (driven at 4 mA); the 2026-10-07 run used 10 mA.
"""
from __future__ import annotations

import json
import math
import random
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

CHANNELS = ["f1_415", "f2_445", "f3_480", "f4_515", "f5_555", "f6_590", "f7_630", "f8_680", "clear", "nir"]
LICOR_KEYS = ["TIME", "CO2_s", "CO2_r", "H2O_s", "H2O_r", "Tchamber", "Txchg", "Tleaf", "Tleaf2",
              "RHcham", "Flow", "Press", "Fan_speed", "Ta", "Tirga_block"]
AS7341_FULL_SCALE = 65535


def led_ma_actual(requested: int) -> int:
    """LED current the AS7341 firmware actually drives: 0 = off, else 4 mA minimum, 2 mA steps, 20 mA board max."""
    if requested <= 0:
        return 0
    r = min(max(int(requested), 4), 20)
    return 4 + ((r - 4) // 2) * 2


# ----------------------------------------------------------------------------- #
# psychrometrics
# ----------------------------------------------------------------------------- #
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


def rh_at(t_from_c: float, rh_from_pct: float, t_to_c: float) -> float:
    """RH that the same air shows at another temperature (e.g. a warmer board)."""
    return rh_from_pct * psat_kpa(t_from_c) / psat_kpa(t_to_c)


# ----------------------------------------------------------------------------- #
# device identity and connection
# ----------------------------------------------------------------------------- #
@dataclass
class PortInfo:
    port: str
    serial_number: Optional[str]
    vid: Optional[int]
    pid: Optional[int]
    description: str


def scan_ports() -> List[PortInfo]:
    import serial.tools.list_ports
    return [PortInfo(p.device, p.serial_number, p.vid, p.pid, p.description or "")
            for p in serial.tools.list_ports.comports()]


class DotHandle:
    """A CO2Dot plus its stable identity; knows how to reconnect itself."""

    def __init__(self, dot, port: str, device_id: str, label: str, firmware: Dict[str, Any]):
        self.dot, self.port, self.device_id, self.label, self.firmware = dot, port, device_id, label, firmware
        self.errors = 0

    def reconnect(self, hello_timeout_s: float = 4.0) -> None:
        from co2dot import CO2Dot
        try:
            self.dot.close()
        except Exception:
            pass
        self.dot = CO2Dot.connect(port=self.port, hello_timeout_s=hello_timeout_s)

    def close(self) -> None:
        try:
            self.dot.close()
        except Exception:
            pass


def connect_dots(labels: Optional[Dict[str, str]] = None, include_ports: Optional[Sequence[str]] = None,
                 hello_timeout_s: float = 4.0, verbose: bool = True) -> Dict[str, DotHandle]:
    """Open every CO2Dot once; return {device_id: DotHandle}.

    device_id = USB serial number (chip MAC on ESP32-C3) or 'port:<COMx>' if absent.
    labels maps device_id -> short bench label (persisted in the run metadata).
    """
    from co2dot import CO2Dot
    labels = labels or {}
    handles: Dict[str, DotHandle] = {}
    for info in scan_ports():
        if include_ports and info.port not in include_ports:
            continue
        try:
            dot = CO2Dot.connect(port=info.port, hello_timeout_s=hello_timeout_s)
        except Exception as e:
            if verbose:
                print(f"  {info.port}: not a CO2Dot ({str(e)[:70]})")
            continue
        dev_id = info.serial_number or f"port:{info.port}"
        try:
            fw = dot.hello()
        except Exception:
            fw = {}
        handles[dev_id] = DotHandle(dot, info.port, dev_id, labels.get(dev_id, dev_id[-5:]), fw)
        if verbose:
            print(f"  {info.port}: {dot.device} v{fw.get('version', '?')}  id={dev_id}  label={handles[dev_id].label}")
    if verbose:
        print(f"{len(handles)} CO2Dot(s) connected")
    return handles


def apply_settings(dots: Dict[str, DotHandle], gain: Optional[int] = None, atime: Optional[int] = None,
                   astep: Optional[int] = None, per_device: Optional[Dict[str, Dict[str, int]]] = None
                   ) -> Dict[str, Dict[str, Any]]:
    """Set spectrometer gain/atime/astep (common or per device_id); return the read-back settings."""
    out = {}
    for dev_id, h in dots.items():
        kw = dict(gain=gain, atime=atime, astep=astep)
        kw.update((per_device or {}).get(dev_id, {}))
        kw = {k: v for k, v in kw.items() if v is not None}
        if kw:
            h.dot.set_spectrometer(**kw)
        out[dev_id] = h.dot.spec_status()
    return out


def preflight(dots: Dict[str, DotHandle], led_ma: int = 1, n: int = 3, min_led_ratio: float = 0.5,
              verbose: bool = True) -> Dict[str, Dict[str, Any]]:
    """Per device: command timing, saturation, LED signal (diff vs dark) and shot-to-shot noise.

    A device passes the LED check when diff/dark >= min_led_ratio on the clear channel
    and diff is well above a few counts. Warnings are printed, nothing raises.
    """
    report = {}
    for dev_id, h in dots.items():
        rec: Dict[str, Any] = {"port": h.port, "label": h.label, "warnings": []}
        try:
            t0 = time.perf_counter(); fl = [h.dot.spec_flash(led_ma) for _ in range(n)]
            rec["spec_flash_s"] = (time.perf_counter() - t0) / n
            rec["settings"] = h.dot.spec_status(); rec["env"] = h.dot.env()
            diff = {c: statistics.mean(f["diff"][c] for f in fl) for c in CHANNELS if c in fl[0]["diff"]}
            dark = {c: statistics.mean(f["dark"][c] for f in fl) for c in diff}
            lit = {c: max(f["lit"][c] for f in fl) for c in diff}
            sd = {c: (statistics.pstdev([f["diff"][c] for f in fl]) if n > 1 else 0.0) for c in diff}
            rec.update(diff=diff, dark=dark, lit_max=lit, diff_sd=sd)
            if max(lit.values()) >= AS7341_FULL_SCALE * 0.95:
                rec["warnings"].append("saturation: lower gain or atime")
            if diff.get("clear", 0) < 50 or diff.get("clear", 0) < min_led_ratio * max(dark.get("clear", 0), 1):
                rec["warnings"].append("LED signal weak: LED off/unconnected, no target in front, or ambient light too high")
            if dark.get("clear", 0) > 0.3 * max(diff.get("clear", 1), 1):
                rec["warnings"].append("ambient light high relative to LED signal: shield the device")
            rel = [sd[c] / diff[c] for c in ("f4_515", "f7_630") if diff.get(c, 0) > 0]
            rec["noise_rel_515_630"] = rel
            if rel and max(rel) > 0.01:
                rec["warnings"].append("shot-to-shot noise > 1 %")
        except Exception as e:
            rec["warnings"].append(f"FAILED: {e}")
        report[dev_id] = rec
        if verbose:
            d = rec.get("diff", {})
            print(f"[{h.label} {dev_id} {h.port}] flash {rec.get('spec_flash_s', float('nan')):.2f} s  "
                  f"LED {led_ma} mA requested = {led_ma_actual(led_ma)} mA  settings {rec.get('settings')}")
            if d:
                print("   diff: " + "  ".join(f"{c[-3:]}:{d[c]:.0f}" for c in d) +
                      f"   dark clear {rec['dark'].get('clear', 0):.0f}   noise 515/630 "
                      + "/".join(f"{100*r:.2f}%" for r in rec["noise_rel_515_630"]))
            for w in rec["warnings"]:
                print("   WARNING:", w)
    return report


# ----------------------------------------------------------------------------- #
# instrument adapters (real and mock)
# ----------------------------------------------------------------------------- #
class LicorLive:
    """Thin adapter around li_mqtt.LI6800 so the Run only needs read/set/get."""

    def __init__(self, li):
        self.li = li

    def read(self) -> Dict[str, Any]:
        return self.li.read()

    def get(self, key: str, default=None):
        return self.li.get(key, default)

    def set(self, **kw) -> None:
        self.li.set(**kw)


class MockLicor:
    """First-order simulation of the LI-6800 controllers for dry runs (real time).

    tau_s: controller time constants. cartridge_empty_after_s: after this many
    seconds the mixer can no longer raise CO2 above ambient (watchdog test).
    """

    def __init__(self, tau_s: Dict[str, float] = None, room_t: float = 23.0,
                 cartridge_empty_after_s: Optional[float] = None):
        self.tau = {"co2": 15.0, "t": 60.0, "rh": 45.0}
        self.tau.update(tau_s or {})
        self.room_t = room_t
        self.cartridge_empty_after_s = cartridge_empty_after_s
        self.t0 = time.monotonic()
        self.sp = {"co2_s": 420.0, "tair": room_t, "rh_air": 45.0, "flow": 400.0}
        self.state = {"co2": 420.0, "t": room_t, "rh": 45.0}
        self._last = time.monotonic()

    def _step(self):
        now = time.monotonic(); dt = now - self._last; self._last = now
        co2_sp = self.sp["co2_s"]
        if self.cartridge_empty_after_s is not None and now - self.t0 > self.cartridge_empty_after_s:
            co2_sp = min(co2_sp, 420.0)
        for k, sp, tau in (("co2", co2_sp, self.tau["co2"]), ("t", self.sp["tair"], self.tau["t"]),
                           ("rh", min(self.sp["rh_air"], 85.0), self.tau["rh"])):
            self.state[k] += (sp - self.state[k]) * (1 - math.exp(-dt / tau))

    def read(self) -> Dict[str, Any]:
        self._step()
        t, rh, co2 = self.state["t"], self.state["rh"], self.state["co2"]
        press = 101.3
        return {"TIME": time.time(), "CO2_s": co2, "CO2_r": co2 + 1.0, "H2O_s": 1000 * rh / 100 * psat_kpa(t) / press,
                "H2O_r": 8.0, "Tchamber": t, "Txchg": t - 2.0 if t < self.room_t else t + 1.0, "Tleaf": t + 0.3,
                "Tleaf2": 999.9, "RHcham": rh, "Flow": self.sp["flow"], "Press": press, "Fan_speed": 10000.0,
                "Ta": self.room_t + 4, "Tirga_block": 27.5}

    def get(self, key, default=None):
        return self.read().get(key, default)

    def set(self, **kw) -> None:
        for k, v in kw.items():
            if v is not None:
                self.sp[k] = float(v)


class MockDot:
    """Fake CO2Dot: two-state dye driven by a shared MockLicor, first-order film response."""
    _E0 = {"f1_415": .18, "f2_445": .133, "f3_480": .128, "f4_515": .156, "f5_555": .275, "f6_590": .506,
           "f7_630": .682, "f8_680": .318, "clear": .33, "nir": .10}
    _E1 = {"f1_415": .31, "f2_445": .316, "f3_480": .241, "f4_515": .164, "f5_555": .125, "f6_590": .117,
           "f7_630": .113, "f8_680": .110, "clear": .20, "nir": .10}
    _BASE = {"f1_415": 600, "f2_445": 1800, "f3_480": 1000, "f4_515": 2000, "f5_555": 3800, "f6_590": 4500,
             "f7_630": 5500, "f8_680": 5000, "clear": 7000, "nir": 400}

    def __init__(self, licor: MockLicor, alpha_per_pct: float = 100.0, tau_s: float = 20.0, scale: float = 1.0):
        self.licor, self.alpha, self.tau, self.scale = licor, alpha_per_pct, tau_s, scale
        self.device, self.port = "CO2Dot", "MOCK"
        self.x = 0.8; self._last = time.monotonic()
        self.cfg = {"model": "AS7341", "available": True, "atime": 100, "astep": 999, "gain": 5, "led": 0}

    def _advance(self):
        r = self.licor.read(); now = time.monotonic(); dt = now - self._last; self._last = now
        p = max(r["CO2_s"], 0.0) / 1e4
        a = self.alpha * math.exp(-0.09 * (r["Tchamber"] - 25.0)) * (1 - 0.004 * (r["RHcham"] - 50.0))
        x_eq = a * p / (1 + a * p)
        tau = self.tau * (1 + 0.02 * (r["RHcham"] - 40.0))
        self.x += (x_eq - self.x) * (1 - math.exp(-dt / max(tau, 1.0)))
        return r

    def hello(self): return {"device": "CO2Dot", "version": "mock"}
    def status(self): return {"spectrometer": self.cfg, "bme": {"available": True}}
    def spec_status(self): return dict(self.cfg)
    def set_spectrometer(self, **kw): self.cfg.update({k: v for k, v in kw.items() if v is not None}); return dict(self.cfg)
    def set_gain(self, g): self.cfg["gain"] = g; return g
    def close(self): pass

    def env(self):
        r = self.licor.read()
        return {"T": r["Tchamber"] + 4.0, "P": 1003.5, "RH": rh_at(r["Tchamber"], r["RHcham"], r["Tchamber"] + 4.0), "Gas": 1000}

    def spec_flash(self, led_ma=1):
        self._advance()
        g = 2 ** (self.cfg["gain"] - 5)
        dark, lit, diff = {}, {}, {}
        for c in CHANNELS:
            a = (1 - self.x) * self._E0[c] + self.x * self._E1[c]
            s = self.scale * g * self._BASE[c] * 10 ** (-a)
            d = 3 * g; s_n = s + random.gauss(0, 0.5 * math.sqrt(s) + 1); d_n = d + random.gauss(0, 1)
            dark[c] = int(max(d_n, 0)); lit[c] = int(min(max(s_n + d_n, 0), AS7341_FULL_SCALE))
            diff[c] = max(lit[c] - dark[c], 0)
        return {"led_current": led_ma, "model": "AS7341", "dark": dark, "lit": lit, "diff": diff}


def mock_dots(n: int = 2, licor: Optional[MockLicor] = None) -> Dict[str, DotHandle]:
    licor = licor or MockLicor()
    out = {}
    for i in range(n):
        dev_id = f"MOCK:{i:02d}"
        out[dev_id] = DotHandle(MockDot(licor, scale=1.0 - 0.15 * i), f"MOCK{i}", dev_id, f"M{i}", {"version": "mock"})
    return out


# ----------------------------------------------------------------------------- #
# protocol
# ----------------------------------------------------------------------------- #
@dataclass
class Protocol:
    """Grid and timing of one calibration run. All times in seconds, CO2 in ppm, T in degC, RH in %.

    Humidity is the slowest variable, so RH blocks are outermost, T levels inside,
    CO2 steps innermost. `passes` > 1 repeats the whole grid in reversed order to
    separate drift/hysteresis from condition. A zero (scrub) and an anchor step
    open each RH block; the zero is repeated at the end of each pass. `extra_cells`
    appends one more block after the last pass, closed by an anchor + zero.
    """
    rh_levels: Tuple[float, ...] = (30, 45, 60, 75)
    t_levels: Tuple[float, ...] = (17, 22, 27, 32)
    co2_levels: Tuple[float, ...] = (0, 150, 300, 400, 500, 700, 1000, 2000)
    rh_cap_by_t: Dict[float, float] = field(default_factory=lambda: {17: 60, 22: 60})  # condensation guard (static)
    co2_jitter_ppm: float = 30.0            # random +-jitter on every non-zero CO2 step
    co2_dwell_s: float = 300.0              # dwell after the CO2 setpoint is reached
    co2_dwell_long_s: float = 600.0         # used when rh >= humid_rh or T <= cold_t (slow film)
    humid_rh: float = 60.0
    cold_t: float = 20.0
    co2_tol_ppm: float = 3.0
    co2_timeout_s: float = 300.0
    t_tol_c: float = 0.5
    t_timeout_s: float = 1500.0
    t_settle_min_s: float = 900.0           # after Tchamber is within tolerance
    rh_tol_pct: float = 3.0
    rh_timeout_s: float = 1800.0
    rh_settle_min_s: float = 2700.0         # film hydration: minimum hold after RHcham is within tolerance
    rh_settle_max_s: float = 5400.0         # ... and maximum, if the drift criterion never passes
    drift_window_s: float = 600.0           # film drift criterion window
    drift_tol: float = 2e-3                 # max |change| of log10(630/515) and log10(415/680) over the window
    anchor: Dict[str, float] = field(default_factory=lambda: {"t": 25.0, "rh": 50.0, "co2": 400.0})
    anchor_dwell_s: float = 600.0
    zero_dwell_s: float = 600.0
    passes: int = 2
    flow_umol_s: float = 400.0
    dewpoint_margin_c: float = 3.0
    board_warmup_c: float = 4.0             # expected board temperature above chamber air (for the guard)
    ambient_restore_ppm: float = 420.0
    seed: int = 0
    cartridge_life_h: float = 10.0          # assumed mixer-active hours per 8 g cartridge; EDIT from experience
    # options, off by default (original behaviour)
    co2_zero_tol_ppm: Optional[float] = None    # a 0 ppm setpoint counts as reached below this (scrub floor ~5-10 ppm)
    settle_at_next_co2: bool = False            # hold the cell's first CO2 setpoint during the T/RH settle
    cell_zero_dwell_s: Optional[float] = None   # dwell of each cell's 0 ppm step; use with settle_at_next_co2
    guard_wait_s: float = 0.0                   # re-check a tripped cell guard this long before skipping the cell
    cooldown_dry: bool = False                  # before cooling, dry until the dew point is cooldown_dew_gap_c below new T
    cooldown_dew_gap_c: float = 7.0             # exchanger undershoot on a 5-8 C cool-down (~6 C observed) + 1 C
    cooldown_dry_timeout_s: float = 900.0
    max_dew_c: Optional[float] = None           # skip cells whose target dew point is above this (room-temperature
                                                # lines; the LI-6800 humidifier topped out at ~19.7 C dew on 2026-10-08)
    extra_cells: Tuple[Tuple[float, float], ...] = ()   # (t, rh) cells run as one block after the last pass, framed by
                                                        # anchor + zero; rh_cap_by_t does not apply, the margins do
    extra_dwell_s: Optional[float] = None       # dwell of every CO2 step of the extra block, 0 ppm included

    @classmethod
    def quick(cls) -> "Protocol":
        """Compressed protocol for mock dry runs (seconds instead of minutes)."""
        return cls(rh_levels=(40, 60), t_levels=(20, 28), co2_levels=(0, 300, 600, 2000), rh_cap_by_t={},
                   co2_dwell_s=6, co2_dwell_long_s=9, co2_timeout_s=20, t_timeout_s=30, t_settle_min_s=4,
                   rh_timeout_s=30, rh_settle_min_s=4, rh_settle_max_s=12, drift_window_s=4, anchor_dwell_s=5,
                   zero_dwell_s=5, passes=1)

    # -- step generation --------------------------------------------------- #
    def dwell_for(self, t: float, rh: float) -> float:
        return self.co2_dwell_long_s if (rh >= self.humid_rh or t <= self.cold_t) else self.co2_dwell_s

    def rh_allowed(self, t: float, rh: float) -> Tuple[bool, str]:
        cap = self.rh_cap_by_t.get(t)
        if cap is not None and rh > cap:
            return False, f"rh {rh} > cap {cap} at {t} C"
        margin = t - dew_point_c(t, rh)
        if margin < self.dewpoint_margin_c:
            return False, f"dew-point margin {margin:.1f} C < {self.dewpoint_margin_c} C"
        return True, ""

    def steps(self) -> List[Dict[str, Any]]:
        """Deterministic flat list of steps; each has kind, pass, block ids, setpoints and dwell."""
        rng = random.Random(self.seed)
        out: List[Dict[str, Any]] = []

        def add(kind, p, rh, t, co2, dwell, **extra):
            out.append(dict(index=len(out), kind=kind, pass_=p, rh=rh, t=t, co2=co2, dwell_s=dwell, **extra))

        for p in range(self.passes):
            rhs = list(self.rh_levels); ts = list(self.t_levels)
            if p % 2 == 1:
                rhs.reverse(); ts.reverse()
            for rh in rhs:
                a = self.anchor
                add("anchor", p, a["rh"], a["t"], a["co2"], self.anchor_dwell_s)
                add("zero", p, a["rh"], a["t"], 0.0, self.zero_dwell_s)
                for t in ts:
                    ok, why = self.rh_allowed(t, rh)
                    if not ok:
                        add("skip", p, rh, t, None, 0.0, reason=why)
                        continue
                    add("cell_settle", p, rh, t, None, 0.0)
                    for sp in self.co2_levels:
                        jit = 0.0 if sp == 0 else rng.uniform(-self.co2_jitter_ppm, self.co2_jitter_ppm)
                        dwell = self.cell_zero_dwell_s if (sp == 0 and self.cell_zero_dwell_s is not None) \
                            else self.dwell_for(t, rh)
                        add("co2", p, rh, t, round(sp + jit, 1), dwell, co2_nominal=sp)
            add("zero", p, self.anchor["rh"], self.anchor["t"], 0.0, self.zero_dwell_s, final=True)
        if self.extra_cells:                    # appended after the passes, so earlier step indices never change
            p, a = self.passes, self.anchor
            add("anchor", p, a["rh"], a["t"], a["co2"], self.anchor_dwell_s)
            add("zero", p, a["rh"], a["t"], 0.0, self.zero_dwell_s)
            for t, rh in self.extra_cells:
                margin = t - dew_point_c(t, rh)
                if margin < self.dewpoint_margin_c:
                    add("skip", p, rh, t, None, 0.0, reason=f"dew-point margin {margin:.1f} C < {self.dewpoint_margin_c} C")
                    continue
                add("cell_settle", p, rh, t, None, 0.0)
                for sp in self.co2_levels:
                    jit = 0.0 if sp == 0 else rng.uniform(-self.co2_jitter_ppm, self.co2_jitter_ppm)
                    dwell = self.extra_dwell_s if self.extra_dwell_s is not None else (
                        self.cell_zero_dwell_s if (sp == 0 and self.cell_zero_dwell_s is not None) else self.dwell_for(t, rh))
                    add("co2", p, rh, t, round(sp + jit, 1), dwell, co2_nominal=sp)
            add("anchor", p, a["rh"], a["t"], a["co2"], self.anchor_dwell_s, final=True)
            add("zero", p, a["rh"], a["t"], 0.0, self.zero_dwell_s, final=True)
        return out

    def estimate(self) -> Dict[str, float]:
        """Rough duration and cartridge use. Settling times use their minimum plus half the timeout."""
        steps = self.steps()
        total = 0.0; mixer = 0.0
        last_rh = last_t = None
        for s in steps:
            if s["kind"] == "skip":
                continue
            if s["rh"] != last_rh:
                total += self.rh_settle_min_s + 0.5 * self.rh_timeout_s; last_rh = s["rh"]
            if s["t"] != last_t:
                total += self.t_settle_min_s + 0.5 * self.t_timeout_s; last_t = s["t"]
            if s["kind"] in ("co2", "anchor", "zero"):
                dt = s["dwell_s"] + 0.3 * self.co2_timeout_s
                total += dt
                if s["co2"] and s["co2"] > 0:
                    mixer += dt
        n_cells = len({(s["rh"], s["t"]) for s in steps if s["kind"] == "co2"})
        return {"hours": round(total / 3600, 1), "mixer_hours": round(mixer / 3600, 1),
                "cartridges": round(mixer / 3600 / self.cartridge_life_h, 2), "cells": n_cells,
                "co2_steps": sum(s["kind"] == "co2" for s in steps), "skipped": sum(s["kind"] == "skip" for s in steps)}

    def feasibility_table(self) -> str:
        """Text table of allowed (T, RH) cells, with dew-point margins and the RH the warm board would see."""
        lines = ["T(C) | " + " | ".join(f"RH {rh:>3.0f}%        " for rh in self.rh_levels)]
        for t in self.t_levels:
            cells = []
            for rh in self.rh_levels:
                ok, _ = self.rh_allowed(t, rh)
                cells.append(f"{'ok  ' if ok else 'SKIP'} m{t - dew_point_c(t, rh):4.1f} b{rh_at(t, rh, t + self.board_warmup_c):3.0f}")
            lines.append(f"{t:4.0f} | " + " | ".join(cells))
        lines.append("m = dew-point margin (C), b = RH at the board (+%.0f C)" % self.board_warmup_c)
        return "\n".join(lines)


# ----------------------------------------------------------------------------- #
# run: logger thread + scheduler
# ----------------------------------------------------------------------------- #
def _json_default(o):
    if hasattr(o, "item"):
        return o.item()
    return str(o)


class Run:
    """One calibration run directory: cycles.jsonl, events.jsonl, meta.json, checkpoint.json."""

    def __init__(self, outdir, dots: Dict[str, DotHandle], licor, protocol: Protocol, *,
                 cadence_s: float = 10.0, led_ma: int = 1, settings_every_n: int = 30,
                 meta_extra: Optional[Dict[str, Any]] = None, verbose: bool = True):
        self.outdir = Path(outdir); self.outdir.mkdir(parents=True, exist_ok=True)
        self.dots, self.licor, self.protocol = dots, licor, protocol
        self.cadence_s, self.led_ma, self.settings_every_n, self.verbose = cadence_s, led_ma, settings_every_n, verbose
        self.phase: Dict[str, Any] = {"kind": "idle", "step": None}
        self._phase_lock = threading.Lock()
        self._stop = threading.Event()
        self._logger: Optional[threading.Thread] = None
        self.recent: deque = deque(maxlen=int(max(protocol.drift_window_s, 60) / max(cadence_s, 0.1)) + 5)
        self.cycle = 0
        self.status = "new"
        self._write_meta(meta_extra or {})

    # -- files ------------------------------------------------------------- #
    def _write_meta(self, extra):
        meta_path = self.outdir / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        meta.setdefault("created", datetime.now().isoformat(timespec="seconds"))
        meta.update({
            "cadence_s": self.cadence_s, "led_ma_requested": self.led_ma, "led_ma_actual": led_ma_actual(self.led_ma),
            "led_ma_note": "AS7341 driver quantizes to 4 mA minimum, 2 mA steps, 20 mA max",
            "protocol": asdict(self.protocol),
            "devices": {d: {"port": h.port, "label": h.label, "firmware": h.firmware} for d, h in self.dots.items()},
            "licor_keys": LICOR_KEYS, "channels": CHANNELS, "licor_class": type(self.licor).__name__,
        })
        meta.update(extra)
        meta_path.write_text(json.dumps(meta, indent=2, default=_json_default), encoding="utf-8")

    def event(self, kind: str, **data) -> None:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "t_mono": time.monotonic(), "kind": kind, **data}
        with (self.outdir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=_json_default) + "\n")
        if self.verbose:
            msg = " ".join(f"{k}={v}" for k, v in data.items() if k not in ("step",))
            print(f"{rec['ts']}  {kind:<18s} {msg}")

    def _checkpoint(self, next_index: int) -> None:
        (self.outdir / "checkpoint.json").write_text(json.dumps({"next_index": next_index, "status": self.status,
                                                                 "ts": datetime.now().isoformat(timespec="seconds")}),
                                                     encoding="utf-8")

    def _load_checkpoint(self) -> int:
        p = self.outdir / "checkpoint.json"
        return json.loads(p.read_text(encoding="utf-8")).get("next_index", 0) if p.exists() else 0

    # -- logger ------------------------------------------------------------ #
    def set_phase(self, **kw) -> None:
        with self._phase_lock:
            self.phase = dict(kw)

    def _read_device(self, h: DotHandle, with_settings: bool) -> Dict[str, Any]:
        rec: Dict[str, Any] = {"port": h.port}
        try:
            rec["spec_flash"] = h.dot.spec_flash(self.led_ma)
            rec["env"] = h.dot.env()
            if with_settings:
                rec["spec"] = h.dot.spec_status()
            h.errors = 0
        except Exception as e:
            rec["error"] = str(e); h.errors += 1
            if h.errors in (2, 5, 10):
                try:
                    h.reconnect(); self.event("device_reconnected", device=h.device_id, port=h.port)
                except Exception as e2:
                    self.event("device_reconnect_failed", device=h.device_id, error=str(e2))
        return rec

    def _logger_loop(self) -> None:
        path = self.outdir / "cycles.jsonl"
        t_next = time.monotonic()
        with path.open("a", encoding="utf-8") as f:
            while not self._stop.is_set():
                t_start = time.monotonic()
                with self._phase_lock:
                    phase = dict(self.phase)
                try:
                    licor = self.licor.read()
                except Exception as e:
                    licor = {"error": str(e)}
                line = {"ts": datetime.now().isoformat(timespec="milliseconds"), "t_mono": t_start, "cycle": self.cycle,
                        "phase": phase, "licor": licor,
                        "devices": {d: self._read_device(h, self.cycle % self.settings_every_n == 0)
                                    for d, h in self.dots.items()}}
                f.write(json.dumps(line, default=_json_default) + "\n"); f.flush()
                self.recent.append(line)
                self.cycle += 1
                t_next += self.cadence_s
                sleep = t_next - time.monotonic()
                if sleep < 0:          # fell behind (too many devices for the cadence): resync
                    if self.cycle % 50 == 1:
                        self.event("logger_late", behind_s=round(-sleep, 2))
                    t_next = time.monotonic()
                else:
                    self._stop.wait(sleep)

    def start_logger(self) -> None:
        if self._logger and self._logger.is_alive():
            return
        self._stop.clear()
        self._logger = threading.Thread(target=self._logger_loop, name="co2dot-logger", daemon=True)
        self._logger.start()
        self.event("logger_started", cadence_s=self.cadence_s, devices=len(self.dots))

    def stop_logger(self) -> None:
        self._stop.set()
        if self._logger:
            self._logger.join(timeout=self.cadence_s + 15)
        self.event("logger_stopped", cycles=self.cycle)

    # -- scheduler helpers -------------------------------------------------- #
    def _wait(self, key: str, target: float, tol: float, timeout_s: float) -> Tuple[bool, float]:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if self._stop.is_set():
                raise KeyboardInterrupt
            v = self.licor.get(key)
            if isinstance(v, (int, float)) and abs(v - target) < tol:
                return True, time.monotonic() - t0
            self._stop.wait(1.0)
        v = self.licor.get(key)             # last reading: a setpoint reached while the PC slept is not a timeout
        ok = isinstance(v, (int, float)) and abs(v - target) < tol
        return ok, time.monotonic() - t0

    def _hold(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise KeyboardInterrupt

    def film_drift(self) -> Optional[float]:
        """Max |change over the drift window| of log10(630/515) and log10(415/680) across devices."""
        W = self.protocol.drift_window_s
        now = time.monotonic()
        pts = [c for c in list(self.recent) if now - c["t_mono"] <= W]
        if len(pts) < 5:
            return None
        worst = 0.0
        for d in self.dots:
            for num, den in (("f7_630", "f4_515"), ("f1_415", "f8_680")):
                xs, ys = [], []
                for c in pts:
                    r = c["devices"].get(d, {}).get("spec_flash")
                    if r and r["diff"].get(num, 0) > 0 and r["diff"].get(den, 0) > 0:
                        xs.append(c["t_mono"]); ys.append(math.log10(r["diff"][num] / r["diff"][den]))
                if len(xs) < 5:
                    continue
                mx, my = statistics.mean(xs), statistics.mean(ys)
                sxx = sum((x - mx) ** 2 for x in xs)
                slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
                worst = max(worst, abs(slope * W))
        return worst

    def _dewpoint_guard(self, t_set: float) -> Optional[str]:
        """Live check: dew point of the sample air vs coldest surface (exchanger, board). None if fine."""
        r = self.licor.read()
        h2o, press = r.get("H2O_s"), r.get("Press")
        if not isinstance(h2o, (int, float)) or not isinstance(press, (int, float)):
            return None
        td = dew_point_from_h2o(h2o, press)
        cold = [v for v in (r.get("Txchg"), r.get("Tchamber"), t_set) if isinstance(v, (int, float)) and v < 500]
        if self.recent:
            for d in self.recent[-1]["devices"].values():
                tb = (d.get("env") or {}).get("T")
                if isinstance(tb, (int, float)):
                    cold.append(tb)
        if not cold:
            return None
        margin = min(cold) - td
        if margin < self.protocol.dewpoint_margin_c:
            return f"dew point {td:.1f} C within {margin:.1f} C of coldest surface {min(cold):.1f} C"
        return None

    def _sample_dew_point(self) -> Optional[float]:
        r = self.licor.read()
        h2o, press = r.get("H2O_s"), r.get("Press")
        if not isinstance(h2o, (int, float)) or not isinstance(press, (int, float)):
            return None
        return dew_point_from_h2o(h2o, press)

    def _dry_before_cooling(self, i: int, t_new: float) -> bool:
        """Lower the RH setpoint until the sample dew point is cooldown_dew_gap_c below t_new. True if it dried."""
        P = self.protocol
        target = t_new - P.cooldown_dew_gap_c
        td, t_now = self._sample_dew_point(), self.licor.get("Tchamber")
        if td is None or not isinstance(t_now, (int, float)) or td <= target:
            return False
        rh_dry = max(5.0, round(100.0 * psat_kpa(target - 1.0) / psat_kpa(t_now), 1))   # aim 1 C below target
        self.licor.set(rh_air=rh_dry)
        self.event("cooldown_dry", index=i, rh_air=rh_dry, td=round(td, 2), target_td=round(target, 2))
        t0 = time.monotonic()
        while time.monotonic() - t0 < P.cooldown_dry_timeout_s:
            td = self._sample_dew_point()
            if td is not None and td <= target:
                break
            self._hold(5.0)
        self.event("cooldown_dried", index=i, after_s=round(time.monotonic() - t0),
                   td=None if td is None else round(td, 2), reached=td is not None and td <= target)
        return True

    # -- scheduler ----------------------------------------------------------- #
    def run(self, resume: bool = True, start_index: Optional[int] = None) -> str:
        """Execute the protocol. Returns 'done', 'paused' (cartridge) or 'interrupted'.

        Ctrl-C (KeyboardInterrupt) stops cleanly: chamber restored, checkpoint kept.
        Call run() again to resume from the checkpoint.
        """
        P = self.protocol
        steps = P.steps()
        i = start_index if start_index is not None else (self._load_checkpoint() if resume else 0)
        self.status = "running"
        self.start_logger()
        self.event("run_started", from_index=i, steps=len(steps), estimate=P.estimate())
        cur_rh = cur_t = None
        co2_fail_streak = 0
        reached_above_ambient = False
        drift = None
        try:
            self.licor.set(flow=P.flow_umol_s)
            while i < len(steps):
                s = steps[i]
                self._checkpoint(i)
                if s["kind"] == "skip":
                    self.event("cell_skipped", index=i, rh=s["rh"], t=s["t"], reason=s["reason"]); i += 1; continue
                if s["kind"] in ("cell_settle", "co2") and P.max_dew_c is not None \
                        and dew_point_c(s["t"], s["rh"]) > P.max_dew_c:     # also when resuming inside such a cell
                    self.event("cell_skipped", index=i, rh=s["rh"], t=s["t"],
                               reason=f"dew point {dew_point_c(s['t'], s['rh']):.1f} C > max_dew_c {P.max_dew_c} C")
                    j = i + 1          # step list unchanged (resume indices stay valid); chamber stays where it is
                    while j < len(steps) and steps[j]["kind"] == "co2" and steps[j]["rh"] == s["rh"] and steps[j]["t"] == s["t"]:
                        j += 1
                    i = j; continue

                # ---- humidity / temperature of the cell (also for anchor and zero steps)
                if s["rh"] != cur_rh or s["t"] != cur_t:
                    new_rh = s["rh"] != cur_rh
                    settle_co2 = None
                    if P.settle_at_next_co2:     # the cell's first CO2 step (or this anchor/zero step's own)
                        nxt = steps[i + 1] if s["kind"] == "cell_settle" and i + 1 < len(steps) else s
                        if nxt.get("co2") is not None:
                            settle_co2 = float(nxt["co2"]); self.licor.set(co2_s=settle_co2)
                    self.set_phase(kind="settle", step=i, rh=s["rh"], t=s["t"], co2=None, pass_=s["pass_"],
                                   co2_settle=settle_co2)
                    cooling = cur_t is not None and s["t"] < cur_t
                    drier = cur_rh is not None and s["rh"] < cur_rh
                    dried = cooling and P.cooldown_dry and self._dry_before_cooling(i, s["t"])
                    rh_after_t = cooling and P.cooldown_dry and (dried or not drier)
                    # dry before cooling, warm before humidifying: order the two setpoints accordingly
                    if rh_after_t:       # cooling: keep the current (dry) RH setpoint until T is reached
                        self.licor.set(tair=s["t"])
                    elif drier:
                        self.licor.set(rh_air=s["rh"]); self.licor.set(tair=s["t"])
                    else:
                        self.licor.set(tair=s["t"]); self.licor.set(rh_air=s["rh"])
                    self.event("setpoint", index=i, tair=s["t"], rh_air=s["rh"])
                    ok_t, dt_t = self._wait("Tchamber", s["t"], P.t_tol_c, P.t_timeout_s)
                    self.event("t_reached" if ok_t else "t_timeout", index=i, t=s["t"], after_s=round(dt_t),
                               Tchamber=self.licor.get("Tchamber"))
                    if rh_after_t:
                        self.licor.set(rh_air=s["rh"])
                    ok_rh, dt_rh = self._wait("RHcham", s["rh"], P.rh_tol_pct, P.rh_timeout_s)
                    self.event("rh_reached" if ok_rh else "rh_timeout", index=i, rh=s["rh"], after_s=round(dt_rh),
                               RHcham=self.licor.get("RHcham"))
                    # the exchanger undershoots right after a cool-down: give the guard time before skipping
                    guard = self._dewpoint_guard(s["t"])
                    t_g = time.monotonic()
                    while guard and time.monotonic() - t_g < P.guard_wait_s:
                        self._hold(20.0); guard = self._dewpoint_guard(s["t"])
                    if guard:
                        self.event("condensation_guard", index=i, detail=guard, action="skip cell",
                                   waited_s=round(time.monotonic() - t_g))
                        cur_rh, cur_t = s["rh"], s["t"]
                        j = i
                        while j < len(steps) and steps[j]["rh"] == s["rh"] and steps[j]["t"] == s["t"]:
                            j += 1
                        i = j; continue
                    if time.monotonic() - t_g > 1.0:
                        self.event("condensation_guard_cleared", index=i, waited_s=round(time.monotonic() - t_g))
                    # hold for film equilibration: long after an RH change (or a dry-down), shorter after a T-only change
                    t_hold0 = time.monotonic()
                    long_hold = new_rh or dried
                    min_hold = P.rh_settle_min_s if long_hold else P.t_settle_min_s
                    max_hold = P.rh_settle_max_s if long_hold else P.t_settle_min_s
                    while True:
                        el = time.monotonic() - t_hold0
                        drift = self.film_drift()
                        if el >= min_hold and ((drift is not None and drift < P.drift_tol) or el >= max_hold):
                            break
                        self._hold(min(30.0, max(P.drift_window_s / 4, 1.0)))
                    self.event("cell_settled", index=i, rh=s["rh"], t=s["t"], held_s=round(time.monotonic() - t_hold0),
                               drift=None if drift is None else round(drift, 5))
                    cur_rh, cur_t = s["rh"], s["t"]
                if s["kind"] == "cell_settle":
                    i += 1; continue

                # ---- CO2 step (co2 / anchor / zero)
                sp = float(s["co2"])
                self.set_phase(kind=s["kind"], step=i, rh=s["rh"], t=s["t"], co2=sp, pass_=s["pass_"],
                               co2_nominal=s.get("co2_nominal", sp), transition=True)
                self.licor.set(co2_s=sp)
                tol = max(P.co2_tol_ppm, 0.01 * sp)
                if sp == 0 and P.co2_zero_tol_ppm:      # the scrubber never reaches 0
                    tol = P.co2_zero_tol_ppm
                ok, dt = self._wait("CO2_s", sp, tol, P.co2_timeout_s)
                co2_now = self.licor.get("CO2_s")
                self.event("co2_reached" if ok else "co2_timeout", index=i, step_kind=s["kind"], co2=sp,
                           after_s=round(dt), CO2_s=co2_now)
                # cartridge watchdog: a setpoint above ambient not reached while CO2_s stays well below it
                if sp > P.ambient_restore_ppm + 50:
                    if ok:
                        reached_above_ambient = True; co2_fail_streak = 0
                    elif isinstance(co2_now, (int, float)) and co2_now < 0.8 * sp:
                        co2_fail_streak += 1
                        self.event("cartridge_suspected", index=i, co2=sp, CO2_s=co2_now, streak=co2_fail_streak)
                        if co2_fail_streak >= 2 and reached_above_ambient:
                            self.status = "paused"; self._checkpoint(i)
                            self.event("run_paused", reason="CO2 cartridge probably empty: replace it and call run() again")
                            return self.status
                self.set_phase(kind=s["kind"], step=i, rh=s["rh"], t=s["t"], co2=sp, pass_=s["pass_"],
                               co2_nominal=s.get("co2_nominal", sp), transition=False)
                self._hold(s["dwell_s"])
                guard = self._dewpoint_guard(s["t"])
                if guard:
                    self.event("condensation_guard", index=i, detail=guard, action="continue, flagged")
                self.event("step_done", index=i, step_kind=s["kind"], co2=sp, rh=s["rh"], t=s["t"], dwell_s=s["dwell_s"])
                i += 1
            self.status = "done"; self._checkpoint(i)
            self.event("run_done", cycles=self.cycle)
        except KeyboardInterrupt:
            self.status = "interrupted"; self._checkpoint(i)
            self.event("run_interrupted", at_index=i)
        finally:
            self.set_phase(kind="idle", step=None)
            try:
                self.licor.set(co2_s=P.ambient_restore_ppm, tair=P.anchor["t"], rh_air=P.anchor["rh"])
                self.event("chamber_restored", co2_s=P.ambient_restore_ppm)
            except Exception as e:
                self.event("chamber_restore_failed", error=str(e))
            self.stop_logger()
        return self.status

    def close(self) -> None:
        self.stop_logger()
        for h in self.dots.values():
            h.close()


# ----------------------------------------------------------------------------- #
# loader
# ----------------------------------------------------------------------------- #
def load_run(outdir):
    """Read a run directory -> (cycles DataFrame, events DataFrame, meta dict).

    cycles: one row per (cycle, device): ts, cycle, device_id, label, phase_*, licor_*,
            env_T/RH/P, dark_*/lit_*/diff_*, gain/atime/astep (forward-filled),
            lr_630_515 and lr_415_680 = log10 ratios of diff counts.
    """
    import numpy as np
    import pandas as pd
    outdir = Path(outdir)
    meta = json.loads((outdir / "meta.json").read_text(encoding="utf-8"))
    labels = {d: v.get("label", d) for d, v in meta.get("devices", {}).items()}
    rows = []
    with (outdir / "cycles.jsonl").open(encoding="utf-8") as f:
        for line in f:
            try:
                c = json.loads(line)
            except json.JSONDecodeError:
                continue   # truncated last line after a crash
            base = {"ts": c["ts"], "t_mono": c["t_mono"], "cycle": c["cycle"]}
            base.update({f"phase_{k}": v for k, v in (c.get("phase") or {}).items()})
            if isinstance(c.get("licor"), dict):
                base.update({f"licor_{k}": c["licor"].get(k) for k in LICOR_KEYS})
            for d, rec in c["devices"].items():
                row = dict(base, device_id=d, label=labels.get(d, d), error=rec.get("error"))
                fl = rec.get("spec_flash")
                if fl:
                    for part in ("dark", "lit", "diff"):
                        row.update({f"{part}_{k}": v for k, v in fl.get(part, {}).items()})
                env = rec.get("env") or {}
                row.update({f"env_{k}": env.get(k) for k in ("T", "RH", "P", "Gas")})
                sp = rec.get("spec") or {}
                row.update({k: sp.get(k) for k in ("gain", "atime", "astep")})
                rows.append(row)
    cycles = pd.DataFrame(rows)
    if not cycles.empty:
        cycles["ts"] = pd.to_datetime(cycles["ts"])
        for k in ("gain", "atime", "astep"):
            cycles[k] = cycles.groupby("device_id")[k].ffill()
        for num, den, name in (("f7_630", "f4_515", "lr_630_515"), ("f1_415", "f8_680", "lr_415_680")):
            a, b = cycles.get(f"diff_{num}"), cycles.get(f"diff_{den}")
            if a is not None and b is not None:
                cycles[name] = np.log10(a.where(a > 0) / b.where(b > 0))
    ev_path = outdir / "events.jsonl"
    events = pd.DataFrame([json.loads(l) for l in ev_path.read_text(encoding="utf-8").splitlines() if l.strip()]) \
        if ev_path.exists() else pd.DataFrame()
    if not events.empty:
        events["ts"] = pd.to_datetime(events["ts"])
    return cycles, events, meta


def settled_points(cycles, last_s: float = 120.0):
    """Mean of the last `last_s` seconds of every CO2/anchor/zero step, per device.

    Uses the phase tags written by the scheduler (phase_step, phase_transition == False).
    Returns one row per (phase_step, device) with channel means, licor means and n cycles.
    """
    import pandas as pd
    df = cycles[cycles["phase_kind"].isin(["co2", "anchor", "zero"]) & (cycles["phase_transition"] == False)].copy()
    if df.empty:
        return pd.DataFrame()
    out = []
    num_cols = [c for c in df.columns if c.startswith(("diff_", "dark_", "lit_", "licor_", "env_", "lr_"))]
    for (step, dev), g in df.groupby(["phase_step", "device_id"]):
        g = g[g["t_mono"] >= g["t_mono"].max() - last_s]
        row = {"phase_step": step, "device_id": dev, "label": g["label"].iloc[0], "n": len(g),
               "kind": g["phase_kind"].iloc[0], "pass_": g["phase_pass_"].iloc[0], "rh_set": g["phase_rh"].iloc[0],
               "t_set": g["phase_t"].iloc[0], "co2_set": g["phase_co2"].iloc[0], "ts_end": g["ts"].max()}
        row.update(g[num_cols].apply(pd.to_numeric, errors="coerce").mean().to_dict())
        out.append(row)
    return pd.DataFrame(out)


if __name__ == "__main__":
    # Dry run without hardware: compressed protocol, two mock devices, 1 s cadence.
    out = Path("co2dot_data") / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_mock_dryrun")
    lic = MockLicor(tau_s={"co2": 2.0, "t": 2.0, "rh": 2.0})
    run = Run(out, mock_dots(2, lic), lic, Protocol.quick(), cadence_s=1.0)
    print(Protocol.quick().feasibility_table())
    print("status:", run.run())
    cyc, ev, meta = load_run(out)
    print(cyc.shape, ev["kind"].value_counts().to_dict())
    print(settled_points(cyc, last_s=3)[["phase_step", "label", "kind", "co2_set", "n", "diff_f7_630", "lr_630_515"]].head(12))
